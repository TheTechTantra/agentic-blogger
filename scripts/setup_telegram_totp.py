#!/usr/bin/env python3
"""
One-time TOTP secret provisioning for the Telegram bot's second factor.

Generates a TOTP (RFC 6238) secret, stores it in TinyDB under
TELEGRAM_TOTP_SECRET, and prints both the raw secret (for manual entry)
and an ASCII QR code (for camera scan) so it can be enrolled in any
compliant authenticator app — FreeOTP, Aegis Authenticator, andOTP,
Google Authenticator, etc.

No browser needed — unlike blogger_authorize.py, this can run inside a
container:
  docker compose run --rm telegram python -m scripts.setup_telegram_totp

Refuses to overwrite an existing secret (would lock out an already
enrolled device) unless --force is passed.
"""

import logging
import sys

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

try:
    import pyotp
    import qrcode
except ImportError as e:
    logger.error(f"Import failed: {e}")
    logger.error("Install: pip install pyotp qrcode")
    sys.exit(1)

try:
    from agentic_blogger.secrets.store import SecretStore
except ImportError as e:
    logger.error(f"Failed to import SecretStore: {e}")
    logger.error("Make sure .env is configured and TinyDB is running")
    sys.exit(1)


def setup_totp(force: bool = False) -> bool:
    logger.info("=== Telegram TOTP Setup ===\n")

    store = SecretStore()

    existing = store.get_optional("TELEGRAM_TOTP_SECRET")
    if existing and not force:
        logger.error("TELEGRAM_TOTP_SECRET already set.")
        logger.error("Re-running would lock out any device already enrolled.")
        logger.error("Pass --force to regenerate anyway.")
        sys.exit(1)

    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name="AgenticBlogger", issuer_name="AgenticBlogger")

    try:
        store.put("TELEGRAM_TOTP_SECRET", secret)
        logger.info("✓ TELEGRAM_TOTP_SECRET stored")
    except Exception as e:
        logger.error(f"Failed to store secret: {e}")
        sys.exit(1)

    logger.info("\nManual entry (works in any authenticator app, no camera needed):")
    logger.info(f"  Secret: {secret}")
    logger.info("\nOr scan this QR code:\n")

    qr = qrcode.QRCode()
    qr.add_data(uri)
    qr.make()
    qr.print_ascii(invert=True)

    logger.info("\n=== Setup Complete ===")
    logger.info("Enroll the secret above in FreeOTP, Aegis Authenticator, andOTP,")
    logger.info("or Google Authenticator. Every Telegram command now requires the")
    logger.info("current 6-digit code as its first word, e.g.:")
    logger.info("  /topic 482913 kubernetes operators explained")

    return True


if __name__ == "__main__":
    try:
        force = "--force" in sys.argv
        success = setup_totp(force=force)
        sys.exit(0 if success else 1)
    except Exception as e:
        logger.exception(f"Unexpected error: {e}")
        sys.exit(1)
