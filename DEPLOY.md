# Deployment Guide

Step-by-step procedure to take this repo from zero to a running Telegram → Claude Code bridge on a Mac. Follow top-to-bottom; each section assumes the previous one succeeded.

---

## 0. What you're deploying

A single Python process (`bot.py`) that long-polls Telegram and forwards messages to a `claude` CLI process running inside a persistent `tmux` session. The bot process and the Claude process have **independent lifecycles** — restarting the bot does not lose Claude's conversation; restarting the tmux session does.

Target host: **your Mac, logged in as your user.** Not a server, not a VM.

---

## 1. Prerequisites

Install once; versions below are minimums.

| Tool | Minimum | Install |
|---|---|---|
| macOS | 13 (Ventura) | — |
| Homebrew | current | https://brew.sh |
| Python | 3.11 | `brew install python@3.11` |
| tmux | 3.3 | `brew install tmux` |
| git | any | `brew install git` |
| `claude` CLI | latest | `npm install -g @anthropic-ai/claude-code` (requires Node 18+) |

Verify:

```bash
python3 --version    # >= 3.11
tmux -V              # >= 3.3
claude --version     # any
```

If `claude` is not on `PATH`, fix that before continuing — the bot will fail to start the session otherwise.

---

## 2. Telegram setup

### 2a. Enable two-step verification on your Telegram account (required first)

The bot's only authorization check is that the sender's chat ID matches `ALLOWED_CHAT_ID`. If your Telegram account is taken over (SIM-swap, session hijack), the attacker passes that check and gets full access to your Mac through Claude. Two-step verification is the only thing that stops this.

**Before creating the bot:**

1. Telegram → Settings → Privacy and Security → Two-Step Verification.
2. Set a strong password and a recovery email.
3. Confirm the recovery email link arrives and works.

Do not proceed until this is active.

### 2b. Create the bot

1. Open Telegram, message [@BotFather](https://t.me/BotFather).
2. Send `/newbot`.
3. Pick a display name (e.g. `My Claude Bridge`) and a non-descriptive username with random suffixes (e.g. `cb_a3f9e2_bot`). Do not use an obvious name like `claudebridge_bot` — the username is publicly searchable.
4. BotFather replies with an HTTP API token like `1234567890:ABC-DEF...`. **Save it.** This is `TELEGRAM_BOT_TOKEN`.
5. **Required:** send `/setprivacy` → choose your bot → `Enable`. Prevents the bot from reading all messages in groups it is added to.
6. **Required:** send `/setjoingroups` → `Disable`. Prevents the bot from being added to groups at all.

### 2c. Get your chat ID

Do not send your identity to a third-party bot. Use the Telegram API directly:

1. Search for your bot's username in Telegram and send it `/start`. The bot won't respond yet, but this creates the chat.
2. In a terminal (after completing §4 Configure and setting `TELEGRAM_BOT_TOKEN` in `.env`):
   ```bash
   source .venv/bin/activate
   python3 -c "
   import os, urllib.request, json
   from dotenv import load_dotenv
   load_dotenv()
   token = os.environ['TELEGRAM_BOT_TOKEN']
   data = json.loads(urllib.request.urlopen(
       f'https://api.telegram.org/bot{token}/getUpdates'
   ).read())
   for u in data.get('result', []):
       c = u.get('message', {}).get('chat', {})
       print(c.get('id'), c.get('type'), c.get('username', c.get('first_name')))
   "
   ```
3. The number beside your username is `ALLOWED_CHAT_ID`. Set it in `.env`.

---

## 3. Install the bot

```bash
mkdir -p ~/code && cd ~/code
git clone https://github.com/kreig133/Claude-Telegram claude-bridge
cd claude-bridge
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Verify:

```bash
python -c "import telegram, dotenv; print('ok')"
```

---

## 4. Configure

```bash
cp .env.example .env
```

Edit `.env` with your real values. Set `TELEGRAM_BOT_TOKEN` first — you need it to run the `getUpdates` snippet in §2c that gives you `ALLOWED_CHAT_ID`.

```
TELEGRAM_BOT_TOKEN=1234567890:ABC-DEF-your-token-here
ALLOWED_CHAT_ID=987654321
TMUX_SESSION=claude-bridge
PROJECT_DIR=/Users/YOU/projects/sandbox
OUTPUT_STABLE_SECONDS=3
POLL_INTERVAL_SECONDS=1.0
```

**Critical choice: `PROJECT_DIR`.** The Claude session runs with `--dangerously-skip-permissions`, so any message you send can read/write/delete anything under this directory. Rules:

- Point it at a **git-tracked** directory so you can `git reset` any damage.
- Do **not** point it at `$HOME`, `~/Documents`, or anything containing credentials, SSH keys, or browser data.
- Create the directory first: `mkdir -p ~/projects/sandbox && cd ~/projects/sandbox && git init`.

Lock down the file:

```bash
chmod 600 .env
```

---

## 5. First run (foreground)

```bash
cd ~/code/claude-bridge
source .venv/bin/activate
python bot.py
```

Expected stderr output:

```
[... INFO] tmux has-session -t claude-bridge
[... INFO] tmux new-session -d -s claude-bridge -c /Users/YOU/projects/sandbox claude --dangerously-skip-permissions
[... INFO] tmux capture-pane -t claude-bridge -p -S -60
[... INFO] bot ready; session=claude-bridge project=/Users/YOU/projects/sandbox
```

Within ~5 seconds your Telegram chat should receive a `<pre>`-wrapped banner showing Claude Code's startup screen. That's the readiness signal.

If it doesn't arrive, see **Troubleshooting** below before continuing.

---

## 6. Smoke test (acceptance criteria)

From your phone, verify each in order:

1. Send `hello`. → Claude greeting returns within ~10 s.
2. Send `read SPEC.md and summarize it in three bullets`. → Summary returns within ~60 s.
3. Send `write a 300-line poem about ferrets`. → Reply splits across multiple Telegram messages; none truncated.
4. While Claude is generating a long reply, send `/cancel`. → Generation stops; bot replies `Sent Ctrl-C.`.
5. Send `/restart`. → Reply `Session restarted.`; fresh Claude banner arrives.
6. Send a PNG as a photo. → Bot replies with a path like `/Users/YOU/projects/sandbox/uploads/20260418_140312_photo_AgAD...jpg`. Confirm the file exists and has non-zero size.
7. From a different Telegram account (friend's phone, test account), send `hi` to your bot. → Bot gives no reply. Check bot stderr: `dropped message from disallowed chat <id>`.
8. Kill the bot with Ctrl-C. Restart with `python bot.py`. → No new banner. Send `hello again` → Claude replies *with context from earlier*; conversation is intact.

All eight must pass before deploying persistently.

---

## 7. Run persistently

Pick **one** of A or B. A is simpler; B survives reboots.

### 7a. Option A — tmux (simpler)

```bash
tmux new -d -s bridge-bot \
  "cd ~/code/claude-bridge && source .venv/bin/activate && python bot.py"
```

Inspect logs:

```bash
tmux attach -t bridge-bot      # Ctrl-b d to detach
```

Stop the bot:

```bash
tmux kill-session -t bridge-bot
```

Note: does **not** survive reboot. After a reboot, re-run the `tmux new` command.

### 7b. Option B — launchd (survives reboot)

Create `~/Library/LaunchAgents/com.you.claude-bridge.plist`. Replace `YOU` with your macOS short username:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.you.claude-bridge</string>

  <key>ProgramArguments</key>
  <array>
    <string>/Users/YOU/code/claude-bridge/.venv/bin/python</string>
    <string>/Users/YOU/code/claude-bridge/bot.py</string>
  </array>

  <key>WorkingDirectory</key>
  <string>/Users/YOU/code/claude-bridge</string>

  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>

  <key>RunAtLoad</key>
  <true/>

  <key>KeepAlive</key>
  <true/>

  <key>StandardOutPath</key>
  <string>/Users/YOU/code/claude-bridge/bot.log</string>

  <key>StandardErrorPath</key>
  <string>/Users/YOU/code/claude-bridge/bot.log</string>
</dict>
</plist>
```

Load and start:

```bash
launchctl load  ~/Library/LaunchAgents/com.you.claude-bridge.plist
launchctl start com.you.claude-bridge
```

Verify:

```bash
launchctl list | grep claude-bridge   # PID should be non-zero
tail -f ~/code/claude-bridge/bot.log
```

Stop / disable:

```bash
launchctl unload ~/Library/LaunchAgents/com.you.claude-bridge.plist
```

Caveats:

- `claude --dangerously-skip-permissions` inside tmux needs access to the npm global bin. The `PATH` entry above covers Apple Silicon (`/opt/homebrew/bin`) and Intel (`/usr/local/bin`). If `claude` is installed via a Node version manager (nvm, volta, asdf), hardcode its absolute directory into the `PATH` too — launchd does **not** source `~/.zshrc`.
- launchd starts the bot on user login, not at boot. That's fine for a personal laptop.
- `bot.log` rotates nowhere. Truncate manually if it grows: `: > ~/code/claude-bridge/bot.log`.

---

## 8. Post-deployment verification

Open a terminal on the Mac and from your phone send a message. Confirm, in order:

```bash
# 1. Bot process exists
pgrep -fl "python.*bot.py"

# 2. tmux sessions exist
tmux ls
#   claude-bridge: ...   ← Claude itself
#   bridge-bot:    ...   ← bot process (only if using Option 7a)

# 3. Bot is logging
tail -n 20 ~/code/claude-bridge/bot.log   # Option B
# or: tmux attach -t bridge-bot            # Option A

# 4. The Claude pane is alive
tmux attach -t claude-bridge   # Ctrl-b d to detach without killing
```

If all four check out, deployment is complete.

---

## 9. Day-two operations

**Send Claude into a different project.** Stop the bot, change `PROJECT_DIR` in `.env`, `tmux kill-session -t claude-bridge`, restart the bot. The bot will create a fresh Claude session in the new directory.

**Reset Claude's context without losing the session.** Send `/clear` from Telegram.

**Kill a stuck generation.** Send `/cancel` (sends Ctrl-C). If that fails, `/restart` recreates the whole tmux session.

**Rotate the bot token.** BotFather → `/revoke` → pick bot → new token. Update `.env`. Restart the bot.

**Update the bot code.**

```bash
cd ~/code/claude-bridge
git pull
source .venv/bin/activate
pip install -r requirements.txt   # in case deps changed
# then restart per your deployment option
```

A bot restart does **not** drop the Claude session — conversation survives.

**Update Claude Code.**

```bash
npm update -g @anthropic-ai/claude-code
# then from Telegram:
/restart
```

**Inspect what's been uploaded.**

```bash
ls -lh "$PROJECT_DIR/uploads/"
```

These accumulate forever. Prune manually.

---

## 10. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Bot exits immediately with `TELEGRAM_BOT_TOKEN is required` | `.env` missing or unreadable | `ls -la .env`; `chmod 600 .env` |
| Bot exits with `ALLOWED_CHAT_ID must be an integer` | Quoted or malformed value in `.env` | Use a bare number: `ALLOWED_CHAT_ID=987654321` |
| Startup banner never arrives | `claude` not on `PATH` for the bot's shell (esp. launchd) | Hardcode the full `claude` install directory into the plist's `PATH` |
| `⚠️ tmux error: tmux binary not found on PATH` | tmux not installed or launchd `PATH` wrong | `brew install tmux`; fix plist `PATH` |
| `⏳ (no new output — Claude may still be thinking...)` | Output didn't stabilize within `OUTPUT_STABLE_SECONDS` because Claude is still streaming | Send `/status` to peek; or bump `OUTPUT_STABLE_SECONDS` for verbose sessions |
| `⏰ (timed out waiting for stable output...)` | Hit the 5-minute hard cap | Reply contains whatever was captured. Send another message to continue; or `/cancel` and retry |
| Bot doesn't reply to you but logs show `dropped message from disallowed chat <id>` | `ALLOWED_CHAT_ID` doesn't match your Telegram ID | Re-run the `getUpdates` snippet from §2c; update `.env`; restart |
| Reply contains garbled box characters or mojibake | ANSI stripping didn't catch a rare sequence | Report an issue with a sample; workaround is `/clear` to reset the UI state |
| Reply formatting is broken with `Bad Request: can't parse entities` | A message somehow contains unescaped HTML that slipped past `html.escape` | Should not happen; file a bug with the triggering input |
| Multiple bot processes running | Forgot to stop the old one before starting new | `pgrep -fl bot.py`; kill duplicates; Telegram rejects concurrent long-polls with `Conflict: terminated by other getUpdates request` — visible in log |

---

## 11. Uninstall

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.you.claude-bridge.plist 2>/dev/null
tmux kill-session -t bridge-bot 2>/dev/null
tmux kill-session -t claude-bridge 2>/dev/null

# Remove launchd unit
rm -f ~/Library/LaunchAgents/com.you.claude-bridge.plist

# Remove code and venv
rm -rf ~/code/claude-bridge

# Revoke the Telegram token via BotFather /revoke if you're done with it
```

Uploaded files under `PROJECT_DIR/uploads/` are **not** removed automatically — delete if desired.

---

## 12. Deployment checklist

Copy, tick as you go:

- [ ] Telegram two-step verification enabled (§2a)
- [ ] `python3 --version` ≥ 3.11
- [ ] `tmux -V` ≥ 3.3
- [ ] `claude --version` works
- [ ] Bot created with @BotFather; non-descriptive username chosen; token saved
- [ ] `/setprivacy Enable` and `/setjoingroups Disable` applied in BotFather
- [ ] `ALLOWED_CHAT_ID` obtained via `getUpdates` (no third-party bot)
- [ ] Repo cloned over HTTPS; venv created; `pip install -r requirements.txt` succeeded
- [ ] `.env` populated; `chmod 600 .env` applied
- [ ] `PROJECT_DIR` points at a git-tracked sandbox, not `$HOME`
- [ ] First foreground run posts a banner to Telegram
- [ ] All eight smoke-test criteria pass
- [ ] Persistent deployment configured (tmux or launchd)
- [ ] Post-deployment verification (section 8) passes
- [ ] `.env` is **not** committed — `git status` shows it ignored
