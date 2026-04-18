# Claude Code Mobile Bridge

Telegram bot that bridges messages from your phone to a long-running `claude` CLI session running in `tmux` on your Mac.

```
[Telegram on phone] ⇅ Telegram Bot API ⇅ bot.py ⇅ tmux("claude-bridge") ⇅ claude CLI
```

Single operator, single machine, single session. Read `SPEC.md` for the design rationale.

## Requirements

- Python 3.11+
- `tmux` on `PATH`
- `claude` CLI (Claude Code) on `PATH`
- A Telegram bot token from [@BotFather](https://t.me/BotFather)
- Your Telegram numeric chat ID (send any message to [@userinfobot](https://t.me/userinfobot) to get it)

## Setup

```bash
git clone <this-repo> claude-bridge
cd claude-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env`:

- `TELEGRAM_BOT_TOKEN` — from BotFather
- `ALLOWED_CHAT_ID` — your Telegram user ID (integer). The bot ignores every other chat.
- `TMUX_SESSION` — tmux session name (default `claude-bridge`)
- `PROJECT_DIR` — directory the Claude session runs in; uploads land in `<PROJECT_DIR>/uploads/`
- `OUTPUT_STABLE_SECONDS` — seconds of unchanged pane output before the bot sends a reply (default 3)
- `POLL_INTERVAL_SECONDS` — pane poll cadence (default 1.0)

## Run

Foreground (dev):

```bash
python bot.py
```

Persistent:

```bash
tmux new -d -s bridge-bot 'cd ~/code/claude-bridge && source .venv/bin/activate && python bot.py'
```

On startup, if the target tmux session does not exist, the bot creates it and launches `claude --dangerously-skip-permissions` inside it, then posts the Claude banner to your chat as a readiness signal. If the session already exists, the bot attaches silently — you can restart `bot.py` without losing the Claude conversation.

## Commands

- *(plain text)* — send to Claude; reply comes back when output stabilizes
- `/status` — last 40 lines of the pane
- `/cancel` — send Ctrl-C to the Claude process
- `/restart` — kill and recreate the tmux session
- `/clear` — send `/clear` into Claude (resets Claude's context, keeps the session)
- `/raw <text>` — send `<text>` without pressing Enter (for multi-line input or special keys)

Send a document or photo and the bot saves it to `<PROJECT_DIR>/uploads/<timestamp>_<name>` and replies with the absolute path. It does **not** auto-inject the path into Claude — reference it in your next message.

## Security notes

- **Allowlist.** The bot silently ignores every chat except `ALLOWED_CHAT_ID`. Non-allowlisted messages get no reply (don't confirm the bot exists to strangers).
- **`--dangerously-skip-permissions`.** The tmux session runs Claude with permission prompts disabled, so any message you send can read, write, or delete files in `PROJECT_DIR`. Point the bot at a git-tracked, sandboxed project — not `$HOME`.
- **No shell execution surface.** The bot passes text to `tmux send-keys` as arguments (`shell=False`), and has no `/exec`-style command. An attacker with your bot token still can't run arbitrary shell outside the Claude session — but they can instruct Claude to do whatever Claude can do, which on `--dangerously-skip-permissions` is effectively everything in the project directory.
- **Token handling.** `.env` is gitignored. Never commit it. Revoke the token via BotFather if it leaks.
- **Logs.** Logs go to stderr only and never include message bodies.

## Troubleshooting

- `⚠️ tmux error: ...` — the session disappeared or tmux isn't installed. The bot tries one auto-restart; if that fails, run `tmux kill-server` and restart the bot.
- `⏳ (no new output ...)` — Claude is still thinking or the output hasn't stabilized inside `OUTPUT_STABLE_SECONDS`. Try `/status` to peek.
- `⏰ (timed out ...)` — hit the 5-minute hard cap; whatever was captured so far is sent.
- Bot replies are wrapped in `<pre>` blocks. Claude's markdown/code comes through verbatim; Telegram's markdown parser is not used.

## Files

```
bot.py             entire bot
requirements.txt   python-telegram-bot, python-dotenv
.env.example       config template
.gitignore         ignores .env, uploads/, venvs, caches
```
