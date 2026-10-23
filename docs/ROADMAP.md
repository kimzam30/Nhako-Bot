# Roadmap: Nhako as the hub of the homelab

Everything below fits the patterns the bot already has: a **tool** (answers or asks for a Confirm tap),
a **watcher** (polled on the scheduler tick, pings when something changes), a **job** (daily/weekly
message), or a **command with buttons**. Ranked by how much it gives for how much work, given what
runs on kimmain today (Oct 2026: n8n, Nextcloud, Postiz, Beszel, Portainer, Valheim, Minecraft,
RustDesk, Homepage, mcweb, the backup containers).

## 1. Homelab operations
- **Backup watch.** `volume-backup`, `n8n-db-backup`, `postiz-db-backup`, `kimzam-backup`: read the
  newest file date (or the container's last log line) and ping when a backup is older than its
  schedule. "when was the last backup?" answers per stack. (`kimzam-backup` exited 255 on 23 Oct.)
- **Host health from Beszel.** CPU, RAM, disk and temperatures through Beszel's API; alert at
  disk > 85 %, and "how's the server?" answers in one line.
- **Stack updates.** "update postiz" = `docker compose pull && up -d` for one stack. The socket proxy
  can't (and shouldn't) do this, so it needs a tiny host-side helper (a systemd unit or an n8n
  workflow) that the bot calls with a fixed list of allowed stacks, always behind a Confirm tap.
- **Public uptime and certificates.** Ping the public URLs (postiz.nhako.com answered 502 twice on
  23 Oct) and warn 14 days before a TLS certificate expires.

## 2. Game servers
- Start and stop Minecraft and Valheim by name now works through Docker control. Next:
  **RCON** for `say`, `whitelist add`, `list`, `save-all`; **auto-stop** an empty server after N
  minutes to free RAM, and "start getgud" warming it back up.
- Daily "who played today" from the Minecraft player sample the monitor already collects.

## 3. n8n bridge (makes everything else cheap)
- One `run_workflow(name)` tool calling n8n webhook URLs from a small allowlist in `.env`:
  every n8n automation becomes a chat command ("run the invoice workflow").
- An inbound endpoint (or n8n's Telegram node with the same chat) so n8n can push results to you
  through Nhako's voice.

## 4. Nextcloud
- "save this to Nextcloud": forwarded files, photos and voice notes go to a WebDAV folder.
- "find the invoice from September" searches file names.

## 5. Postiz, beyond reminders
- Failed posts: Instagram had six ERROR posts this week. A ping when a post fails (any network, not
  just TikTok) and a "retry" button.
- Approve drafts from Telegram: list drafts with buttons that schedule them.
- Best time to post, from the stats the bot already reads.

## 6. One morning briefing
Agenda + today's TikTok post times + overdue Notion tasks + homelab problems + backup ages, in one
message instead of several.

## 7. Later
Email digest, spending notes and habit tracking in Notion databases, home automation if Home
Assistant joins the stack.
