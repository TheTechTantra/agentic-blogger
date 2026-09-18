# 3. Observability: Fail Open on Seeing, Fail Closed on Doing

*Part 3 of the [Agentic Blogger series](README.md).*

An LLM pipeline is an unusually hostile thing to observe. The work is
non-deterministic, the failures are frequently *semantic* rather than thrown, the
unit of work spans minutes, and every step costs money that is invisible unless
you go looking for it.

This system observes itself at four layers. The rule governing all of them is
one sentence.

---

## The rule

> **Fail open on observability. Fail closed on anything that changes what the
> pipeline does.**

MLflow plays three roles here — tracing server, Prompt Registry, and AI Gateway
— and the rule splits them cleanly:

| MLflow role | Outage behaviour | Why |
|---|---|---|
| Tracing | Silent degradation, job continues | Losing a trace costs a trace |
| Prompt Registry | **Job fails** (`PromptRegistryError` → `FAILED`) | A job that ran on absent prompt text produces a *plausible wrong post* |
| AI Gateway | **Job fails** (measured: 2.2s) | A bypass means spend no budget sees and calls no usage table records |

The distinction is between losing information about the work and changing the
work. Only the first is safe to swallow.

Tracing is fail-open by construction — every entry point catches broadly and
hands back a `nullcontext`:

```python
def start_job_run(job_id: str, resumed: bool = False):
    """Returns an active mlflow run context manager, or a no-op context
    manager if MLflow is unavailable. Never raises."""
    if not init_mlflow():
        return contextlib.nullcontext()

    try:
        run_ctx = mlflow.start_run(run_name=f"job-{job_id}")
        mlflow.set_tag("job_id", job_id)
        mlflow.set_tag("resumed", str(resumed))
        return run_ctx
    except Exception:
        logger.exception("Failed to start MLflow run — continuing without tracing")
        return contextlib.nullcontext()
```

The prompt loader states the opposite contract just as explicitly:

```python
"""The registry is the single source of truth for every prompt this pipeline
sends to a model. There is deliberately NO fallback: no bundled defaults, no
local cache we maintain, no last-known-good copy. If the registry cannot be
reached the job fails and someone fixes the registry."""
```

There is even a small structural detail protecting the boundary: the prompt
module sets its own tracking URI rather than sharing the tracing module's setup,
*"so a tracing init failure (which is swallowed by design) can never leave
prompt loading pointed at the wrong server."*

---

## Layer 1 — Traces (MLflow)

One MLflow run per job, wrapping the whole graph invocation:

```python
with start_job_run(job_id, resumed=resumed), _lease_heartbeat(job_id, self.worker_id):
    ...
    final_state = self.compiled_graph.invoke(initial_state, config=config)
```

`mlflow.langchain.autolog()` fills in the spans beneath it — every chain,
every model call, prompts and responses. Gateway-side, endpoints registered with
`usage_tracking: true` add `gateway/<endpoint>` traces to the *same* experiment,
so the client's view of a call and the proxy's view sit side by side.

Job-level metrics land at the end:

```python
mlflow.log_metric("total_cost_usd", float(cost_usd))
mlflow.log_metric("total_tokens_in", tokens_in)
mlflow.log_metric("total_tokens_out", tokens_out)
```

---

## Layer 2 — The ledger (Postgres)

Traces are for looking at one run. A relational ledger is for asking questions
across all of them, and this is where the system's most useful data lives.

`node_runs` — one row per LLM call:

| Column group | Contents |
|---|---|
| identity | `job_id`, `node_name`, `role` |
| routing | `provider`, `model` |
| usage | `tokens_in`, `tokens_out`, cache read/write buckets |
| money | `cost_usd` (from the gateway, see below) |
| timing | `latency_ms` |
| prompts | `prompts_json` — names and versions actually resolved |
| failure | `error_class`, `error_message` |

`jobs` carries the rolled-up totals, plus `attempt`/`max_attempts`,
`lease_owner`/`lease_expires_at`, `failure_class`/`failure_reason`, and a
`config_snapshot` JSONB.

Then `research_sources`, `drafts` (versioned — 0 is the first draft, N the Nth
revision), `revisions` (from-draft → to-draft plus the findings that caused it),
and `publications`.

Together those answer questions a trace viewer cannot: *what did last week cost
by node?* *which role fails most?* *did the revision actually change anything?*
*what sources has this blog cited, ever?*

### The line in the ledger that makes the numbers trustworthy

`node_runs.cost_usd` is not an estimate. It is the gateway's own figure, read
back per call:

```python
"""Nothing in this repo prices a token. The gateway is the only component that
computes cost, and this module reads its number — it does not recompute,
cross-check, or fall back to a local table. There is one place spend is
decided, and it is the same place the budget is enforced, so `node_runs.cost_usd`
and the gateway's own usage and budget figures cannot disagree."""
```

That property — **the ledger and the budget are the same number by
construction** — is worth more than a slightly more accurate rate table, because
a rate table drifts from provider pricing silently and a discrepancy between
"what we recorded" and "what the budget saw" is very hard to debug after the
fact.

### Prompt attribution, and why it drains

Which prompt version produced a given call is a question you only ask after a bad
output, which is exactly when it is too late to add instrumentation. So prompts
record themselves as they resolve:

```python
# Which prompts have been resolved but not yet attributed to a node_run.
#
# Drain-based rather than a context manager wrapping the LLM call, because
# nodes render their prompts *before* entering track_llm_call — a collector
# scoped to the invocation would capture nothing. load() always appends here;
# track_llm_call drains at record time, so a node_run row gets exactly the
# prompts resolved since the previous row was written.
#
# Thread-local: LangGraph may run nodes on worker threads, and one job's
# prompts must never land on another's row.
_local = threading.local()
```

Result, per call:

```json
[{"name": "draft_user", "version": 7, "alias": "production"}]
```

There is a matching cleanup in preflight, which resolves all 21 prompts at job
start and would otherwise dump every one of them onto whichever node writes the
first row:

```python
# Preflight touches every prompt; leaving them in the accumulator would
# attribute all of them to whichever node writes the first node_run.
drain()
```

---

## Layer 3 — Logs you can actually reconstruct a run from

The original problem, stated in the module docstring:

> A job touches eight nodes, two LLM roles per node in places, the prompt
> registry, Postgres and Blogger, and every one of those logged from its own
> module with no shared key. Reconstructing a run meant guessing from timestamps.

### Correlation

Two ContextVars and a handler-level filter:

```python
_job_id: ContextVar[str] = ContextVar("job_id", default="")
_node: ContextVar[str] = ContextVar("node", default="")
```

Stamped by a `ContextFilter` onto **every** record passing the root handler —
including records from libraries that know nothing about this application. Lines
come out like:

```text
2026-09-09 16:41:02,118 [INFO] agentic_blogger.graph.builder [job=99639782-… node=draft]: done in 41.2s out: draft_markdown=18244ch draft_title=63ch draft_id=…
```

The subtle part is *where* the binding happens:

> The binding happens **inside** the node wrapper, not around
> `compiled_graph.invoke()`: LangGraph runs sync nodes on an executor, and a
> ContextVar set on the calling thread is not guaranteed to reach the worker
> thread.

The lease heartbeat thread has the same problem and the same fix:

```python
def beat():
    # The heartbeat runs on its own thread, so it needs its own binding —
    # ContextVars are per-thread and this one starts empty.
    with bind_job(job_id):
        _beat_loop()
```

**If you take one thing from this post:** context propagation into framework
worker threads is not automatic, and the symptom — logs that are correct but
unlabelled — is easy to not notice for weeks.

### Redaction

The Telegram bot token is a *path segment* in every API URL. So `httpx` at INFO
printed the credential ~8,600 times a day, which is why it had been silenced
entirely — and silencing it cost the only cheap evidence that a call actually
left the container.

Redaction restores the useful half:

```python
# Telegram puts the bot token in the URL path: /bot<id>:<hash>/getUpdates.
# Keep the numeric id — it identifies which bot without being the credential —
# and drop the secret half.
_TELEGRAM_TOKEN_RE = re.compile(r"(/bot\d{5,}):[A-Za-z0-9_-]{20,}")
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]{20,}")

# Cheap prefilter: skip the regex work on the overwhelming majority of lines
# that contain nothing secret-shaped.
_SUSPECT = re.compile(r"(?i)/bot\d|bearer |token|secret|api[_-]?key|authorization")
```

Keeping the numeric bot id is a nice piece of design: you retain the ability to
tell *which* bot, and lose only the secret.

One implementation trap, handled:

```python
if redacted != original:
    # Collapse args into the message: they have already been
    # interpolated, and leaving them would re-interpolate a string
    # that no longer has placeholders.
    record.msg = redacted
    record.args = ()
```

And the honest caveat, stated twice in the codebase: *redaction is a backstop,
not a licence — never pass a secret to a logger on purpose.*

### Noise control without going silent

Long polling produces one identical httpx line per round trip. Dropping them all
would recreate the dead-or-idle ambiguity:

```python
class PollNoiseFilter(logging.Filter):
    """Collapse successful Telegram long-poll lines into a periodic summary."""
```

```text
telegram long-poll alive: 178 successful getUpdates in the last 30m (individual lines suppressed)
```

Non-2xx polls still print, as do `getMe`, `deleteWebhook`, `sendMessage` and
every non-Telegram httpx call. The summary is produced by rewriting a poll record
in place — *"logging from inside a filter reenters the handler."*

The worker has the mirror-image heartbeat:

```python
# An idle worker logs nothing, which is indistinguishable from a dead one —
# and that ambiguity cost real time during the last incident.
IDLE_LOG_EVERY_S = 300
```

**Silence is not a signal.** Both directions of this system spend a line every
few minutes to make sure it never has to be interpreted.

### Level policy

`LOG_LEVEL` sets the application level; library loggers are floored
independently in `_LIBRARY_LEVELS`, so `LOG_LEVEL=DEBUG` gets you
prompt-resolution and payload detail *without* unleashing langchain and httpcore
internals. And:

```python
setup_logging  # uses basicConfig(force=True)
```

> so a library that configured logging first cannot leave a second,
> *unfiltered* handler attached.

That is a genuinely nasty failure mode: your redaction filter is installed, and
the secret still prints, from the handler you did not know existed.

---

## Layer 4 — Domain-level signals

Generic infrastructure telemetry does not tell you the *content* pipeline is
healthy. These lines do:

```python
logger.info("factcheck findings=%d verdicts=%s", ...)
logger.info("flagged [%s] %r (source=%s)", f.get("verdict"), (f.get("claim") or "")[:160], ...)
logger.info("revised words %d -> %d (%+d)", ...)
logger.info("brief tiers=%d exemplars=%d contested=%d format=%s brief_len=%dch", ...)
logger.info("seo title=%r (%dch) labels=%d/%dch slug=%r meta_description=%dch", ...)
logger.info("formatted markdown=%dch html=%dch (sanitizer dropped %dch) ...", ...)
```

Each is a trend line for a *semantic* failure that never raises: fact-check that
stops finding anything, a revision that changes nothing, a research brief that
quietly stops producing code exemplars, a sanitizer that starts eating content.

Warnings mark the same class of silent degradation:

```python
logger.warning("research produced no extractable sources")
logger.warning("dropped %d unattributed code exemplars", ...)
logger.warning("seo returned %d labels — dropped %d over Blogger's count cap: %s", ...)
logger.warning("research_synth returned no parsed brief — falling back to prose")
```

Every one of those is a path where the job still succeeds and the output is
worse. In an LLM pipeline that is the *normal* failure, and nothing else will
tell you about it.

---

## What is not observable yet

Honest gaps, all of them recorded in the architecture doc:

- **`jobs.mlflow_run_id` is never written.** `/status` renders a link from it,
  `mark_job_state` accepts the argument, the runner never passes one. So the
  MLflow link never appears. Smallest-effort, highest-value fix in the repo.
- **`node_runs.mlflow_span_id` is always NULL.** No trace↔ledger join.
- **`scrub_dict` has no caller.** Redaction covers logs; with
  `mlflow.langchain.autolog()` on, prompts and responses still reach MLflow
  unredacted.
- **No cross-service trace context.** Telegram → orchestrator is DB rows only.
- **No alerting.** Everything is pull — `docker logs`, the MLflow UI, `/status`,
  `/cost`. Nothing pushes when a job lands in `FAILED` or `BLOCKED`, and
  `jobs.telegram_msg_id` (which exists for exactly that) is never written.

---

## Six transferable lessons

1. **Decide per dependency whether it fails open or closed, and write the
   decision in the module docstring.** "MLflow" is not one dependency — it is
   three, with three different answers.
2. **Instrument at registration, not in the function.** The node that forgets to
   log is the one that breaks.
3. **Log sizes and counts, not payloads.** Enough to reconstruct, cheap enough to
   always be on.
4. **Bind context inside the worker thread.** Frameworks run your code somewhere
   you did not choose.
5. **Redact at the handler so libraries are covered too** — and prefilter, since
   it runs on every record.
6. **Watch the semantic signals.** In an LLM pipeline, the failures that matter
   most rarely raise.

---

**Next:** [Reliability and Idempotency](04-reliability-and-idempotency.md) — leases, checkpoints, and giving Blogger an idempotency key it refuses to provide.
