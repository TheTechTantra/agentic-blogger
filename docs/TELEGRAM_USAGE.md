# Telegram Bot — Command Reference & Test Walkthrough

Operator guide for `agentic_blogger/ingestion/telegram_bot.py`. For how the bot
fits the rest of the system, see [ARCHITECTURE.md](ARCHITECTURE.md) §7.

---

## 1. Message format

Every message — command or bare text — must carry a fresh 6-digit TOTP code.
**Its position differs between the two entry paths:**

| Entry path | Format | Example |
|------------|--------|---------|
| Slash command | `/command <code> [args...]` | `/topic 482913 kubernetes operators explained` |
| Bare text (no slash) | `<code> <topic text>` | `482913 kubernetes operators explained` |

Why the difference: python-telegram-bot's `CommandHandler` strips `/topic` and
hands the rest to `context.args`, so the code lands in `args[0]`. The bare-text
`MessageHandler` gets the raw string, so the code is the first word. The
`restricted` decorator normalizes both into `context.args` with the code
removed, then calls the handler.

---

## 2. Commands

| Command | Arguments | Effect |
|---------|-----------|--------|
| `/topic` | `<code> <text>` | Queue a blog job. Replies with `job_id`. If the topic hash already exists, replies `Already queued as job <id> (state=…)` instead of creating a second job |
| `/url` | `<code> <link> [angle]` | Queue a job sourced from a URL. `link` must be a leading `http(s)://` URL, max 250 chars. Optional `angle` becomes the topic text; without it the URL itself is the topic |
| `/status` | `<code> <job_id>` | Job id, topic, state, `attempt/max_attempts`, cost so far, MLflow run link, failure reason |
| `/queue` | `<code>` | All `QUEUED` + `RUNNING` jobs, one per line, topic truncated to 40 chars |
| `/cancel` | `<code> <job_id>` | Request cancellation; replies with the resulting state |
| `/retry` | `<code> <job_id>` | Re-queue a `FAILED`/`BLOCKED` job. Resumes from its checkpoint. Any other state replies "not in FAILED/BLOCKED state" |
| `/cost` | `<code> [today\|week]` | Aggregate spend. Defaults to `today` (last 24h); anything else is treated as `week` (last 7 days) |
| `/models` | `<code>` | Current role → model mapping from `load_models()` |
| *(bare text)* | `<code> <text>` | Same handler as `/topic` |

### URL handling inside `/topic`

A bare-text or `/topic` message whose **first word** is an `http(s)` URL is
routed down the URL path automatically — the rest of the line becomes the
topic text, the URL becomes `source_url`. A URL appearing mid-sentence is
treated as ordinary topic text (`_URL_RE` is anchored with `^…$`).

The page is never downloaded by the bot. It is fetched later by Anthropic's
server-side `web_fetch` tool inside the research node.

---

## 3. Auth model

Two independent gates, both enforced on **every** message by the `restricted`
decorator:

1. **Allowlist** — `update.effective_user.id` must appear in
   `TELEGRAM_ALLOWED_USER_IDS`, read from TinyDB (refreshed per call), not from
   the environment. Failure replies `Not authorized.`
2. **TOTP** — the code must be a fresh, unused 6-digit code. Failure replies
   `Invalid or expired code.`

TOTP parameters (`agentic_blogger/security/totp.py`):

| Parameter | Value |
|-----------|-------|
| Interval | 30s |
| Accepted skew | ±1 step (±30s) |
| Replay protection | Matched step must exceed the `last_step` high-water mark in `telegram_totp_state` — a code cannot be reused even inside its own validity window |
| Lockout | 5 failed attempts → `locked_until` set 5 minutes ahead |

Practical consequence: **one code per message.** Sending two commands inside the
same 30-second window requires waiting for the authenticator to roll over.

---

## 4. First-time setup

```bash
# 1. Start the stack
make up

# 2. Apply migrations
make migrate

# 3. Enrol TOTP (prints the secret + an ASCII QR code)
docker compose run --rm telegram python -m scripts.setup_telegram_totp
```

Scan or hand-enter the secret into any RFC 6238 authenticator (FreeOTP, Aegis,
andOTP, Google Authenticator). Re-running refuses to overwrite an enrolled
secret; `--force` regenerates and locks out the existing device.

Your numeric Telegram user id must be present in the `TELEGRAM_ALLOWED_USER_IDS`
TinyDB secret (comma-separated). Get the id from `@userinfobot`.

---

## 5. End-to-end test walkthrough

Watch both containers in a second terminal:

```bash
docker compose logs -f telegram orchestrator
```

Then, in the Telegram chat (substitute a live code each time):

```
/models 482913
```
Confirms secrets, TinyDB, and config loading are wired. Should list role → model.

```
/topic 519204 kubernetes operators explained
```
Expect `Queued. job_id=<id>` plus a `/status` hint.

```
/queue 733815
```
Expect the job listed as `QUEUED` or `RUNNING`.

```
/status 004127 <job_id>
```
Poll as the pipeline advances. Cost climbs; once `jobs.mlflow_run_id` is set an
MLflow link appears (base URL from `MLFLOW_PUBLIC_URL`, default
`http://127.0.0.1:25000`).

### URL path

```
/url 662901 https://example.com/some-article what this means for platform teams
```
Expect `Queued from URL. job_id=<id>`.

### Failure paths

```
/cancel 118340 <job_id>
/retry  553027 <job_id>
/cost   901772 today
```

### Negative cases worth exercising

| Input | Expected reply |
|-------|----------------|
| Command with no code | `Invalid or expired code.` |
| Same code twice | `Invalid or expired code.` (replay rejected) |
| From a non-allowlisted account | `Not authorized.` |
| `/url <code> not-a-url` | `First argument must be an http(s) URL.` |
| `/url <code> <251+ char URL>` | `URL too long (max 250 characters).` |
| Same topic text twice | `Already queued as job <id> (state=…)` |
| `/status <code> nosuchjob` | `Job not found.` |
| `/retry` on a `SUCCEEDED` job | `Job not found, or not in FAILED/BLOCKED state.` |

---

## 6. Notes

- Status is **pull-only**. The pipeline never pushes progress back to Telegram;
  `/status` is the only way to observe a running job from the chat.
- The bot writes rows and nothing else. It imports only `db/`, `config/`,
  `secrets/`, `security/` — never graph or node code — so the telegram and
  orchestrator containers stay independent.
