#!/usr/bin/env python3
"""Telegram bot that bridges messages to a Claude Code tmux session."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
ALLOWED_CHAT_ID_RAW = os.getenv("ALLOWED_CHAT_ID")
TMUX_SESSION = os.getenv("TMUX_SESSION", "claude-bridge")
PROJECT_DIR = os.path.expanduser(os.getenv("PROJECT_DIR", "~/projects/qareen"))
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "default").strip() or "default"
OUTPUT_STABLE_SECONDS = float(os.getenv("OUTPUT_STABLE_SECONDS", "3"))
POLL_INTERVAL_SECONDS = float(os.getenv("POLL_INTERVAL_SECONDS", "1.0"))

if not TOKEN:
    print("TELEGRAM_BOT_TOKEN is required", file=sys.stderr)
    sys.exit(1)
if not ALLOWED_CHAT_ID_RAW:
    print("ALLOWED_CHAT_ID is required", file=sys.stderr)
    sys.exit(1)
try:
    ALLOWED_CHAT_ID = int(ALLOWED_CHAT_ID_RAW)
except ValueError:
    print("ALLOWED_CHAT_ID must be an integer", file=sys.stderr)
    sys.exit(1)

MAX_WAIT_SECONDS = 300
MAX_CHUNK = 4000
CAPTURE_LINES = 2000
ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
BOX_CHARS = set(" \t─│╭╮╰╯┌┐└┘├┤┬┴┼━┃┏┓┗┛╔╗╚╝═║")

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(message)s",
    stream=sys.stderr,
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bridge")


async def run_tmux(*args: str) -> tuple[int, str, str]:
    log.info("tmux %s", " ".join(args))
    try:
        proc = await asyncio.create_subprocess_exec(
            "tmux", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError:
        raise RuntimeError("tmux binary not found on PATH")
    out, err = await proc.communicate()
    return (
        proc.returncode or 0,
        out.decode("utf-8", errors="replace"),
        err.decode("utf-8", errors="replace"),
    )


async def session_exists() -> bool:
    rc, _, _ = await run_tmux("has-session", "-t", TMUX_SESSION)
    return rc == 0


MODEL_RE = re.compile(r"^[A-Za-z0-9_.\-\[\]]+$")


def _claude_launch_cmd() -> str:
    parts = ["claude", "--dangerously-skip-permissions"]
    if CLAUDE_MODEL and CLAUDE_MODEL != "default":
        if not MODEL_RE.match(CLAUDE_MODEL):
            log.warning("ignoring invalid CLAUDE_MODEL=%r; using account default", CLAUDE_MODEL)
        else:
            parts += ["--model", CLAUDE_MODEL]
    return " ".join(parts)


async def create_session() -> None:
    Path(PROJECT_DIR).mkdir(parents=True, exist_ok=True)
    rc, _, err = await run_tmux(
        "new-session", "-d", "-s", TMUX_SESSION,
        "-c", PROJECT_DIR,
        _claude_launch_cmd(),
    )
    if rc != 0:
        raise RuntimeError(f"failed to create tmux session: {err.strip()}")


async def ensure_session() -> bool:
    if await session_exists():
        return False
    await create_session()
    return True


async def capture_pane(lines: int = CAPTURE_LINES) -> str:
    rc, out, err = await run_tmux(
        "capture-pane", "-t", TMUX_SESSION, "-p", "-S", f"-{lines}"
    )
    if rc != 0:
        raise RuntimeError(f"capture-pane failed: {err.strip()}")
    return out


async def send_keys_text(text: str, press_enter: bool = True) -> None:
    if text:
        rc, _, err = await run_tmux("send-keys", "-t", TMUX_SESSION, "-l", "--", text)
        if rc != 0:
            raise RuntimeError(f"send-keys failed: {err.strip()}")
    if press_enter:
        rc, _, err = await run_tmux("send-keys", "-t", TMUX_SESSION, "Enter")
        if rc != 0:
            raise RuntimeError(f"send-keys Enter failed: {err.strip()}")


async def send_special_key(key: str) -> None:
    rc, _, err = await run_tmux("send-keys", "-t", TMUX_SESSION, key)
    if rc != 0:
        raise RuntimeError(f"send-keys {key} failed: {err.strip()}")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


def strip_trailing_prompt(s: str) -> str:
    lines = s.rstrip("\n").split("\n")
    while lines:
        last = lines[-1].rstrip()
        if not last:
            lines.pop()
            continue
        if last.startswith(">"):
            lines.pop()
            continue
        if set(last) <= BOX_CHARS:
            lines.pop()
            continue
        break
    return "\n".join(lines)


def diff_output(before: str, after: str) -> str:
    if after.startswith(before):
        return after[len(before):]
    tail = before[-500:] if len(before) > 500 else before
    if tail:
        idx = after.rfind(tail)
        if idx >= 0:
            return after[idx + len(tail):]
    return after


def chunk_text(text: str, max_len: int = MAX_CHUNK) -> list[str]:
    if not text:
        return []
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_len:
        split = remaining.rfind("\n", 0, max_len)
        if split <= 0:
            split = max_len
        chunks.append(remaining[:split])
        remaining = remaining[split:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


async def wait_for_stable_output(before: str) -> tuple[str, bool]:
    stable_needed = max(1, int(round(OUTPUT_STABLE_SECONDS / POLL_INTERVAL_SECONDS)))
    stable_count = 0
    previous = before
    start = time.monotonic()
    while True:
        await asyncio.sleep(POLL_INTERVAL_SECONDS)
        current = await capture_pane()
        if current == previous:
            stable_count += 1
            if stable_count >= stable_needed:
                return current, False
        else:
            stable_count = 0
            previous = current
        if time.monotonic() - start > MAX_WAIT_SECONDS:
            return previous, True


def allowed(update: Update) -> bool:
    chat = update.effective_chat
    return chat is not None and chat.id == ALLOWED_CHAT_ID


async def reply_pre(update: Update, text: str) -> None:
    for chunk in chunk_text(text):
        escaped = html.escape(chunk)
        await update.effective_chat.send_message(
            f"<pre>{escaped}</pre>", parse_mode=ParseMode.HTML
        )


async def reply_plain(update: Update, text: str) -> None:
    await update.effective_chat.send_message(text)


async def try_autorestart(update: Update) -> None:
    try:
        if not await session_exists():
            await create_session()
            await reply_plain(update, "Auto-restarted session.")
    except Exception:
        log.exception("auto-restart failed")


async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        if update.effective_chat:
            log.info("dropped message from disallowed chat %s", update.effective_chat.id)
        return
    msg = update.message
    if msg is None or msg.text is None:
        return
    text = msg.text
    log.info("message from %s (%d chars)", ALLOWED_CHAT_ID, len(text))
    try:
        before = await capture_pane()
        await send_keys_text(text, press_enter=True)
        final, timed_out = await wait_for_stable_output(before)
        new = diff_output(before, final)
        new = strip_ansi(new)
        new = strip_trailing_prompt(new)
        if timed_out:
            await reply_plain(update, "⏰ (timed out waiting for stable output; partial result below)")
        if not new.strip():
            await reply_plain(update, "⏳ (no new output — Claude may still be thinking; try /status)")
            return
        await reply_pre(update, new)
    except RuntimeError as e:
        log.error("tmux error in handle_text: %s", e)
        await reply_plain(update, f"⚠️ tmux error: {e}")
        await try_autorestart(update)
    except Exception as e:
        log.exception("handler error")
        await reply_plain(update, f"💥 internal error: {type(e).__name__}")


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    log.info("/start from %s", update.effective_chat.id)
    await reply_plain(update, (
        "Claude Code Bridge\n\n"
        "Send any text to talk to Claude.\n\n"
        "Commands:\n"
        "/status — show last ~40 lines of the pane\n"
        "/cancel — send Ctrl-C to Claude\n"
        "/restart — recreate the tmux session\n"
        "/clear — send /clear to Claude (reset context)\n"
        "/raw <text> — send text without pressing Enter"
    ))


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    log.info("/status from %s", update.effective_chat.id)
    try:
        pane = await capture_pane(lines=40)
        pane = strip_ansi(pane)
        tail = "\n".join(pane.rstrip("\n").split("\n")[-40:])
        if not tail.strip():
            await reply_plain(update, "(pane is empty)")
            return
        await reply_pre(update, tail)
    except RuntimeError as e:
        log.exception("/status failed")
        await reply_plain(update, f"⚠️ tmux error: {e}")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    log.info("/cancel from %s", update.effective_chat.id)
    try:
        await send_special_key("C-c")
        await reply_plain(update, "Sent Ctrl-C.")
    except RuntimeError as e:
        log.exception("/cancel failed")
        await reply_plain(update, f"⚠️ tmux error: {e}")


async def cmd_restart(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    log.info("/restart from %s", update.effective_chat.id)
    try:
        if await session_exists():
            await run_tmux("kill-session", "-t", TMUX_SESSION)
        await create_session()
        await reply_plain(update, "Session restarted.")
    except RuntimeError as e:
        log.exception("/restart failed")
        await reply_plain(update, f"⚠️ tmux error: {e}")


async def cmd_clear(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    log.info("/clear from %s", update.effective_chat.id)
    try:
        await send_keys_text("/clear", press_enter=True)
        await reply_plain(update, "Context cleared.")
    except RuntimeError as e:
        log.exception("/clear failed")
        await reply_plain(update, f"⚠️ tmux error: {e}")


async def cmd_raw(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    log.info("/raw from %s", update.effective_chat.id)
    msg = update.message
    if msg is None or msg.text is None:
        return
    payload = msg.text.partition(" ")[2]
    if not payload:
        await reply_plain(update, "Usage: /raw <text>")
        return
    try:
        await send_keys_text(payload, press_enter=False)
        await reply_plain(update, "Sent (no Enter).")
    except RuntimeError as e:
        log.exception("/raw failed")
        await reply_plain(update, f"⚠️ tmux error: {e}")


async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    log.info("file upload from %s", update.effective_chat.id)
    msg = update.message
    if msg is None:
        return
    try:
        uploads = Path(PROJECT_DIR) / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        if msg.document:
            tg_file = await msg.document.get_file()
            raw_name = msg.document.file_name or "document"
            safe_name = os.path.basename(raw_name).lstrip(".") or "document"
            if "/" in safe_name or "\\" in safe_name or ".." in safe_name:
                safe_name = "document"
            dest = uploads / f"{ts}_{safe_name}"
            if not dest.resolve().is_relative_to(uploads.resolve()):
                raise ValueError("upload path escapes uploads dir")
        elif msg.photo:
            largest = msg.photo[-1]
            tg_file = await largest.get_file()
            dest = uploads / f"{ts}_photo_{largest.file_unique_id}.jpg"
        else:
            await reply_plain(update, "Unsupported attachment.")
            return
        await tg_file.download_to_drive(custom_path=str(dest))
        await reply_plain(update, str(dest.resolve()))
    except Exception as e:
        log.exception("file upload failed")
        await reply_plain(update, f"💥 internal error: {type(e).__name__}")


async def post_init(app: Application) -> None:
    created = await ensure_session()
    if created:
        await asyncio.sleep(2)
        try:
            banner = await capture_pane(lines=60)
            banner = strip_ansi(banner).rstrip()
            if banner:
                trimmed = banner[-MAX_CHUNK:]
                escaped = html.escape(trimmed)
                await app.bot.send_message(
                    chat_id=ALLOWED_CHAT_ID,
                    text=f"<pre>{escaped}</pre>",
                    parse_mode=ParseMode.HTML,
                )
        except Exception:
            log.exception("failed to send startup banner")
    log.info("bot ready; session=%s project=%s model=%s", TMUX_SESSION, PROJECT_DIR, CLAUDE_MODEL)


def main() -> None:
    app = Application.builder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("restart", cmd_restart))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CommandHandler("raw", cmd_raw))
    app.add_handler(MessageHandler(filters.Document.ALL | filters.PHOTO, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
