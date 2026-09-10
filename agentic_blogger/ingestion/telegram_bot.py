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
from agentic_blogger.config.loader import prompts_spec
from agentic_blogger.db import repo
from agentic_blogger.observability.log_setup import bind_job, setup_logging
from agentic_blogger.secrets.store import SecretStore
from agentic_blogger.security import totp

_TOTP_CODE_RE = re.compile(r"\d{6}")
# Deliberately anchored: only a leading http(s) URL turns a message into a
# URL-sourced job. A URL mentioned mid-sentence is part of the topic text.
_URL_RE = re.compile(r"^https?://\S+$")

# httpx logs every request at INFO including the full URL, which for the
# Telegram API means the bot token as a path segment. It runs at INFO again
# because setup_logging installs a redaction filter that rewrites the token
# out of the record before it is emitted — the per-request line is worth
# keeping, it is the only evidence that a poll actually left the container.
setup_logging("telegram")
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
        # Length only, never the text: a message body starts with a live TOTP
        # code and may carry anything else the user typed.
        logger.info("update received handler=%s user_id=%s chat_id=%s text_len=%d",
                    func.__name__, user_id,
                    update.effective_chat.id if update.effective_chat else None,
                    len(update.message.text or "") if update.message else 0)
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
        logger.info("authorized handler=%s user_id=%s argc=%d", func.__name__, user_id,
                    len(rest_args))
        try:
            return await func(update, context)
        except Exception:
            # Without this the handler error goes only to PTB's own error
            # machinery and the user sees silence; log it against the handler
            # that raised, then let PTB handle it as before.
            logger.exception("handler=%s raised for user_id=%s", func.__name__, user_id)
            raise
    return wrapper


async def _queue(update: Update, topic_text: str, source_url: str | None) -> None:
    logger.info("queueing source_url=%s topic=%r", source_url or "-", topic_text[:120])
    result = repo.create_topic_and_job(
        topic_text,
        source="telegram",
        requested_by=str(update.effective_user.id),
        config_snapshot={
            "config_version": "v1",
            # Which prompt alias this job was queued against. Not the resolved
            # version — that is per node (alias resolution happens at each
            # node), and lands on node_runs.prompts_json.
            "prompt_alias": prompts_spec()["alias"],
            "telegram_chat_id": update.effective_chat.id,
        },
        source_url=source_url,
    )
    with bind_job(result["job_id"]):
        if result["reused"]:
            # Worth a WARNING: this is the shape that confuses people — the
            # same topic text dedupes onto an earlier job, including one that
            # already exhausted its attempts and will never run again.
            logger.warning("deduped onto existing job (state=%s) — no new work queued",
                           result["state"])
        else:
            logger.info("queued new job source=telegram requested_by=%s state=%s",
                        update.effective_user.id, result["state"])

    if result["reused"]:
        await update.message.reply_text(
            f"Already queued as job {result['job_id']} (state={result['state']})"
        )
        return
    prefix = f"Queued from URL. job_id={result['job_id']}" if source_url \
        else f"Queued. job_id={result['job_id']}"
    await update.message.reply_text(
        f"{prefix}\nUse /status {result['job_id']} to check progress."
    )


@restricted
async def cmd_topic(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # restricted() has already stripped the leading TOTP code and command
    # name — context.args is just the real content now, for both /topic
    # and bare-text entry.
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Usage: /topic <text>")
        return

    # A bare URL pasted with no command means "write about this article" —
    # route it through the URL path rather than searching for the URL string.
    first, _, rest = text.partition(" ")
    if _URL_RE.match(first):
        await _queue(update, rest.strip() or first, first)
        return

    await _queue(update, text, None)


@restricted
async def cmd_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/url <link> [angle] — the page is fetched by Anthropic's server-side
    web_fetch tool inside the research node. Nothing is downloaded here; the
    bot only records the URL."""
    if not context.args:
        await update.message.reply_text("Usage: /url <link> [angle]")
        return

    url = context.args[0].strip()
    if not _URL_RE.match(url):
        await update.message.reply_text("First argument must be an http(s) URL.")
        return
    # web_fetch rejects URLs over 250 characters with url_too_long. Catching it
    # here costs nothing; catching it in the research node costs a job run.
    if len(url) > 250:
        await update.message.reply_text("URL too long (max 250 characters).")
        return

    angle = " ".join(context.args[1:]).strip()
    await _queue(update, angle or url, url)


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
    app.add_handler(CommandHandler("url", cmd_url))
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
