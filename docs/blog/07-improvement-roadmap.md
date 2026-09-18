# 7. What I'd Improve Next

*Part 7 of the [Agentic Blogger series](README.md).*

A ranked backlog for a system that works. Each item says what is wrong, where the
code is, roughly what it costs, and — for the ones that matter — why it has to
happen in a particular order.

Ranked by **value per unit of effort**, not by ambition.

---

## Tier 1 — Small, and you will notice immediately

### 1.1 Write `jobs.mlflow_run_id`

The highest value-to-effort item in the repo, and it is a two-line fix.

The Telegram `/status` command already renders an MLflow link:

```python
if job.get("mlflow_run_id"):
    lines.append(f"MLflow: {MLFLOW_PUBLIC_URL}/#/experiments/0/runs/{job['mlflow_run_id']}")
```

`repo.mark_job_state()` already accepts `mlflow_run_id`. The runner never passes
one, so the column is always NULL and **the link never appears.**

Fix: capture the run id from `start_job_run()` and pass it through.

- **Effort:** ~1 hour
- **Value:** one tap from a job id to its full trace

### 1.2 Push a notification when a job finishes

Everything today is pull — `/status`, `/cost`, `docker logs`. A job that lands in
`FAILED` or `BLOCKED` at 3am is discovered whenever you next think to ask.

`jobs.telegram_chat_id` is written. `jobs.telegram_msg_id` exists and is never
written. The plumbing is half there.

Fix: on terminal state, `sendMessage` to the originating chat with state, cost,
and the post URL or the failure reason.

- **Effort:** half a day
- **Value:** turns an operator-polled system into one that tells you

### 1.3 Honour `jobs.cancel_requested`

`/cancel` sets the flag, and `claim_job` respects it — but only when *claiming*:

```sql
WHERE (state = 'QUEUED' OR (state = 'RUNNING' AND lease_expires_at < now()))
  AND cancel_requested = false
```

Nothing reads it mid-run, so a running job finishes regardless — including the
Opus draft you just tried to stop paying for.

Fix: check it in `_traced` between nodes and raise a `JobCancelled` the runner
maps to state `CANCELLED`. Node-boundary granularity is the right resolution —
you cannot cancel mid-call anyway, and the checkpoint makes the boundary a
natural stopping point.

- **Effort:** 2 hours
- **Value:** `/cancel` stops meaning "cancel eventually"

### 1.4 Redact what reaches MLflow

`observability/scrub.py` provides `scrub_dict`. **Nothing calls it.**

`mlflow.langchain.autolog()` captures full prompts and responses, so anything
secret-shaped that ever entered a prompt is sitting in a trace unredacted. Logs
are covered; traces are not.

Fix: a trace processor applying `scrub_dict` to span inputs/outputs.

- **Effort:** half a day
- **Value:** closes the one place a secret could still land at rest

### 1.5 Fix the documentation drift

Three things in `docs/ARCHITECTURE.md` no longer match the code:

- §6 says *"Cost is a local estimate from a static rate table, never billing
  truth."* That is the old design. §5C of the same document describes the
  current one correctly — cost is read back from the gateway and there is no
  rate table in the repo.
- §9.3 says *"No `tests/` directory."* There is one, with
  `test_prompt_backup_is_not_a_load_path.py` in it.
- §9.1 lists as uncommitted a set of changes now committed in `41a9faa`,
  `be27a83` and `67a2fc9`.

A doc that is 90% right is more dangerous than one that is obviously stale,
because you stop checking.

- **Effort:** 30 minutes

---

## Tier 2 — Real work, high payoff

### 2.1 A test suite

One test file exists, and it guards an architectural invariant rather than
behaviour. Verification is otherwise `scripts/smoke_*.py`, which need a live
stack, real credentials, and real spend.

What is missing, in priority order:

| Target | Why it is testable without a model |
|---|---|
| `needs_revision()` | Pure function over a dict — the entire branching logic of the graph |
| `_extract_text_and_sources()` | Pure parser; recorded fixtures for every shape, including the dict-shaped error block and the nested code-execution case |
| `extract_text()` | Pure; the adaptive-thinking block list is the whole bug class |
| `_fit_labels()` | Pure; Blogger's comma-counted char cap has off-by-one written all over it |
| `scrub_text` / `_redact` | Pure; and this is a security control that already silently broke once |
| `claim_job` concurrency | Postgres in a fixture; assert two workers never claim the same row |
| Beacon reconcile | Mock the Blogger service; assert a second attempt adopts rather than inserts |
| `cost_for_call` | Mock the metrics endpoint; assert the span-name filter prevents double-counting |

Almost none of that needs an LLM. The deterministic parts of this system are
where the sharp edges live — which is the general rule for LLM pipelines, and the
reason "you can't test AI" is a bad excuse.

- **Effort:** ~1 week for meaningful coverage
- **Value:** the difference between changing this confidently and not

### 2.2 Content quality evaluation

The pipeline measures cost, latency and tokens. It does not measure whether the
posts are good. Fact-check findings are the only proxy, and they are a
correctness signal, not a quality one.

MLflow's evaluation tooling is already in the stack. Candidates:

- **Claim density** — checkable claims per 100 words, trending over time
- **Source utilization** — of N sources gathered, how many are actually cited?
  (`research_sources.used_in_draft` exists and is never populated)
- **Revision effectiveness** — do flagged findings survive the revision pass? A
  revision that changes nothing is currently invisible
- **LLM-judge rubric** — structure, specificity, hedging, reading level
- **Regression on prompt changes** — a fixed topic set run against a new prompt
  version before the alias moves

The last one is the real prize. Prompt edits take effect in 300 seconds with no
review step. That is excellent for iteration and slightly alarming for quality —
an eval gate before the alias move restores the balance without losing the speed.

- **Effort:** 1–2 weeks
- **Value:** closes the loop from "it runs" to "it is getting better"

### 2.3 Publish live, with permalink control

`posts.insert(isDraft=True)` is hardcoded. `pub_state.LIVE` exists in the enum
and nothing sets it. `seo_meta.slug` is generated, stored, and never sent to
Blogger — `scripts/probe_blogger_permalink.py` was the investigation into why.

The sequencing matters: **auto-publishing before 2.2 exists is a mistake.**
Right now a human reviews every draft, which is what makes "fact-check advises,
it does not gate" a safe policy. Remove the human without adding an eval gate and
you have removed the only quality control.

Suggested order: eval harness → a confidence threshold (zero contradicted
findings, source count above N, eval score above X) → auto-publish only above it,
everything else stays a draft.

- **Effort:** 2 days for the mechanism, and it should wait for 2.2

### 2.4 Idempotent human-facing state

A smaller one worth folding in here: `research_sources.used_in_draft` and
`node_runs.mlflow_span_id` are both columns designed for questions nobody can
currently answer ("did we cite what we found?", "which trace span was this
ledger row?"). Both are cheap to populate and both unlock analysis.

---

## Tier 3 — Scaling, in the one order that works

The worker is single-replica, and this is the item most people would reach for
first. It is third because **four independent constraints have to fall in
sequence**, and doing them out of order produces a system that looks like it
scaled and quietly reports wrong numbers.

**Constraint 1 — cost attribution is by time window.**

```python
"""Attribution is by time window, not by request id — the gateway does not return
a trace id or a cost header... That is exact here because the orchestrator is a
single-replica worker running one node at a time, so nothing else is calling
the gateway during the window. It would stop being exact under concurrent
workers."""
```

Two workers calling the same model concurrently get each other's cost attributed
to their own `node_runs` rows. Nothing errors. The totals stay right; the
per-node attribution silently becomes fiction.

*Must be fixed first, because it is the one failure with no symptom.*

**Constraint 2 — the gateway runs `--workers 1`.** Budget policies are enforced
per uvicorn worker under the default `local` tracker strategy. More gateway
workers means N independent budgets. Requires a Redis budget tracker.

**Constraint 3 — the secrets vault concurrent-write constraint**, which is the
original stated reason for one replica (`worker.py` docstring). Needs the
advisory-lock serialization the plan already anticipates.

**Constraint 4 — lease correctness**, which is *already done* (the heartbeat in
[post 4](04-reliability-and-idempotency.md)) and was the latent bug that
single-replica happened to be hiding.

Working order:

1. Per-call cost attribution that does not depend on a time window — a
   request-id correlation, or accept a documented approximation
2. Redis budget tracker → gateway `--workers > 1`
3. Advisory-lock serialization on the secrets path
4. Then, and only then, `replicas: 2`

- **Effort:** 1–2 weeks total
- **Value:** honestly, low right now. Throughput is not the bottleneck for a
  personal blog. This is on the list because the *analysis* is the deliverable —
  knowing the order is most of the work

---

## Tier 4 — Capability

### 4.1 Images and diagrams

Text-only by decision. `img` is in the bleach allowlist; nothing generates or
hosts images. A technical post explaining an architecture with no diagram is
working with one hand tied.

Needs: a generation or diagram-as-code step, hosting, and a fact-check path that
can evaluate a diagram — that last one is the hard part, and the reason this is
deferred rather than pending.

### 4.2 The video pipeline

Four tables, a `pipeline_kind` enum, a `parent_job_id` column and a `script` role
all exist. The `video/` package is empty. `YoutubeService/` is a sibling
directory that nothing calls.

The schema being pre-built is a real head start. The graph is reusable up to
`draft`; a video branch forks at the script node.

### 4.3 Multi-provider

`openai` and `gemini` are declared as providers with capability entries, and no
role targets either. `llm/capabilities.py` holds a static per-provider capability
table so nodes could branch on a *capability* rather than a provider name —
nothing branches on it yet.

The value is not "more models". It is that a role could then declare *"I need
server-side search"* and let routing satisfy it, instead of the constraint living
in a YAML comment:

```yaml
  # Bound to the web_search_20260209 / web_fetch_20260209 server tools, which
  # require Opus 4.6+ or Sonnet 4.6+ — Haiku 4.5 rejects both type strings.
```

### 4.4 Cross-service tracing

Telegram → orchestrator is DB rows only; correlation is by `job_id`, not span
link. For a bot whose entire job is inserting a row, the current design is
proportionate. Worth revisiting only if ingestion grows logic.

---

## Deliberately not doing

Ideas that sound good and are not, with reasons:

**A second fact-check pass.** `MAX_REVISIONS = 1` is a deliberate bound. An
unbounded critique loop against a model that will always find *something* is a
money pump. The escape hatch is a human reviewing a draft, and it is cheaper.

**A local prompt cache as a registry fallback.** The whole point of
[post 5](05-prompts-and-models-as-config.md) is that a job running on stale
prompt text produces a plausible wrong post while a failed job produces an alert.
There is a test stopping exactly this.

**Direct provider calls for latency.** Saves a hop, destroys the property that
the ledger and the budget are the same number, and reintroduces provider
credentials into the worker — losing the greppable invariant from
[post 6](06-security-and-secrets.md).

**Letting a planner choose the node order.** The fixed graph is what makes runs
priceable, traceable and explainable. Agency is scoped to tool use, the
conditional edge, and structured output — and that is enough.

---

## The whole backlog, one table

| # | Item | Effort | Value | Blocked by |
|---|---|---|---|---|
| 1.1 | Write `mlflow_run_id` | 1h | High | — |
| 1.2 | Completion notifications | 0.5d | High | — |
| 1.3 | Honour `cancel_requested` | 2h | Medium | — |
| 1.4 | Redact MLflow traces | 0.5d | Medium | — |
| 1.5 | Fix doc drift | 0.5h | Medium | — |
| 2.1 | Test suite | 1w | Very high | — |
| 2.2 | Eval harness | 1–2w | Very high | 2.1 |
| 2.3 | Publish live | 2d | High | 2.2 |
| 2.4 | Populate unused columns | 0.5d | Medium | — |
| 3.x | Horizontal scaling | 1–2w | Low now | Cost attribution, Redis budget, advisory locks |
| 4.1 | Images | 1w | Medium | Fact-check path for visuals |
| 4.2 | Video | 2–3w | Medium | — |
| 4.3 | Multi-provider | 3d | Low | — |
| 4.4 | Cross-service tracing | 2d | Low | — |

If I could do only three: **1.1, 1.2, 2.1.** Two of them are an afternoon, and
the third is what makes everything after it safe.

---

## Closing

The pattern across this whole backlog is that the remaining work is
*infrastructure for changing the system*, not features. The pipeline runs, it is
observable, it is cheap to modify, and it fails in ways that name their own
recovery. What it lacks is the ability to tell whether a change made the output
better — which is the last and hardest observability problem in any LLM product,
and the one no amount of tracing solves for you.

---

*Back to the [series index](README.md).*
