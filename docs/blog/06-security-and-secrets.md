# 6. Security and Secrets: Nobody Holds the Keys

*Part 6 of the [Agentic Blogger series](README.md).*

The threat model for a personal LLM pipeline is specific and easy to
underestimate:

- Anyone who guesses the bot's username can spend your provider credits.
- A leaked provider key is a bill, not an inconvenience.
- A leaked Blogger OAuth refresh token is publish access to your blog.
- Model-generated HTML goes onto a public platform.
- Logs are the most common place secrets escape.

Five defences, one per problem.

---

## 1. Two factors on every single message

Not on login — there is no login. On **every message**.

```python
def restricted(func):
    """Mandatory per plan §7 — without it, anyone who guesses the bot
    username spends your Anthropic credits.

    Also enforces a TOTP second factor (defense in depth alongside the
    user-ID allowlist): every message must lead with a fresh, unused
    6-digit code."""
```

So a message looks like:

```text
/topic 483920 What are embeddings in vector DBs?
```

Factor one is the user-ID allowlist — and it is read from **TinyDB, refreshed
per call**, not from the environment. Revoking access does not require a
restart.

```python
if user_id not in _allowed_ids():
    logger.warning("Unauthorized access attempt from user_id=%s", user_id)
    await update.message.reply_text("Not authorized.")
    return
```

Factor two is TOTP. Telegram account compromise alone is not enough.

### Replay protection pyotp does not give you

This is the part most TOTP integrations get wrong:

```python
"""Replay protection: pyotp's own `.verify()` only checks whether a code is
valid for *some* step in the window — it says nothing about whether that
exact step has already been consumed. We find the specific matched step
and compare it against a persisted high-water mark in Postgres (TinyDB has
no CAS/atomicity primitive) so the same code can't be replayed
even within its own 30s validity window."""
```

```python
def _matched_step(totp: pyotp.TOTP, code: str) -> int | None:
    """Return the specific timestep `code` is valid for, or None."""
    current_step = int(time.time() // _INTERVAL)
    for step in range(current_step - _VALID_WINDOW, current_step + _VALID_WINDOW + 1):
        candidate = totp.at(step * _INTERVAL)
        if hmac.compare_digest(candidate, code):
            return step
    return None
```

```python
step = _matched_step(totp, code)
last_step = state["last_step"] if state else 0

if step is None or step <= last_step:
    repo.record_totp_failure(user_id, max_attempts=_MAX_FAILED_ATTEMPTS,
                              lockout_minutes=_LOCKOUT_MINUTES)
    return False

repo.record_totp_success(user_id, step)
```

Three things to notice:

- `hmac.compare_digest`, not `==`. Constant-time comparison on a secret-derived
  value.
- `step <= last_step` is a monotonic high-water mark. An observed code is dead
  the moment it is used, even inside its 30-second window.
- The state lives in **Postgres**, not TinyDB, and the reason is written down:
  TinyDB has no compare-and-set primitive. *Pick the store that has the
  primitive your invariant needs.*

Plus a lockout:

```python
_MAX_FAILED_ATTEMPTS = 5
_LOCKOUT_MINUTES = 5
```

```python
if state and state.get("locked_until") and state["locked_until"] > _now():
    logger.warning("user_id=%s locked out until %s", user_id, state["locked_until"])
    return False
```

### The decorator rewrites args so handlers stay simple

```python
if context.args:
    code, rest_args = context.args[0], context.args[1:]
else:
    parts = (update.message.text or "").split(maxsplit=1)
    code = parts[0] if parts else ""
    rest_args = [parts[1]] if len(parts) > 1 else []
...
context.args = rest_args
```

Two entry paths — `CommandHandler` (which has already stripped the command) and
bare text — normalize into one shape with the code removed. Downstream handlers
parse arguments exactly as if TOTP did not exist. Auth that changes the message
format is auth every handler has to know about; this one does not.

(`update.message` is frozen in PTB v20+, which is why the normalization targets
`context.args`.)

### Logging an authenticated request without logging the credential

```python
# Length only, never the text: a message body starts with a live TOTP
# code and may carry anything else the user typed.
logger.info("update received handler=%s user_id=%s chat_id=%s text_len=%d", ...)
```

You still get the audit trail — who, which handler, how big. You do not get the
live code.

---

## 2. Secrets in a vault, not in the environment

`.env` holds no credentials. Secrets live in **TinyDBService**, a small
encrypted HTTP vault, reached through a client:

```python
class SecretStore:
    """Read-only client for TinyDBService credential store."""

    def __init__(self, tinydb_url=None, api_key=None, readonly: bool = False):
```

What it holds: `ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`,
`TELEGRAM_ALLOWED_USER_IDS`, `TELEGRAM_TOTP_SECRET`, and the Blogger
`CLIENT_ID` / `CLIENT_SECRET` / `REFRESH_TOKEN` / `BLOG_ID`.

Three properties over env vars:

1. **Rotatable without a restart.** `_store.refresh()` re-reads.
2. **Not visible in `docker inspect`**, process environment, or a crash dump.
3. **`readonly=True`** available where writes would be a bug — the TOTP module
   uses it.

Every port in the stack binds to `127.0.0.1` only.

---

## 3. The workers hold no provider credential at all

The strongest control in the system, and it is structural rather than
procedural.

```python
_GATEWAY_PLACEHOLDER_KEY = "mlflow-gateway"
```

```python
def _ensure_api_key(provider: str) -> None:
    """A no-op when the gateway is enabled. Not merely unnecessary then, but
    unwanted: putting a provider key in this process's environment is exactly
    the thing routing through the gateway is meant to stop, and a key sitting
    in os.environ is one accidental direct base_url away from being used."""
    if gateway_spec()["enabled"]:
        return
```

The real keys live encrypted in the gateway's store. Clients are constructed
with a placeholder string that exists only to satisfy their own constructor
validation.

And the invariant is *testable*:

> `grep -E 'ANTHROPIC|OPENAI|GEMINI'` on the orchestrator's environment
> returning nothing is the invariant, and it is what makes "no call bypasses the
> gateway" enforceable rather than merely intended.

That sentence is the whole security argument. A policy you can grep for is a
policy. A policy in a code-review checklist is a hope.

Consequences worth stating:

- Compromising the worker container yields no provider key.
- Key rotation touches TinyDB and the gateway, never the workers.
- Because no bypass exists, budget enforcement is real rather than advisory.

---

## 4. Redaction at the logging handler

Covered in [post 3](03-observability.md); restated here because it is a security
control, not a formatting nicety.

The Telegram bot token is a URL path segment, so `httpx` at INFO printed it
thousands of times a day. The fix redacts at the handler so library records are
covered too:

```python
_TELEGRAM_TOKEN_RE = re.compile(r"(/bot\d{5,}):[A-Za-z0-9_-]{20,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{20,}")
_SUSPECT = re.compile(r"(?i)/bot\d|bearer |token|secret|api[_-]?key|authorization")
```

plus a key-shaped `key=value` scrubber:

```python
_SECRET_KEY_SOURCE = (
    r"api[_-]?key|token|secret|authorization|refresh_token|client_secret|bearer"
)
```

Two notes.

**`basicConfig(force=True)`** matters here. A library that configured logging
first would leave a second, *unfiltered* handler attached — your filter installed,
the secret still printed.

**A bug that hid for a long time**, preserved in the source:

```python
# The inline (?i) used to live inside this pattern, which made it unusable as
# a fragment: Python 3.11+ rejects a global flag that is not at the start of
# the *composed* expression, so scrub_text raised re.PatternError on every
# call. It went unnoticed because nothing imported this module until the log
# filter did.
```

**A security control nothing calls is not a control.** Worth periodically
grepping for callers of your own defensive code — `scrub_dict` still has none,
which is why MLflow traces are unredacted today.

And the caveat both modules repeat: *redaction is a backstop, not a licence —
never pass a secret to a logger on purpose.*

---

## 5. Treating model output as untrusted input

The content pipeline reads arbitrary web pages and emits HTML to a public blog.
The format node is the boundary:

```python
_md = MarkdownIt("commonmark", {"html": False}).enable("table")

raw_html = _md.render(markdown)
safe_html = bleach.clean(raw_html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)
```

Defence in depth: `{"html": False}` blocks raw HTML at the renderer, `bleach`
allowlists what comes out. Attributes are allowlisted per tag —

```python
_ALLOWED_ATTRS = {
    "a": ["href", "title", "rel"],
    "img": ["src", "alt", "title"],
    "*": ["class"],
}
```

— so no `onclick`, no `style`, no `srcset`.

The prompt-template injection defence in
[post 5](05-prompts-and-models-as-config.md) is the same instinct applied to a
different engine: generated text is substituted *last*, so it can never be a
substitution target itself.

And the sanitizer is monitored, because sanitizers delete silently:

```python
logger.info("formatted markdown=%dch html=%dch (sanitizer dropped %dch) ...", ...)
```

---

## 6. OAuth handled as an expiring credential

The Blogger grant is on a Testing-mode consent screen, which means it expires on
a schedule. That is treated as a first-class operational event, not an
exception:

```python
if "invalid_grant" in str(e):
    logger.error("Blogger OAuth refresh rejected as invalid_grant")
    raise InvalidGrantError(str(e)) from e
```

```python
# Expiry is the thing that silently kills this pipeline (the grant is on a
# Testing-mode consent screen), so print it every publish rather than
# discovering it from a 400 weeks later.
logger.info("Blogger credentials refreshed, access token expires %s", creds.expiry)
```

Expiry printed on **every** publish, so the deadline is always visible in the
logs. The failure routes to `BLOCKED` with a message naming the recovery script,
and the checkpoint means recovery costs nothing but the re-auth.

---

## Scorecard

| Control | Where | Strength |
|---|---|---|
| User allowlist | `telegram_bot.restricted` | Refreshed per call from TinyDB |
| TOTP second factor | `security/totp.py` | Per message, replay-protected, constant-time compare, lockout |
| Secrets vault | `secrets/store.py` + TinyDBService | Off-env, rotatable, readonly mode |
| No provider keys in workers | `llm/registry.py` | **Structural** — greppable invariant |
| Log redaction | `observability/log_setup.py` | Handler-level, covers libraries |
| HTML sanitization | `nodes/format.py` | Allowlist + renderer-level block |
| Template injection | `prompts/registry.py` | Ordered substitution |
| OAuth expiry | `publishing/blogger_client.py` | Explicit `BLOCKED` state + visible countdown |

### Known gaps

- **MLflow traces are unredacted.** `autolog()` captures prompts and responses;
  `scrub_dict` has no caller. If a secret ever entered a prompt it would land in
  a trace.
- **`verify=False`** on the `SecretStore` HTTP client (`# local network`).
  Acceptable on a loopback-bound bridge network, not beyond it.
- **No audit trail for secret reads.** Who read what, when, is not recorded.
- **No key rotation schedule.** Rotation is supported; nothing prompts it.

---

## The transferable idea

Most of these controls share one property: they are **structural rather than
procedural**. The worker cannot leak a provider key because it does not have
one. A handler cannot skip TOTP because the decorator is the only path to it.
The backup prompts cannot become a runtime fallback because a test fails if they
do.

Procedural controls decay as soon as the person who wrote them stops reviewing
every change. Structural ones do not.

---

**Next:** [What I'd Improve Next](07-improvement-roadmap.md).
