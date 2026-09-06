"""Telegram bot — long-polling, command handlers, job queueing.

Writes rows and nothing else. Never imports node or graph code — only
db/ and config/ — so the two containers stay genuinely independent.
"""

import logging
import os
import re
from datetime import datetime, timedelta, timezone
from functools import wraps

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from agentic_blogger.config.loader import load_models
from agentic_blogger.db import repo
from agentic_blogger.secrets.store import SecretStore
from agentic_blogger.security import totp

_TOTP_CODE_RE = re.compile(r"\d{6}")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger(__name__)

_store = SecretStore(readonly=True)
MLFLOW_PUBLIC_URL = os.getenv("MLFLOW_PUBLIC_URL", "http://127.0.0.1:25000")


def _allowed_ids() -> set[str]:
    _store.refresh()
    raw = _store.get_optional("TELEGRAM_ALLOWED_USER_IDS") or ""
    return {x.strip() for x in raw.split(",") if x.strip()}


def restricted(func):
    """Mandatory per plan §7 — without it, anyone who guesses the bot
    username spends your Anthropic credits.

    Also enforces a TOTP second factor (defense in depth alongside the
    user-ID allowlist): every message must lead with a fresh, unused
    6-digit code. Normalizes both entry paths (CommandHandler args vs
    bare text) into `context.args` with the code stripped, so every
    downstream handler's existing parsing keeps working unchanged —
    update.message itself is frozen in PTB v20+ and can't be rewritten.
    """
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id) if update.effective_user else None
        if user_id not in _allowed_ids():
            logger.warning("Unauthorized access attempt from user_id=%s", user_id)
            await update.message.reply_text("Not authorized.")
            return

        if context.args:
            code, rest_args = context.args[0], context.args[1:]
        else:
            parts = (update.message.text or "").split(maxsplit=1)
            code = parts[0] if parts else ""
            rest_args = [parts[1]] if len(parts) > 1 else []

        if not _TOTP_CODE_RE.fullmatch(code) or not totp.verify_and_consume(user_id, code):
            logger.warning("Invalid/replayed/missing TOTP code from user_id=%s", user_id)
            await update.message.reply_text("Invalid or expired code.")
            return

        context.args = rest_args
        return await func(update, context)
    return wrapper


@restricted
async def cmd_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # restricted() has already stripped the leading TOTP code and command
    # name — context.args is just the real content now, for both /topic
    # and bare-text entry.
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /topic <text>")
        return

    result = repo.create_topic_and_job(
        text,
        source="telegram",
        requested_by=str(update.effective_user.id),
        config_snapshot={
            "config_version": "v1",
            "telegram_chat_id": update.effective_chat.id,
        },
    )
    if result["reused"]:
        await update.message.reply_text(
            f"Already queued as job {result['job_id']} (state={result['state']})"
        )
    else:
        await update.message.reply_text(
            f"Queued. job_id={result['job_id']}\nUse /status {result['job_id']} to check progress."
        )


@restricted
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /status <job_id>")
        return
    job = repo.get_job(context.args[0])
    if not job:
        await update.message.reply_text("Job not found.")
        return
    lines = [
        f"Job {job['id']}",
        f"Topic: {job['topic_text']}",
        f"State: {job['state']}",
        f"Attempt: {job['attempt']}/{job['max_attempts']}",
        f"Cost so far: ${job['cost_usd']:.4f}",
    ]
    if job.get("mlflow_run_id"):
        lines.append(f"MLflow: {MLFLOW_PUBLIC_URL}/#/experiments/0/runs/{job['mlflow_run_id']}")
    if job.get("failure_reason"):
        lines.append(f"Failure: {job['failure_reason']}")
    await update.message.reply_text("\n".join(lines))


@restricted
async def cmd_queue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    jobs = repo.list_jobs(["QUEUED", "RUNNING"])
    if not jobs:
        await update.message.reply_text("Queue is empty.")
        return
    lines = [f"{j['id']} [{j['state']}] {j['topic_text'][:40]}" for j in jobs]
    await update.message.reply_text("\n".join(lines))


@restricted
async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /cancel <job_id>")
        return
    state = repo.request_cancel(context.args[0])
    if state is None:
        await update.message.reply_text("Job not found.")
    else:
        await update.message.reply_text(f"Job now: {state}")


@restricted
async def cmd_retry(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /retry <job_id>")
        return
    job_id = repo.retry_job(context.args[0])
    if job_id is None:
        await update.message.reply_text("Job not found, or not in FAILED/BLOCKED state.")
    else:
        await update.message.reply_text(f"Re-queued job {job_id}. It will resume from its checkpoint.")


@restricted
async def cmd_cost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    period = context.args[0] if context.args else "today"
    now = datetime.now(timezone.utc)
    since = now - timedelta(days=1) if period == "today" else now - timedelta(days=7)
    total = repo.cost_since(since)
    await update.message.reply_text(f"Cost ({period}): ${total:.4f}")


@restricted
async def cmd_models(update: Update, context: ContextTypes.DEFAULT_TYPE):
    roles = load_models()["roles"]
    lines = [f"{role}: {spec['model']}" for role, spec in roles.items()]
    await update.message.reply_text("\n".join(lines))


def main():
    token = SecretStore().get("TELEGRAM_BOT_TOKEN")

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("topic", cmd_topic))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("queue", cmd_queue))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("retry", cmd_retry))
    app.add_handler(CommandHandler("cost", cmd_cost))
    app.add_handler(CommandHandler("models", cmd_models))
    # Bare text (no leading slash) is treated as a topic submission
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_topic))

    logger.info("Telegram bot starting (long polling)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
