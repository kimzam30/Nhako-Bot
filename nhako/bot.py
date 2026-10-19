"""Telegram side: auth, commands, typing indicator, confirm buttons, long-message splitting."""
import asyncio
import contextlib
import html
import io
import logging
import re

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest
from telegram.ext import (Application, ApplicationBuilder, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from . import scheduler
from .agent import Agent, make_context
from .config import Settings
from .llm import LLM
from .speech import Transcriber
from .store import Store
from .tools import default_registry
from .monitor import Monitor
from .tools.calendar import GoogleCalendar, agenda
from .tools.homelab import Docker, docker_report, mc_report, parse_servers
from .tools.notion import Notion, tasks_report
from .tools.postiz import Postiz

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
• "remember that I prefer meetings after 10am" / "what do you know about me?"
• "what's going out on social media this week?" / "draft a tweet about the server wipe" (saved as a draft)

/today [tomorrow, this week, ...] lists your calendar instantly
/reminders shows reminders you can cancel
/mc [server] shows who's online
/docker [name] shows your containers
/tasks [today, this week, overdue] lists your open Notion tasks
/memory shows what I remember about you, tap one to forget it
/ping checks that my brain and calendar are reachable
{agenda_line}"""


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


def memory_buttons(facts) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton(f"🗑 {text}"[:60], callback_data=f"mem:{fid}")]
                                 for fid, text in facts[:30]])


def build_app(settings: Settings) -> Application:
    llm = LLM(settings.ollama_host, settings.ollama_model, settings.ollama_num_ctx)
    store = Store(settings.db_path)
    ctx = make_context(settings, store)
    cal = GoogleCalendar(ctx)
    docker = Docker(settings.docker_proxy_url) if settings.docker_proxy_url else None
    servers = parse_servers(settings.mc_servers)
    notion = Notion(settings.notion_token, settings.notion_tasks_db)         if settings.notion_token and settings.notion_tasks_db else None
    postiz = Postiz(settings.postiz_url, settings.postiz_api_key)         if settings.postiz_url and settings.postiz_api_key else None
    agent = Agent(llm, store, default_registry(ctx, cal, docker, notion, postiz), ctx, settings.history_turns)
    monitor = Monitor(store, docker, servers,
                      mc_alert={s.strip().lower() for s in settings.mc_alert_servers.split(",") if s.strip()},
                      ignore={s.strip() for s in settings.alert_ignore.split(",") if s.strip()}) \
        if settings.alerts else None
    agenda_time = scheduler.parse_agenda_time(settings.agenda_time)
    help_text = HELP.format(model=settings.ollama_model, agenda_line=(
        f"\nEvery morning at {agenda_time:%H:%M} I send you the day's agenda." if agenda_time else ""))
    background: list[asyncio.Task] = []
    ears = Transcriber(settings.whisper_model, str(settings.data_dir / "whisper"))
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
            "📝 Notion: " + await check(notion and notion.schema(), "NOTION_TOKEN / NOTION_TASKS_DB"),
            "📣 Postiz: " + await check(postiz and postiz.channels(), "POSTIZ_URL / POSTIZ_API_KEY"),
        ]))

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
            tasks = await tasks_report(notion, ctx.now(), "today")  # due today, overdue included
            if not tasks.startswith("✅"):
                text += "\n\n" + tasks
        return text

    async def tasks_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(update, await tasks_report(notion, ctx.now(), " ".join(context.args)))

    async def today(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(update, await agenda(cal, ctx, " ".join(context.args) or "today"))

    async def mc(update: Update, context: ContextTypes.DEFAULT_TYPE):
        await send(update, await mc_report(servers, " ".join(context.args)) if servers
                   else "No Minecraft servers configured (MC_SERVERS).")

    async def docker_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if docker is None:
            return await send(update, "Docker status isn't set up (DOCKER_PROXY_URL).")
        try:
            await send(update, await docker_report(docker, " ".join(context.args)))
        except Exception as e:
            log.warning("docker proxy failed: %r", e)
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
                    return await send(update, "⚠️ I couldn't process that voice note. Details are in the log.")
                if not text:
                    return await send(update, "🎙️ I couldn't make out any words in that voice note.")
                await send(update, f"🎙️ “{text}”")
            else:
                text = msg.text
            try:
                reply = await agent.handle(update.effective_chat.id, text)
            except Exception:
                log.exception("handling %r failed", text[:80])
                return await send(update, "⚠️ My local brain didn't answer. Is Ollama running? Try /ping.")
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
                await send(update, "⚠️ Something broke while handling that. Details are in the log.")

    async def post_init(app: Application):
        await store.open()
        if problem := await llm.health():
            log.warning("Ollama check failed at startup: %s", problem)
        background.append(asyncio.create_task(scheduler.run(
            store, lambda chat_id, text: send_to(app.bot, chat_id, text), ctx.now,
            agenda_time=agenda_time, agenda_chat=settings.allowed_user_id,
            agenda=morning,
            monitor=monitor, alert_chat=settings.allowed_user_id)))

    async def post_shutdown(app: Application):
        for task in background:
            task.cancel()
        await store.close()

    app = (ApplicationBuilder().token(settings.telegram_token)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start, filters=me))
    app.add_handler(CommandHandler("help", help_cmd, filters=me))
    app.add_handler(CommandHandler("ping", ping, filters=me))
    app.add_handler(CommandHandler("today", today, filters=me))
    app.add_handler(CommandHandler("reminders", reminders, filters=me))
    app.add_handler(CommandHandler("mc", mc, filters=me))
    app.add_handler(CommandHandler("docker", docker_cmd, filters=me))
    app.add_handler(CommandHandler("tasks", tasks_cmd, filters=me))
    app.add_handler(CommandHandler("memory", memory_cmd, filters=me))
    app.add_handler(MessageHandler(me & (filters.TEXT | filters.VOICE) & ~filters.COMMAND, on_message))
    app.add_handler(CallbackQueryHandler(on_button, pattern=r"^(ok|no):\d+$"))
    app.add_handler(CallbackQueryHandler(on_cancel_reminder, pattern=r"^rm:\d+$"))
    app.add_handler(CallbackQueryHandler(on_forget_fact, pattern=r"^mem:\d+$"))
    app.add_handler(MessageHandler(~me, denied))
    app.add_error_handler(on_error)
    return app
