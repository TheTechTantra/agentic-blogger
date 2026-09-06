"""TOTP (RFC 6238) second factor for the Telegram bot.

Standard, open algorithm — works with any compliant authenticator app
(FreeOTP, Aegis, andOTP, Google Authenticator, ...). Server-side generation
and verification via `pyotp`.

Replay protection: pyotp's own `.verify()` only checks whether a code is
valid for *some* step in the window — it says nothing about whether that
exact step has already been consumed. We find the specific matched step
and compare it against a persisted high-water mark in Postgres (TinyDB has
no CAS/atomicity primitive — see plan) so the same code can't be replayed
even within its own 30s validity window.
"""

import hmac
import logging
import time
from datetime import datetime, timezone

import pyotp

from agentic_blogger.db import repo
from agentic_blogger.secrets.store import SecretStore

logger = logging.getLogger(__name__)

_INTERVAL = 30
_VALID_WINDOW = 1  # +/- 30s clock skew
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_MINUTES = 5

_store = SecretStore(readonly=True)


def generate_secret() -> str:
    return pyotp.random_base32()


def provisioning_uri(secret: str, account_name: str, issuer: str = "AgenticBlogger") -> str:
    return pyotp.TOTP(secret).provisioning_uri(name=account_name, issuer_name=issuer)


def _matched_step(totp: pyotp.TOTP, code: str) -> int | None:
    """Return the specific timestep `code` is valid for, or None."""
    current_step = int(time.time() // _INTERVAL)
    for step in range(current_step - _VALID_WINDOW, current_step + _VALID_WINDOW + 1):
        candidate = totp.at(step * _INTERVAL)
        if hmac.compare_digest(candidate, code):
            return step
    return None


def verify_and_consume(user_id: str, code: str) -> bool:
    """Validate `code` for `user_id`, enforcing replay protection and a
    failed-attempt lockout. Returns True only on a fresh, correct code."""
    _store.refresh()
    secret = _store.get_optional("TELEGRAM_TOTP_SECRET")
    if not secret:
        logger.error("TELEGRAM_TOTP_SECRET not set — run scripts/setup_telegram_totp.py")
        return False

    state = repo.get_totp_state(user_id)
    if state and state.get("locked_until") and state["locked_until"] > _now():
        logger.warning("user_id=%s locked out until %s", user_id, state["locked_until"])
        return False

    totp = pyotp.TOTP(secret, interval=_INTERVAL)
    step = _matched_step(totp, code)
    last_step = state["last_step"] if state else 0

    if step is None or step <= last_step:
        repo.record_totp_failure(user_id, max_attempts=_MAX_FAILED_ATTEMPTS,
                                  lockout_minutes=_LOCKOUT_MINUTES)
        return False

    repo.record_totp_success(user_id, step)
    return True


def _now():
    return datetime.now(timezone.utc)
