# Agentic-Blogger: Architecture & Feature Overview

**Last Updated:** 2026-09-07
**Verified against:** working tree at commit `41a9faa` + uncommitted changes
**Status:** Core blog pipeline implemented end-to-end. Publishes as a Blogger **draft**, not live. Video pipeline is schema-only.

---

## 1. High-Level Architecture

```
                        ┌──────────────────────────────┐
                        │      Telegram Bot            │
                        │  services/telegram container │
                        │  long-poll; TOTP + allowlist │
                        │  writes DB rows only —       │
                        │  never imports graph/nodes   │
                        └───────────────┬──────────────┘
                                        │ create_topic_and_job() (QUEUED)
                                        │ get_job / list_jobs / request_cancel
                                        ▼
┌───────────────────────────────────────────────────────────────────────────┐
│                          PostgreSQL 16  (schema: app)                     │
│  topics · jobs · node_runs · research_sources · drafts · revisions ·      │
│  publications · video_* (unused) · telegram_totp_state                    │
│  + LangGraph PostgresSaver checkpoint tables (public schema)              │
└───────────────────────────────────────────────────────┬───────────────────┘
             ▲ claim_job() FOR UPDATE SKIP LOCKED       │ checkpoint read/write
             │ node_runs / drafts / publications writes │
┌────────────┴───────────────────────────────────────────▼──────────────────┐
│                       Orchestrator Worker (single replica)                │
│  orchestrator/worker.py : poll loop, POLL_INTERVAL_S=5                    │
│  graph/runner.py        : claim → invoke compiled graph → mark state      │
│  graph/builder.py       : 8 nodes, RetryPolicy(max_attempts=3) per node   │
│  checkpointer           : PostgresSaver (opened once, held for process)   │
└──────┬─────────────────────────────┬──────────────────────────┬───────────┘
       │ build_llm(role)             │ start_job_run()          │ SecretStore
       ▼                             ▼                          ▼
┌──────────────────┐      ┌────────────────────┐      ┌────────────────────┐
│ MLflow  :25000   │      │ MLflow  :25000     │      │ TinyDBService      │
│ AI GATEWAY       │      │ experiment         │      │ :28080 HTTP vault  │
│ the only path to │      │ agentic-blogger/   │      │ API keys, Blogger  │
│ a provider; holds│      │ blog; langchain    │      │ OAuth, bot token,  │
│ the keys, tracks │      │ autolog; fail-open │      │ user allowlist     │
│ usage, enforces  │      └────────────────────┘      └────────────────────┘
│ the budget       │
│ (fail-CLOSED)    │      ┌────────────────────────────────────────────────┐
└────────┬─────────┘      │ Google Blogger API v3 — posts.insert(          │
       │                  │ isDraft=True) + beacon reconcile via           │
       └── web_search_    │ posts.list(status=DRAFT)                       │
           20260209       └────────────────────────────────────────────────┘
           (Anthropic
            server tool)

Docker network: blogger-net (bridge).
```

---

## 2. Service Inventory

| Service | Source | Host Port | Purpose |
|---------|--------|-----------|---------|
| **Telegram Bot** | `services/telegram/` → `ingestion/telegram_bot.py` | — | Long-polling. Commands `/topic /status /queue /cancel /retry /cost /models`, plus bare text = topic. Every message needs a fresh TOTP code |
| **Orchestrator** | `services/orchestrator/` → `orchestrator/worker.py` | — | Single-replica poll loop; claims a job, runs the LangGraph pipeline |
| **PostgreSQL** | `postgres:16-alpine` | 127.0.0.1:25432 | `app` schema (business tables) + LangGraph checkpoint tables |
| **MLflow** | `services/mlflow/` (custom image) | 127.0.0.1:25000 | Three roles in one process: tracking server, Prompt Registry, and **AI Gateway** — the only path from the workers to an LLM provider. Postgres backend, `/mlartifacts` artifact root, `--workers 1` |
| **TinyDB Service** | `TinyDBService/` (submodule) | 127.0.0.1:28080 | Encrypted secrets vault over HTTP; `SecretStore` client. This is a custom lightweight vault — you can substitute any secret store (HashiCorp Vault, AWS Secrets Manager, etc.) by implementing the `SecretStore` interface in `secrets/store.py` |

All ports bind to `127.0.0.1` only.

---

## 3. LangGraph Pipeline — 8 nodes, one conditional loop

**Topology** (`graph/builder.py`):

```
                                       ┌────────────────┐
                                       │                ▼
research ──► outline ──► draft ──► factcheck ──► revise ─┘
                                       │
                                  needs_revision()
                                       │ "continue"
                                       ▼
                                     seo ──► format ──► publish ──► END
```

`needs_revision(state)` returns `"revise"` when the report contains any
`unsupported` / `contradicted` finding **and** `revision_count < MAX_REVISIONS`
(currently **1**). Otherwise `"continue"`. So the loop runs at most once, then
the post ships with the findings surfaced rather than blocking — deliberate
(factcheck advises, it does not gate).

Every node is registered with `RetryPolicy(max_attempts=3)`, so LangGraph
retries a raising node in-process before the exception reaches the runner.

**State** (`graph/state.py:BlogState`, a `TypedDict`, nodes return deltas):

| Key | Type | Written by |
|-----|------|-----------|
| `job_id`, `topic` | str | runner (initial state) |
| `sources` | `Annotated[list[dict], operator.add]` — appended, never replaced | research |
| `research_brief` | dict — `ResearchBrief.model_dump()`, degrades to `{"brief": str}` | research |
| `outline_plan` | dict — `{title_options, sections}` | outline |
| `draft_markdown`, `draft_title`, `draft_id` | str | draft, revise |
| `factcheck_report` | dict — `{findings: [...]}` | factcheck |
| `revision_count` | int | revise |
| `seo_meta` | dict — `{title, meta_description, labels, slug}` | seo |
| `html` | str | format |
| `published` | dict — `{remote_post_id, remote_url, state}` | publish |

State keys deliberately avoid the node names (`research`, `outline`,
`factcheck`, `seo`) — LangGraph rejects a state key equal to a node name.

### Node detail

| Node | Role / Model | What it does |
|------|--------------|--------------|
| **research** | `search_tool` (sonnet-5) then `research_synth` (**sonnet-5**) | Pass 1: `web_search_20260209` server tool, forced via `tool_choice`, `max_uses=12`, staff-practitioner system prompt demanding primary sources, per-tier searches, versioned claims, attributed code. Parses text + `web_search_tool_result` blocks (fail-open to empty sources), persists them via `insert_research_sources`. Pass 2: `.with_structured_output(ResearchBrief, include_raw=True)` — a server-tool call and structured output cannot share one invocation. Drops any code exemplar whose `source_url` did not survive. Falls back to `{"brief": raw}` if parsing fails |
| **outline** | `outline` (haiku-4.5) | `Outline` structured output from topic + `brief` prose: 3 title candidates, 4–7 sections each with key points and a target tier |
| **draft** | `draft` (opus-5, effort high → `thinking: adaptive`) | Full Markdown post, 1200–1800 words, no title heading. Persists `drafts` row at `version=0` |
| **factcheck** | `factcheck` (opus-5, effort high) | `FactCheckReport` — every checkable claim gets `supported` / `unsupported` / `contradicted`, confidence, source URL, suggested fix. Never raises on findings |
| **revise** | `draft` role (opus-5) | Rewrites the draft against the flagged findings only. Persists a new `drafts` row at `version=revision_count+1` and a `revisions` audit row, bumps `revision_count` |
| **seo** | `seo` (haiku-4.5) | `SeoMeta` — title ≤70 chars, meta description ≤160, labels truncated to 20 (Blogger's cap), slug |
| **format** | none — no LLM, no cost | `markdown-it-py` (commonmark, `html: False`, tables enabled) → `bleach.clean()` against a fixed tag/attribute allowlist. Prepends two HTML comments: the **beacon** `<!-- agentic-blogger:job={job_id} v=1 -->` and a factcheck tally |
| **publish** | none — no LLM | See §8 |

The beacon is not cosmetic — it is the publish node's idempotency mechanism
(§8), so `format` must run before `publish` and its output must reach Blogger
unmodified.

---

## 4. Data Model

Schema `app`, created by `migrations/versions/0001_initial.py`, extended by
`0002_telegram_totp.py`. Enums: `job_state`, `pub_state`, `pipeline_kind`.

| Table | Role | Notes |
|-------|------|-------|
| `topics` | Deduplicated topic text | `topic_hash` UNIQUE — a repeat `/topic` reuses the existing job |
| `jobs` | Queue + state machine | `state`, `attempt`/`max_attempts`, `priority`, `idempotency_key` UNIQUE, `thread_id` (LangGraph checkpoint key), `lease_owner`/`lease_expires_at`, `cancel_requested`, `mlflow_run_id`, `config_snapshot` JSONB, rolled-up `cost_usd`/`tokens_in`/`tokens_out`, `telegram_chat_id`/`telegram_msg_id`, `failure_class`/`failure_reason`. Partial index `ix_jobs_claim (priority, created_at)` |
| `node_runs` | Per-LLM-call ledger | provider/model/role, token counts incl. cache read/write columns, `cost_usd`, `latency_ms`, `mlflow_span_id`, `error_class`/`error_message` |
| `research_sources` | Search results | url/canonical/title/snippet/domain, `search_provider`, `search_query`, `rank`, `used_in_draft`, `credibility` |
| `drafts` | Versioned drafts | `version` 0 = first draft, N = Nth revision. `markdown`, `html`, `outline_json`, `seo_json`, `content_sha256`, `produced_by` |
| `revisions` | Revise audit trail | `from_draft_id` → `to_draft_id`, `reason`, `findings_json` |
| `publications` | Publish outcome | `platform`, `blog_id`, `remote_post_id`, `remote_url`, `state`, `is_draft`, `request_id`, `api_response` JSONB, `error_message` |
| `video_scripts`, `video_assets`, `video_renders`, `video_publications` | Video pipeline | **Schema only** — no code writes these |
| `telegram_totp_state` | TOTP replay protection | `last_step`, `failed_attempts`, `locked_until` |

**Job states:**
```
QUEUED ──► RUNNING ──┬──► PUBLISHED   graph completed (post is a Blogger DRAFT)
                     ├──► FAILED      exception and attempt >= max_attempts
                     ├──► BLOCKED     JobBlocked — needs a human (expired OAuth)
                     └──► CANCELLED   /cancel
```
On an exception with attempts remaining, the job is deliberately **left
RUNNING** — the lease expires and the next `claim_job()` resumes it from the
LangGraph checkpoint.

**Publication states:** `PENDING` (intent row, written before Google is
touched) → `DRAFT` (post exists remotely) or `FAILED`. `LIVE` exists in the
enum but nothing sets it — the pipeline never publishes publicly.

---

## 5. Configuration & Model Routing

`config/models.yaml` — roles as they actually stand:

| Role | Model | max_tokens | effort |
|------|-------|-----------|--------|
| `research_synth` | `anthropic:claude-sonnet-5` | 16000 | low |
| `outline` | `anthropic:claude-haiku-4-5` | 4000 | low |
| `draft` | `anthropic:claude-opus-5` | 32000 | **high** |
| `factcheck` | `anthropic:claude-opus-5` | 16000 | **high** |
| `seo` | `anthropic:claude-haiku-4-5` | 4000 | low |
| `script` | `anthropic:claude-haiku-4-5` | 8000 | low | *(video; unused)* |
| `search_tool` | `anthropic:claude-sonnet-5` | 16000 | low |

**Fallbacks:** `opus-5 → sonnet-5`; `haiku-4-5 → ollama:qwen3.5:9b`.
Fallback targets are registered as gateway endpoints too — a fallback that
exists in config but not in the gateway is worse than none, since it only
fails once the primary is already failing.
**Providers declared:** anthropic, openai, gemini, ollama. Only anthropic and
ollama are reachable from a role today.

**Effort mapping** (`llm/registry.py`): only `anthropic` + `effort: high`
produces anything — `thinking: {"type": "adaptive"}`. Never `budget_tokens`
(400s on Opus 5). Every other combination is a no-op, logged as
`effort_requested` vs `effort_effective`.

**Wrapping order matters and is enforced by convention:**
`build_llm(role)` returns the *bare* Runnable → call `.bind_tools()` /
`.with_structured_output()` on it → then `with_resilience(llm, role)` last.
`RunnableRetry` / `RunnableWithFallbacks` do not forward those methods.
`with_resilience` applies `.with_retry(stop_after_attempt=3,
wait_exponential_jitter=True)` then `.with_fallbacks(...)`; a fallback that
cannot even be constructed is logged and skipped rather than taking the
primary path down.

`llm/capabilities.py` holds a static per-provider capability table so nodes
can branch on a capability rather than a provider name. Nothing branches on it
yet.

**Secrets:** `SecretStore` (`secrets/store.py`) talks to TinyDBService over
HTTP. `registry._ensure_api_key()` used to pull the provider key and export it
as an env var for the LangChain chat classes to read; **it is now a no-op**,
because every call goes through the AI Gateway and the workers deliberately
hold no provider credential (§5C). `scripts/register_gateway.py` is the only
consumer of those TinyDB entries.

**Timeouts:** `defaults.timeout_s: 600` was dead config until the gateway
landed; it is now wired into every chat model constructor. An unset client
timeout in front of a proxy is an unbounded hang, not a slow call.

---

## 5A. Prompts — MLflow Prompt Registry

> Operator runbook — migration, editing, backup, failure modes:
> [PROMPT_REGISTRY.md](PROMPT_REGISTRY.md)

**The registry is the only source of truth for prompt text.** No prompt string
lives in Python. `agentic_blogger/prompts/` is the only way to get one.

| Piece | What it does |
|---|---|
| `prompts/registry.py` | `load(name)` / `render(name, variables, content)` against `prompts:/<name>@<alias>` |
| `prompts/specs.py` | manifest: every prompt, its allowed `{{variables}}`, its `response_format` |
| `prompts/preflight.py` | `check_all()` — run at job start, before any LLM spend |
| `scripts/register_prompts.py` | one-time bootstrap seeding the registry |
| `scripts/export_prompts.py` | backup-only mirror into `prompts_backup/` |

21 prompts are registered: one per message (system and user are separate
entries), plus conditional fragments, plus four JSON maps carrying the
Pydantic `Field(description=...)` text that `.with_structured_output()`
compiles into the tool schema.

### No fallback, on purpose

An unreachable registry **fails the job** (`PromptRegistryError` → state
`FAILED`), after the shared retry policy is exhausted. There is no bundled
default and no local cache we maintain. A job that ran on stale or absent
prompt text produces a plausible wrong post; a failed job produces an alert.

This is the one place the fail-open rule in `observability/mlflow_setup.py`
does not apply. Tracing still degrades silently — only prompts hard-fail.

`prompts_backup/` exists for disaster recovery and is **never read at
runtime**; `tests/test_prompt_backup_is_not_a_load_path.py` enforces that.

### Versioning

Prompts resolve by **alias** (`prompts.alias`, default `production`), per node,
at call time. Consequence: one job can legitimately span two prompt versions if
a prompt is republished mid-run, so there is no single job-level version.
What actually ran is recorded per node in `node_runs.prompts_json`:

```json
[{"name": "draft_user", "version": 7, "alias": "production"}]
```

`prompts.cache_ttl_seconds` (default 300) is set explicitly rather than
inheriting MLflow's implicit 60s alias cache. It is simultaneously the window
in which an edited prompt has not taken effect and the window in which a
registry outage stays invisible — one number, both meanings.

Schema descriptions are the exception: bound once per process and cached, so
editing one needs a worker restart.

### Editing a prompt

Edit it in the MLflow UI and move the alias. No deploy, no code change.
Re-running `register_prompts.py` pushes the seed texts as new versions and
would revert UI edits — it is a bootstrap and a recovery tool, not part of the
edit loop.

## 5B. Retry policy

`config/models.yaml` `resilience:` — 5 attempts, exponential backoff with
jitter, 1s to 30s — applied via `agentic_blogger/resilience.py` to Blogger
publishing and LLM invocations. LangChain's Runnable `.with_retry()` accepts
only an attempt count and a jitter flag, so the backoff bounds apply to the
Blogger path only.

**MLflow is excluded on purpose.** Its REST client retries internally; layering
ours on top produced 35 HTTP attempts per prompt and a >5-minute failure for an
unreachable registry. Prompt fetches call MLflow unwrapped, and its own
`MLFLOW_HTTP_REQUEST_*` knobs (set on the orchestrator in `docker-compose.yml`)
are the policy. Preflight also aborts on the first unreachable prompt rather
than checking all 21, so a full outage fails in ~17s instead of minutes.

Retry predicates stay with the call site: the Blogger client must never retry
an expired OAuth grant, and its retry deliberately re-runs the beacon reconcile
so a timed-out-but-succeeded insert is adopted rather than double-posted.

## 5C. LLM routing — MLflow AI Gateway

Every LLM call leaves the worker addressed to `http://mlflow:5000/gateway`.
There is no direct provider path and no bypass. Operator runbook:
[AI_GATEWAY.md](AI_GATEWAY.md).

**The workers hold no provider credential.** `llm/registry.py:_ensure_api_key()`
is a no-op while the gateway is enabled, so nothing writes an API key into
`os.environ`; clients are constructed with a placeholder key and a gateway
`base_url`. The real keys live encrypted in the gateway's store, pushed there
from TinyDB by `scripts/register_gateway.py`. TinyDB remains the source of
truth; the gateway holds a copy.

This is the **second fail-closed dependency** on MLflow, alongside the Prompt
Registry (§5A). Tracing stays fail-open. The rule is unchanged — fail open on
observability, fail closed on anything that changes what the pipeline *does* —
and a proxy that every call passes through is squarely the latter: a bypass
would mean spend that no budget sees and calls that no usage table records.
Measured failure time with the gateway down: 2.2s.

### Endpoint name == model id

Gateway endpoints are named after model ids (`claude-opus-5`), not roles. Both
gateway call surfaces read a request's `model` field as the *endpoint* name, so
this convention lets the clients keep sending what they always sent — and
`provider:model` keys and the per-node cost attribution
in `llm/callbacks.py` keep working with `base_url` as the only change.

`ollama:qwen3.5:9b` is the exception: endpoint names disallow colons, so it
registers as `qwen3.5-9b`. `llm/gateway.py:endpoint_name()` is the single
function that rewrites it, imported by both the registration script and the
client construction path — the two must agree or every call 404s.

### Two surfaces, one reason

| Provider | Surface | Path |
|---|---|---|
| anthropic | passthrough | `/gateway/anthropic/v1/messages` |
| gemini, openai, ollama | OpenAI-compatible | `/gateway/mlflow/v1/chat/completions` |

Anthropic uses the passthrough because three things the pipeline depends on
exist only in the provider-native body and would be lost to a translating
proxy: the `web_search_20260209` / `web_fetch_20260318` **server tools** that
are the research node, **adaptive thinking** block lists that
`llm/text.py:extract_text` parses, and the **prompt-cache token buckets** that
the gateway prices separately. All three verified intact through the
gateway; `scripts/smoke_gateway.py` asserts each one so a regression is caught
rather than silently degrading cost reporting.

### Usage, cost, budget

Endpoints are registered with `usage_tracking: true`, giving per-endpoint token
/ cost / latency in the AI Gateway UI plus `gateway/<endpoint>` traces in the
same experiment as the job runs.

**The gateway is the only thing that prices a call.** No rate table, price
catalog client, or cost formula exists in this repo any more. `llm/cost.py`
reads the gateway's own `total_cost` back per call — filtered to its
`provider/<provider>/<model>` span, over the call's wall-clock window — and
`llm/callbacks.py` writes it to `node_runs.cost_usd`. The job ledger and the
budget are therefore the same number by construction, verified against the
gateway's budget tracker delta.

This depends on two things worth knowing. The mlflow service runs with
`MLFLOW_ENABLE_ASYNC_TRACE_LOGGING: "false"`, without which the cost row lands
~4.7s after the response. And attribution is by time window, so it is exact
only while the orchestrator stays a single-replica worker — see
[AI_GATEWAY.md §5](AI_GATEWAY.md) before scaling it out.

Budget policies are enforced per uvicorn worker under the default `local`
tracker strategy, so the gateway runs `--workers 1`. That happens to coincide
with the memory finding already recorded against the MLflow service — but they
are independent reasons, and raising the worker count requires a Redis budget
tracker first.

## 6. Observability

`observability/mlflow_setup.py` — **fail-open by construction**: every entry
point catches broadly and returns a `nullcontext`, so an MLflow outage costs
traces, never a job.

- Tracking URI `MLFLOW_TRACKING_URI` (default `http://mlflow:5000`), experiment
  `agentic-blogger/blog`, `mlflow.langchain.autolog()` on.
- `start_job_run(job_id, resumed)` wraps the whole graph invocation; tags
  `job_id`, `resumed`.
- `log_job_metrics()` writes `total_cost_usd` / `total_tokens_in` /
  `total_tokens_out` after a successful run.

**Cost tracking is not a LangChain callback.** `llm/callbacks.py:track_llm_call`
is a context manager applied per call site: `.with_fallbacks()` /
`.with_retry()` wrap the Runnable opaquely enough that a global handler cannot
attribute usage back to `(job_id, node_name)`. Each node does:

```python
with track_llm_call(job_id, node_name, role, spec["model"]) as record:
    response = llm.invoke([...])
    record(response)
```

On success it reads `response.usage_metadata`, prices it via
`llm/cost.py`, writes a `node_runs` row and rolls
the cost into `jobs`. On an exception it still writes a `node_runs` row
carrying `error_class`/`error_message`, then re-raises.

Cost is a local estimate from a static rate table, never billing truth.

### 6A. Log tracing

`observability/log_setup.py` is the single logging entry point for both
service containers (`setup_logging("orchestrator")` / `setup_logging("telegram")`).
It exists because the logs previously could not reconstruct a run: eight nodes,
two LLM roles in places, the prompt registry, Postgres and Blogger each logged
from their own module with no shared key, so correlation meant guessing from
timestamps.

**Correlation.** `bind_job(job_id)` and `bind_node(name)` set ContextVars; a
handler-level `ContextFilter` stamps them onto every record, including records
from libraries that know nothing about this application. Lines render as:

```
2026-09-09 16:41:02,118 [INFO] agentic_blogger.graph.builder [job=99639782-… node=draft]: done in 41.2s out: draft_markdown=18244ch draft_title=63ch draft_id=…
```

The binding happens **inside** the node wrapper, not around
`compiled_graph.invoke()`: LangGraph runs sync nodes on an executor, and a
ContextVar set on the calling thread is not guaranteed to reach the worker
thread. The lease heartbeat thread binds its own for the same reason.

**Node entry/exit** is central, in `graph/builder.py:_traced` — applied when
each node is registered, so a node added later cannot forget it. Entry logs the
input keys that node reads (`_INPUTS`) as sizes; exit logs elapsed time and the
returned delta. `_traced_router` logs which branch `needs_revision` chose and
why. Sizes rather than contents: a draft is tens of kilobytes and the signal is
that it exists and roughly how big it got.

**Redaction.** A `RedactingFilter` on the handler rewrites credential-shaped
substrings out of every record before it is emitted — the Telegram bot token in
a URL path (`/bot<id>:<secret>`, the numeric id is kept), `Bearer` values, and
`key=value` pairs whose key looks secret (via `observability/scrub.py`). This is
what allows `httpx` to run at INFO again: the per-request line is the only
cheap evidence that a call actually left the container, and it was previously
silenced wholesale because the token was in it. Redaction is a backstop, not a
licence — never pass a secret to a logger on purpose.

**Poll noise.** `PollNoiseFilter` drops successful Telegram `getUpdates` lines
(~8.6k/day, all identical) and replaces them with one summary line every 30
minutes; a poll that comes back non-2xx is still printed, as are `getMe`,
`deleteWebhook`, `sendMessage` and every non-Telegram httpx call. The summary
exists so a quiet bot is distinguishable from a dead one — it is produced by
rewriting a poll record in place rather than emitting a new one, because
logging from inside a filter reenters the handler.

**Levels.** `LOG_LEVEL` (compose, default `INFO`) sets the application level.
Library loggers are floored independently in `_LIBRARY_LEVELS`, so `LOG_LEVEL=DEBUG`
adds prompt-resolution and payload detail without unleashing langchain/httpcore
internals. `setup_logging` uses `basicConfig(force=True)` so a library that
configured logging first cannot leave a second, *unfiltered* handler attached.

An idle orchestrator logs one line every 5 minutes (`IDLE_LOG_EVERY_S`) —
silence was previously indistinguishable from a dead worker.

---

## 7. Ingestion: Telegram Bot

`ingestion/telegram_bot.py` imports only `db/`, `config/`, `secrets/`,
`security/` — never graph or node code, so the two containers stay independent.

| Command | Effect |
|---------|--------|
| `/topic <text>` (or bare text) | `create_topic_and_job()`; replies with the job id, or reports the existing job if the topic hash already exists |
| `/url <link> [angle]` | Queues a URL-sourced job; `link` must be a leading http(s) URL, max 250 chars. Also reachable by pasting a bare URL as the first word of `/topic` |
| `/status <job_id>` | State, attempt, cost so far, MLflow link, failure reason |
| `/queue` | QUEUED + RUNNING jobs |
| `/cancel <job_id>` | `request_cancel()` |
| `/retry <job_id>` | Re-queues a FAILED/BLOCKED job; resumes from its checkpoint |
| `/cost [today\|week]` | Aggregate spend over the window |
| `/models` | Current role → model mapping |

**Auth is two-factor on every single message** (`restricted` decorator):

1. `update.effective_user.id` must appear in `TELEGRAM_ALLOWED_USER_IDS`, read
   from **TinyDB** (refreshed per call), not from the environment.
2. The message must **lead with a fresh, unused 6-digit TOTP code**. `security/
   totp.py:verify_and_consume` checks the code and records the matched step in
   `telegram_totp_state`, so a replayed code inside the same 30-second window is
   rejected. Repeated failures set `locked_until`.

The decorator strips the code and normalizes both entry paths (command args vs
bare text) back into `context.args`, so handlers parse arguments unchanged —
`update.message` is frozen in PTB v20+ and cannot be rewritten.

**Code position differs by entry path:** `CommandHandler` strips the command
first, so a slash command reads `/topic <code> <text>`; bare text reads
`<code> <text>`.

Enrollment: `scripts/setup_telegram_totp.py`.

Full command reference, setup steps, and an end-to-end test walkthrough:
[TELEGRAM_USAGE.md](TELEGRAM_USAGE.md).

---

## 8. Publishing: Google Blogger

`nodes/publish.py` + `publishing/blogger_client.py`.

**Posts land as drafts.** `posts.insert(..., isDraft=True)`. Nothing in the
codebase flips a post live; that is a manual step in Blogger today.

**Why the beacon exists:** Blogger's `posts.insert` accepts no idempotency
key, and a crash between the API call and the checkpoint write leaves no local
record. The beacon comment that `format` embedded in the HTML is the only
thing that survives into the remote object, so it is what distinguishes
"timed out but actually succeeded" from "genuinely failed".

**Sequence:**

1. `get_pending_publication(job_id)` — if already `DRAFT`/`LIVE`, return it and
   skip everything below.
2. `insert_publication_intent(...)` — the `PENDING` row is committed **before**
   Google is touched.
3. `publish_draft(job_id, title, html, labels)`:
   - `_service()` refreshes the OAuth credential from TinyDB; `invalid_grant`
     is re-raised as `InvalidGrantError`.
   - `_find_by_beacon()` lists `status=["DRAFT"]` posts and adopts one whose
     content carries `agentic-blogger:job={job_id}` (or whose title matches).
   - Otherwise `posts.insert(isDraft=True, fetchImages=False)
     .execute(num_retries=0)`. `num_retries=0` is mandatory: googleapiclient's
     built-in retry does a blind re-POST, which is precisely the double-post
     this design prevents.
4. `update_publication(state="DRAFT", remote_post_id, remote_url, is_draft=True,
   api_response=...)`.

**Retry policy** (`tenacity`, 5 attempts, exponential jitter 2→30s) wraps the
**whole reconcile-then-insert** function, not just the insert — so every retry
re-checks the beacon first. Retryable statuses are
`{400, 403, 429, 500, 502, 503, 504}`. The 400 is not a mistake: Google-side
throttling was observed empirically to surface as HTTP 400
`reason=badRequest` ("Request contains an invalid argument") after heavy
`posts.insert` traffic, with identical minimal payloads failing uniformly once
volume climbed.

**Failure handling:** `InvalidGrantError` marks the publication `FAILED` and
raises `JobBlocked` → the runner marks the job `BLOCKED`. Recovery is
`scripts/blogger_authorize.py` (stdlib loopback flow). Any other exception
marks the publication `FAILED` and propagates as a normal failure.

---

## 9. Current Work & Known Gaps

### 9.1 Uncommitted in the working tree

| Path | Change |
|------|--------|
| `agentic_blogger/nodes/research.py` | Tier-stratified two-pass research; source/exemplar attribution enforcement |
| `agentic_blogger/nodes/schemas.py` | `TierBrief`, `CodeExemplar`, expanded `ResearchBrief`, tier on `OutlineSection` |
| `agentic_blogger/graph/state.py` | State docs/shape for the expanded brief |
| `agentic_blogger/nodes/format.py` | markdown-it + bleach + beacon/factcheck comments |
| `config/models.yaml` | `research_synth` moved to sonnet-5 @16K |
| `assets/disclaimer.txt` | Deleted — nothing references it; `format` composes its HTML entirely from the Markdown plus the two comments |
| `scripts/probe_blogger_permalink.py` | Untracked; Blogger permalink investigation |
| `docs/` | Untracked; this document |

### 9.2 Wired but never exercised

These exist and would work, but no caller reaches them:

- `jobs.cancel_requested` — `/cancel` sets it; no node or loop reads it, so a
  running job finishes regardless.
- `jobs.mlflow_run_id` — `/status` renders a link from it, but nothing ever
  writes it (`mark_job_state` accepts the argument; the runner never passes
  one). **The MLflow link never appears.**
- `node_runs.mlflow_span_id` — column and parameter exist; always NULL.
  (`node_runs.prompts_json`, added in migration 0003, *is* populated.)
- `observability/scrub.py` is now wired — but only into the log handler
  (§6A). With `mlflow.langchain.autolog()` on, prompts and responses still
  reach **MLflow** unredacted; `scrub_dict` has no caller.
- `jobs.telegram_msg_id` — never written; no completion notification is pushed
  back to Telegram. Status is pull-only via `/status`.
- `llm/capabilities.py` — no caller branches on it.

### 9.3 Pending / not implemented

| Item | Status |
|------|--------|
| **Publish live** | `posts.insert(isDraft=True)` is hardcoded; `pub_state.LIVE` is unreachable |
| **Permalink control** | Investigating via `scripts/probe_blogger_permalink.py`. `seo_meta.slug` is generated and stored but never sent to Blogger |
| **Video pipeline** | Four tables + `pipeline_kind` enum + `parent_job_id` + a `script` role exist; `video/` package is empty |
| **OpenAI / Gemini** | Providers and capability entries declared; no role targets them |
| **Automated tests** | No `tests/` directory. `scripts/smoke_*.py` (llm, graph, search, mlflow, blogger) are the only verification path |
| **Horizontal scaling** | One replica by design (`worker.py` docstring): the secrets-vault concurrent-write constraint plus the missing lease heartbeat |
| **Cross-service tracing** | Telegram → Orchestrator is DB rows only; no propagated trace context |
| **Images / diagrams** | Text-only by decision. `img` is in the bleach allowlist, but nothing generates or hosts images |

### 9.4 Operational note

The Blogger refresh token expires around **2026-09-13**. On expiry every job
reaching `publish` fails with `JobBlocked` and lands in `BLOCKED`. Recovery is
`scripts/blogger_authorize.py` (stdlib loopback), then `/retry` — the jobs
resume from their checkpoints and re-enter `publish`, where the beacon
reconcile prevents double-posting.

---

## 10. Tech Stack

- **Python 3.12**; LangChain 0.3.x, LangGraph 0.2.x, `langgraph-checkpoint-postgres` 2.x
- **Pydantic 2.9.2** for structured outputs; **SQLAlchemy 2.0** + `psycopg` for DB access
- **markdown-it-py** + **bleach** for the format node; **tenacity** for Blogger retries
- **python-telegram-bot 21.8**; **pyotp** + **qrcode** for TOTP
- **google-api-python-client** / **google-auth** for Blogger
- **MLflow 3.16.0** (`mlflow[genai]` — the AI Gateway's provider adapters and
  encrypted credential store ship in that extra); **TinyDB 4.8.0** behind the
  vault service
- **PostgreSQL 16**, **Alembic**, Docker Compose (5 services)
- Dev: pytest, pytest-asyncio, black, ruff (no test suite present yet)

---

## 11. Running It

```bash
# 1. Configure — see .env.example
#    Secrets live in TinyDB, not .env: ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN,
#    TELEGRAM_ALLOWED_USER_IDS, BLOGGER_CLIENT_ID/CLIENT_SECRET/REFRESH_TOKEN/BLOG_ID

#    ./scripts/seed_secrets.sh also generates MLFLOW_CRYPTO_KEK_PASSPHRASE,
#    which the MLflow container refuses to start without.

# 2. Start
docker compose up -d

# 3. Migrate
docker exec agentic-blogger-orchestrator alembic upgrade head

# 3a. Register the AI Gateway — until this runs there is no path to a provider
make register-gateway

# 4. Enrol TOTP for your Telegram user
python scripts/setup_telegram_totp.py

# 5. Authorize Blogger (writes the refresh token into TinyDB)
python scripts/blogger_authorize.py

# 6. Message the bot — TOTP code goes AFTER the command:
#    "/topic <6-digit-code> What are embeddings in vector DBs?"
docker logs -f agentic-blogger-orchestrator
```

**Smoke scripts:** `scripts/smoke_llm.py`, `smoke_gateway.py`, `smoke_search.py`,
`smoke_graph.py`, `smoke_mlflow.py`, `smoke_blogger.py`. `make smoke` runs
prompts, mlflow, gateway, llm, search and blogger.

---

## 12. File Map

| Path | Purpose |
|------|---------|
| `graph/builder.py` | Node registry, edges, the `factcheck ⇄ revise` conditional loop |
| `graph/runner.py` | Claim → invoke → mark state; `JobBlocked` handling |
| `graph/state.py` | `BlogState` TypedDict |
| `orchestrator/worker.py` | Poll loop; owns the `PostgresSaver` for the process lifetime |
| `nodes/*.py` | research, outline, draft, factcheck, revise, seo, format, publish |
| `nodes/schemas.py` | `ResearchBrief`, `TierBrief`, `CodeExemplar`, `Outline`, `FactCheckReport`, `SeoMeta` |
| `db/repo.py` | All SQL. Claiming, leases, ledgers, drafts, revisions, publications, TOTP state |
| `db/engine.py` | Engine + `psycopg_url()` for the checkpointer |
| `llm/registry.py` | Role → Runnable; effort mapping; gateway routing; retry + fallback wrapping |
| `llm/gateway.py` | `endpoint_name()` — the single model-id → gateway-endpoint rule |
| `llm/callbacks.py` | `track_llm_call` — per-call cost/latency ledger |
| `llm/cost.py` | Reads each call's cost back from the AI Gateway — the only cost source; no local rate table |
| `llm/capabilities.py` | Static provider capability table (unused) |
| `config/loader.py`, `config/models.yaml` | Role/model/fallback config |
| `publishing/blogger_client.py` | OAuth, beacon reconcile, tenacity retry, insert |
| `secrets/store.py` | TinyDBService HTTP client |
| `security/totp.py` | TOTP verify + replay consumption |
| `observability/mlflow_setup.py` | Fail-open MLflow wiring |
| `observability/scrub.py` | Redaction helpers (wired into the log handler; not into MLflow) |
| `observability/log_setup.py` | Logging entry point: job/node correlation, secret redaction, level policy |
| `ingestion/telegram_bot.py` | Commands, TOTP + allowlist gate |
| `migrations/versions/` | `0001_initial.py`, `0002_telegram_totp.py` |

---

## 13. Design Decisions Worth Knowing

1. **Job claiming and durable execution are separate.** LangGraph's
   checkpointer resumes a node sequence; it says nothing about which job to
   pick up. The claim loop in `db/repo.py` stays ours.
2. **Fact-check advises, it does not gate.** At most one revision pass, then
   the post ships with the finding counts embedded in the HTML comment.
3. **Fail-open on observability, fail-closed on anything that changes what the
   pipeline does.** MLflow tracing going down never fails a job. Its Prompt
   Registry and its AI Gateway both do, because a job that ran on absent prompt
   text or bypassed the budget is worse than a job that did not run. Separately,
   a code exemplar without a source URL is dropped before it can reach a draft.
4. **The beacon is the idempotency key** Blogger does not give us.
5. **Provider swaps are config, not code** — one line in `models.yaml`, which
   is why nodes take a role name and never a model string.
6. **The workers hold no provider credential.** Keys live encrypted in the AI
   Gateway; TinyDB is the source of truth and `make register-gateway` pushes
   them across. `grep -E 'ANTHROPIC|OPENAI|GEMINI'` on the orchestrator's
   environment returning nothing is the invariant, and it is what makes "no
   call bypasses the gateway" enforceable rather than merely intended.
7. **Gateway endpoint names are model ids**, not role names — both call
   surfaces resolve a request's `model` field to an endpoint, so this keeps
   `provider:model` cost attribution working with `base_url` as the only
   change (§5C).
