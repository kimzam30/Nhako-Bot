"""Telegram side: auth, commands, typing indicator, confirm buttons, long-message splitting."""
import asyncio
import contextlib
import html
import io
import logging
import re

import httpx
from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import scheduler
from .agent import Agent, make_context
from .config import Settings
from .llm import LLM
from .monitor import Monitor
from .speech import Transcriber
from .store import Store
from .tools import default_registry, homelab
from .tools.calendar import GoogleCalendar, agenda
from .tools.homelab import Docker, docker_report, mc_report, parse_servers
from .tools.notion import Notion, morning_tasks, overdue_nudge, projects_report, stats_text, tasks_report, weekly_review
from .tools.postiz import Postiz, TikTokWatch, weekly_digest

log = logging.getLogger(__name__)

TELEGRAM_LIMIT = 4096

HELP = """I'm Nhako, running locally on {model}.

Just talk to me (text or voice):
• "what do I have tomorrow?"
• "add dentist next tuesday 3pm"
• "move the dentist to 5pm" / "remove the dentist thing" (I ask first)
• "remind me in 20 minutes to take the laundry out"
• "search for Paper 1.21 release notes"
• "who's on getgud?" / "is nextcloud healthy?"
• "add renew passport to my tasks by friday" / "what's due this week?" / "I finished the invoice"
• "move the landing page task to in progress" / "push bug fixes to friday" / "remove the thumbnail task"
• "how far along are my projects?" / "set Nhako Beam progress to 50%" / "pause NeraOS" / "how much XP this week?"
• "what's in the Nhako Depot page?"
• "remember that I prefer meetings after 10am" / "what do you know about me?"
• "restart nextcloud" / "pause the postiz stack" / "show vaultwarden logs"
• "what's going out on social media this week?" / "draft a tweet about the server wipe" (saved as a draft)
• "how did my tiktoks do this week?" / "how many followers did I gain this month?"

/today [tomorrow, this week, ...] lists your calendar instantly
/reminders shows reminders you can cancel
/mc [server] shows who's online
/docker [name, running, paused, stopped, created] shows your containers, tap one to start/stop/restart it
/logs name [lines] shows the end of a container's log
/tasks [today, this week, overdue, in progress] lists your open Notion tasks
/projects [all] shows your Notion projects and how far along they are
/stats [this week, this month, all time] shows tasks cleared, XP and your level
/memory shows what I remember about you, tap one to forget it
/ping checks that my brain and calendar are reachable
{agenda_line}"""

# The menu Telegram shows when you type "/".
COMMANDS = [
    BotCommand("today", "Calendar for today (or: tomorrow, this week...)"),
    BotCommand("tasks", "Open Notion tasks (or: today, this week, overdue)"),
    BotCommand("projects", "Notion projects and their progress (or: all)"),
    BotCommand("stats", "Tasks cleared, XP and level (or: this month, all time)"),
    BotCommand("reminders", "Upcoming reminders, tap to cancel"),
    BotCommand("mc", "Who's online on the Minecraft servers"),
    BotCommand("docker", "Containers: health, start/stop/restart (or: running, paused...)"),
    BotCommand("logs", "The end of a container's log"),
    BotCommand("memory", "What I remember about you"),
    BotCommand("ping", "Check the model and integrations"),
    BotCommand("help", "What I can do"),
]


def split_message(text: str, limit: int = TELEGRAM_LIMIT) -> list[str]:
    """Split on paragraph, then line, then hard boundaries so each part fits Telegram's limit."""
    parts, rest = [], text.strip()
    while len(rest) > limit:
        cut = max(rest.rfind("\n\n", 0, limit), rest.rfind("\n", 0, limit))
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    return parts + [rest] if rest else parts or [""]


@contextlib.asynccontextmanager
async def typing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Keep "typing..." visible while the model works (Telegram clears it after ~5s)."""
    async def loop():
        while True:
            with contextlib.suppress(Exception):
                await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
            await asyncio.sleep(4)
    task = asyncio.create_task(loop())
    try:
        yield
    finally:
        task.cancel()


def confirm_buttons(action_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✅ Confirm", callback_data=f"ok:{action_id}"),
                                  InlineKeyboardButton("❌ Cancel", callback_data=f"no:{action_id}")]])


def to_html(text: str) -> str:
    """The model writes light Markdown (**bold**, `code`); Telegram wants HTML or shows the asterisks."""
    t = html.escape(text, quote=False)
    t = re.sub(r"^(\s*)[*-]\s+", r"\1• ", t, flags=re.M)  # "*   item" bullets
    t = re.sub(r"^#{1,6}\s+(.+)$", r"<b>\1</b>", t, flags=re.M)  # "## Heading"
    t = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", t)
    t = re.sub(r"`([^`\n]+)`", r"<code>\1</code>", t)
    # [text](https://...) links. The text was escaped above; a quote can't end the href.
    t = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s\"]+)\)", r'<a href="\2">\1</a>', t)
    return t


async def send_to(bot, chat_id: int, text: str, markup=None) -> None:
    parts = split_message(text)
    for i, part in enumerate(parts):
        kwargs = dict(reply_markup=markup if i == len(parts) - 1 else None, disable_web_page_preview=True)
        try:
            await bot.send_message(chat_id, to_html(part), parse_mode=ParseMode.HTML, **kwargs)
        except BadRequest:  # e.g. a tag split across two parts: send it plain instead
            await bot.send_message(chat_id, part, **kwargs)


async def send(update: Update, text: str, markup=None) -> None:
    await send_to(update.get_bot(), update.effective_chat.id, text, markup)


def reminder_buttons(reminders, tz) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        f"❌ {r.due_at.astimezone(tz):%a %d %b %H:%M} {r.text}"[:60], callback_data=f"rm:{r.id}")]
        for r in reminders[:20]])


DOCKER_FILTERS = [("🟢 Running", "running"), ("⏸ Paused", "paused"), ("⚪ Stopped", "stopped"),
                  ("🆕 Created", "created"), ("🐳 All", "all")]
DOCKER_VERBS = [("start", "▶️ Start"), ("unpause", "⏯ Resume"), ("restart", "🔁 Restart"), ("pause", "⏸ Pause"),
                ("stop", "⏹ Stop")]


def docker_list_buttons(containers) -> InlineKeyboardMarkup:
    """One button per container (two per row), then the state filters. callback: dk:p:<short id>."""
    buttons = [InlineKeyboardButton(f"{c.icon()} {c.name}"[:40], callback_data=f"dk:p:{c.id[:12]}")
               for c in containers[:40]]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    rows.append([InlineKeyboardButton(label, callback_data=f"dk:l:{state}") for label, state in DOCKER_FILTERS])
    return InlineKeyboardMarkup(rows)


def docker_panel(c, protected: set[str]) -> InlineKeyboardMarkup:
    """The actions that make sense for this container's state, plus logs and a way back."""
    verbs = [] if c.name in protected else [(v, label) for v, label in DOCKER_VERBS if c.state in homelab.VALID[v]]
    act = [InlineKeyboardButton(label, callback_data=f"dk:a:{v}:{c.id[:12]}") for v, label in verbs]
    rows = [act[i:i + 3] for i in range(0, len(act), 3)]
    rows.append([InlineKeyboardButton("📜 Logs", callback_data=f"dk:g:{c.id[:12]}"),
                 InlineKeyboardButton("🔄 Refresh", callback_data=f"dk:p:{c.id[:12]}"),
                 InlineKeyboardButton("⬅️ All", callback_data="dk:l:all")])
    return InlineKeyboardMarkup(rows)


def docker_line(c, protected: set[str]) -> str:
    lock = " 🔒" if c.name in protected else ""
    problem = f" ⚠️ {c.problem()}" if c.problem() else ""
    return f"{c.icon()} {c.name}{lock} ({c.project or 'no stack'}): {c.status}{problem}"


def memory_buttons(facts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 {text}"[:60], callback_data=f"mem:{fid}")]
                                 for fid, text in facts[:30]])


def csv(text: str, lower: bool = False) -> set[str]:
    return {s.strip().lower() if lower else s.strip() for s in text.split(",") if s.strip()}


def build_app(settings: Settings) -> Application:
    llm = LLM(settings.ollama_host, settings.ollama_model, settings.ollama_num_ctx,
              timeout=settings.ollama_timeout, keep_alive=settings.ollama_keep_alive)
    store = Store(settings.db_path)
    ctx = make_context(settings, store)
    cal = GoogleCalendar(ctx)
    servers = parse_servers(settings.mc_servers)
    docker = notion = postiz = monitor = None
    if settings.docker_proxy_url:
        docker = Docker(settings.docker_proxy_url)
    if settings.notion_token and settings.notion_tasks_db:
        notion = Notion(settings.notion_token, settings.notion_tasks_db)
    if settings.postiz_url and settings.postiz_api_key:
        postiz = Postiz(settings.postiz_url, settings.postiz_api_key)
    if settings.alerts:
        monitor = Monitor(store, docker, servers, mc_alert=csv(settings.mc_alert_servers, lower=True),
                          ignore=csv(settings.alert_ignore), llm=llm)
    agent = Agent(llm, store, default_registry(ctx, cal, docker, notion, postiz), ctx, settings.history_turns)
    agenda_time = settings.agenda_at
    help_text = HELP.format(model=settings.ollama_model, agenda_line=(
        f"\nEvery morning at {agenda_time:%H:%M} I send you the day's agenda." if agenda_time else ""))
    background: list[asyncio.Task] = []
    ears = Transcriber(settings.whisper_model, str(settings.data_dir / "whisper"), settings.whisper_language)
    me = filters.User(user_id=settings.allowed_user_id)

    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(update, "👋 Hi! I'm Nhako.\n\n" + help_text)

    async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(update, help_text)

    async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
        brain = await llm.health()
        calendar = settings.google_token_path.exists()
        await send(update, "\n".join([
            f"🧠 {settings.ollama_model}: " + ("ok" if brain is None else f"❌ {brain}"),
            "📅 Google Calendar: " + ("token present" if calendar else "❌ not set up (scripts/google_auth.py)"),
            "📝 Notion: " + await notion_check(),
            "📣 Postiz: " + await check(postiz and postiz.channels(), "POSTIZ_URL / POSTIZ_API_KEY"),
        ]))

    async def notion_check() -> str:
        result = await check(notion and notion.project_schema(), "NOTION_TOKEN / NOTION_TASKS_DB")
        if result == "ok":
            return "ok (tasks + projects)" if await notion.project_schema() else "ok (tasks only)"
        return result

    async def check(call, env: str) -> str:
        if call is None:
            return f"not set up ({env})"
        try:
            await call
            return "ok"
        except Exception as e:
            log.warning("ping check failed: %r", e)
            return f"❌ {type(e).__name__}"

    async def morning() -> str:
        text = await agenda(cal, ctx, "today")
        if notion is not None:
            if tasks := await morning_tasks(notion, ctx.now()):  # overdue, today, next days, in progress
                text += "\n\n" + tasks
        return text

    async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(update, await tasks_report(notion, ctx.now(), " ".join(context.args)))

    async def projects_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(update, await projects_report(notion, " ".join(context.args)))

    async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if notion is None:
            await send(update, await tasks_report(None, ctx.now()))  # says how to set Notion up
            return
        try:
            await send(update, await stats_text(notion, ctx.now(), " ".join(context.args)))
        except Exception:
            log.exception("stats failed")
            await send(update, "⚠️ I couldn't read your Notion stats.")

    jobs: list[scheduler.Job] = []
    if notion is not None and (review := settings.weekly_review_at):
        jobs.append(scheduler.Job("weekly_review_sent", review[1], lambda: weekly_review(notion, ctx.now()),
                                  weekday=review[0]))
    if notion is not None and (nudge := settings.overdue_nudge_at):
        jobs.append(scheduler.Job("nudge_sent", nudge, lambda: overdue_nudge(notion, ctx.now(), store)))
    if postiz is not None and (review := settings.weekly_review_at):
        jobs.append(scheduler.Job("postiz_digest_sent", review[1], lambda: weekly_digest(postiz, ctx.now()),
                                  weekday=review[0]))
    watchers: list[scheduler.Watcher] = []
    if postiz is not None and settings.postiz_tiktok_reminders:
        watchers.append(scheduler.Watcher(1, TikTokWatch(postiz, store).check))  # every tick; fetches every 5 min

    async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(update, await agenda(cal, ctx, " ".join(context.args) or "today"))

    async def mc(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(update, await mc_report(servers, " ".join(context.args)) if servers
                   else "No Minecraft servers configured (MC_SERVERS).")

    protected = homelab.protected_names(settings)

    async def docker_view(state: str):
        """-> (text, buttons) for /docker running|paused|stopped|created|all."""
        containers = await docker.containers()
        if state == "all":
            return homelab.stack_summary(containers), docker_list_buttons(containers)
        hits = homelab.filter_containers(containers, state)
        return (f"🐳 {len(hits)} {state}:" if hits else f"🐳 No {state} containers."), docker_list_buttons(hits)

    async def docker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if docker is None:
            return await send(update, "Docker status isn't set up (DOCKER_PROXY_URL).")
        wanted = " ".join(context.args)
        try:
            if wanted.lower() in homelab.STATE_FILTERS or wanted.lower() == "all":
                text, buttons = await docker_view(wanted.lower())
                return await send(update, text, buttons)
            if not wanted:
                text = homelab.stack_summary(await docker.containers()) + "\n\nTap a filter to pick a container:"
                return await send(update, text, docker_list_buttons([]))
            hits = homelab.match_containers(await docker.containers(), wanted)
            await send(update, await docker_report(docker, wanted), docker_list_buttons(hits) if hits else None)
        except Exception as e:
            log.warning("docker proxy failed: %r", e)
            await send(update, "I couldn't reach Docker (is nhako-docker-proxy running?).")

    async def logs_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if docker is None:
            return await send(update, "Docker isn't set up (DOCKER_PROXY_URL).")
        if not context.args:
            return await send(update, "Which container? e.g. /logs nextcloud 50")
        lines = int(context.args[-1]) if len(context.args) > 1 and context.args[-1].isdigit() else homelab.LOG_LINES
        name = " ".join(a for a in context.args if not (a.isdigit() and len(context.args) > 1))
        try:
            hits = homelab.resolve(await docker.containers(), name)
            if len(hits) != 1:
                return await send(update, f"Logs of which one? {', '.join(c.name for c in hits)}" if hits
                                  else f"No container called “{name}”. Try /docker all")
            await send(update, homelab.logs_text(hits[0].name, await docker.logs(hits[0], min(lines, 200))))
        except Exception as e:
            log.warning("docker logs failed: %r", e)
            await send(update, "I couldn't read that log (is nhako-docker-proxy running?).")

    async def on_docker(update: Update, context: ContextTypes.DEFAULT_TYPE):
        """dk:l:<state> list, dk:p:<id> panel, dk:a:<verb>:<id> act (or ask), dk:y:<verb>:<id> confirmed,
        dk:g:<id> logs."""
        query = update.callback_query
        await query.answer()
        if query.from_user.id != settings.allowed_user_id or docker is None:
            return
        _, kind, *rest = query.data.split(":")
        try:
            if kind == "l":
                text, buttons = await docker_view(rest[0])
                return await query.edit_message_text(text, reply_markup=buttons)
            c = next((c for c in await docker.containers() if c.id.startswith(rest[-1])), None)
            if c is None:
                return await query.edit_message_text("That container is gone.", reply_markup=docker_list_buttons([]))
            if kind == "g":
                return await send(update, homelab.logs_text(c.name, await docker.logs(c)))
            if kind == "a" and rest[0] in homelab.CONFIRM:
                verb = rest[0]
                return await query.edit_message_text(
                    f"{docker_line(c, protected)}\n\n{verb.capitalize()} {c.name}?",
                    reply_markup=InlineKeyboardMarkup([[
                        InlineKeyboardButton(f"✅ Yes, {verb}", callback_data=f"dk:y:{verb}:{c.id[:12]}"),
                        InlineKeyboardButton("↩️ No", callback_data=f"dk:p:{c.id[:12]}")]]))
            note = ""
            if kind in ("a", "y"):
                ok, skipped = homelab.check_targets(rest[0], [c], protected)
                note = await homelab.run_action(docker, rest[0], ok) if ok else "\n".join(skipped)
                c = next((x for x in await docker.containers() if x.id == c.id), c)
            text = docker_line(c, protected) + (f"\n\n{note}" if note else "")
            await query.edit_message_text(text, reply_markup=docker_panel(c, protected))
        except BadRequest as e:  # "message is not modified" after a Refresh with nothing new
            log.debug("docker panel: %s", e)
        except httpx.HTTPError as e:
            log.warning("docker button failed: %r", e)
            await send(update, "I couldn't reach Docker (is nhako-docker-proxy running?).")

    async def reminders(update: Update, context: ContextTypes.DEFAULT_TYPE):
        upcoming = await store.upcoming_reminders(update.effective_chat.id)
        if not upcoming:
            return await send(update, "No reminders set. Try “remind me in 20 minutes to stretch”.")
        await send(update, f"⏰ {len(upcoming)} reminder(s). Tap one to cancel it.",
                   reminder_buttons(upcoming, settings.tz))

    async def on_cancel_reminder(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if query.from_user.id != settings.allowed_user_id:
            return
        cancelled = await store.cancel_reminder(int(query.data.partition(":")[2]), update.effective_chat.id)
        upcoming = await store.upcoming_reminders(update.effective_chat.id)
        note = f"Cancelled: {cancelled.text}" if cancelled else "That reminder was already sent or cancelled."
        await query.edit_message_text(f"{note}\n\n⏰ {len(upcoming)} reminder(s) left.",
                                      reply_markup=reminder_buttons(upcoming, settings.tz) if upcoming else None)

    async def memory_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        facts = await store.facts(update.effective_chat.id)
        if not facts:
            return await send(update, "I don't have anything saved about you yet. Say “remember that …”.")
        await send(update, f"🧠 I remember {len(facts)} thing(s). Tap one to forget it.", memory_buttons(facts))

    async def on_forget_fact(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if query.from_user.id != settings.allowed_user_id:
            return
        gone = await store.delete_fact(int(query.data.partition(":")[2]), update.effective_chat.id)
        facts = await store.facts(update.effective_chat.id)
        note = f"Forgotten: {gone}" if gone else "That was already forgotten."
        await query.edit_message_text(f"{note}\n\n🧠 {len(facts)} thing(s) left.",
                                      reply_markup=memory_buttons(facts) if facts else None)

    async def on_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
        msg = update.effective_message
        async with typing(update, context):
            if msg.voice:
                buf = io.BytesIO()
                await (await msg.voice.get_file()).download_to_memory(buf)
                buf.seek(0)
                try:
                    text = await ears.transcribe(buf)
                except Exception:
                    log.exception("voice transcription failed")
                    return await send(update, "⚠️ I couldn't open that voice note. Try again?")
                if not text:
                    return await send(update, "🎙️ I couldn't make out any words in that voice note.")
                await send(update, f"🎙️ “{text}”")
            else:
                text = msg.text
            try:
                reply = await agent.handle(update.effective_chat.id, text)
            except httpx.TimeoutException:
                log.warning("model timed out after %ss on %r", settings.ollama_timeout, text[:80])
                return await send(update, "⌛ That took too long. Try again?")
            except Exception:
                log.exception("handling %r failed", text[:80])
                return await send(update, "⚠️ I couldn't think just now. Try again, or /ping.")
        await send(update, reply.text, confirm_buttons(reply.confirm_id) if reply.confirm_id else None)

    async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        if query.from_user.id != settings.allowed_user_id:
            return
        verb, _, action_id = query.data.partition(":")
        result = await agent.confirm(update.effective_chat.id, int(action_id), accepted=verb == "ok")
        # Replace the buttons with the outcome so they can't be pressed twice.
        await query.edit_message_text(f"{query.message.text}\n\n{result}"[:TELEGRAM_LIMIT])

    async def denied(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user = update.effective_user
        log.warning("ignored message from unauthorised user %s (%s)", user.id if user else "?",
                    user.username if user else "?")

    async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
        """Last resort: anything a handler didn't catch is logged AND reported, never silent."""
        log.error("unhandled error", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            with contextlib.suppress(Exception):
                await send(update, "⚠️ Something went wrong. Try again in a bit.")

    async def post_init(app: Application):
        await store.open()
        with contextlib.suppress(Exception):  # the menu is a nicety; never block startup on it
            await app.bot.set_my_commands(COMMANDS)
        if problem := await llm.health():
            log.warning("Ollama check failed at startup: %s", problem)
        else:
            background.append(asyncio.create_task(llm.warm()))
        background.append(asyncio.create_task(scheduler.run(
            store, lambda chat_id, text: send_to(app.bot, chat_id, text), ctx.now,
            agenda_time=agenda_time, agenda_chat=settings.allowed_user_id, agenda=morning,
            warm=llm.warm, history_keep=settings.history_keep, heartbeat=settings.data_dir / "heartbeat",
            monitor=monitor, alert_chat=settings.allowed_user_id, jobs=jobs, watchers=watchers)))

    async def post_shutdown(app: Application):
        for task in background:
            task.cancel()
        await asyncio.gather(*background, return_exceptions=True)
        for client in (docker, notion, postiz):
            if client is not None:
                await client.aclose()
        await store.close()

    # Concurrent updates: /today or a button tap answers at once even while the model is busy
    # with a long web summary. Model calls still queue one at a time inside LLM.
    app = (ApplicationBuilder().token(settings.telegram_token).concurrent_updates(True)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start, filters=me))
    app.add_handler(CommandHandler("help", help_cmd, filters=me))
    app.add_handler(CommandHandler("ping", ping, filters=me))
    app.add_handler(CommandHandler("today", today, filters=me))
    app.add_handler(CommandHandler("reminders", reminders, filters=me))
    app.add_handler(CommandHandler("mc", mc, filters=me))
    app.add_handler(CommandHandler("docker", docker_cmd, filters=me))
    app.add_handler(CommandHandler("logs", logs_cmd, filters=me))
    app.add_handler(CommandHandler("tasks", tasks_cmd, filters=me))
    app.add_handler(CommandHandler("projects", projects_cmd, filters=me))
    app.add_handler(CommandHandler("stats", stats_cmd, filters=me))
    app.add_handler(CommandHandler("memory", memory_cmd, filters=me))
    app.add_handler(MessageHandler(me & (filters.TEXT | filters.VOICE) & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(ok|no):\d+$"))
    app.add_handler(CallbackQueryHandler(on_cancel_reminder, pattern=r"^rm:\d+$"))
    app.add_handler(CallbackQueryHandler(on_forget_fact, pattern=r"^mem:\d+$"))
    app.add_handler(CallbackQueryHandler(on_docker, pattern=r"^dk:[lpayg]:"))
    app.add_handler(MessageHandler(~me, denied))
    app.add_error_handler(on_error)
    return app
