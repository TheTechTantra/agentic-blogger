# Prompt Registry — Operator Guide

How to migrate to, operate, and recover the MLflow Prompt Registry.

Architecture rationale lives in [ARCHITECTURE.md §5A](ARCHITECTURE.md). This
document is the runbook.

---

## The one rule

**The MLflow Prompt Registry is the only source of truth for prompt text.**

No prompt string lives in Python. `agentic_blogger/prompts/` is the only way to
get one. There is no fallback — no bundled defaults, no local cache we
maintain, no last-known-good copy.

If the registry is unreachable, the job **fails**. That is the intended
behaviour, not a bug to work around. A job that ran on stale or absent prompt
text produces a plausible wrong post; a failed job produces an alert.

`prompts_backup/` is for disaster recovery only and is never read at runtime.
`tests/test_prompt_backup_is_not_a_load_path.py` fails the build if that ever
changes.

---

## 1. One-time migration

Run in order. Steps 1–2 are a breaking MLflow major upgrade; do not skip the
schema migration.

### 1.1 Upgrade MLflow 2.18 → 3.16

Prompt Registry did not exist before MLflow 2.21.0; the `mlflow.genai`
namespace landed in 3.1 and the APIs were marked stable in 3.10.

Already pinned in the repo:

| File | Change |
|---|---|
| `requirements.txt` | `mlflow==2.18.0` → `mlflow==3.16.0` |
| `services/mlflow/Dockerfile` | same bump |
| `services/mlflow/entrypoint.sh` | added `mlflow db upgrade` before `mlflow server`, and `--allowed-hosts` |
| `docker-compose.yml` | mlflow memory 512M → 3G; MLflow HTTP retry env on orchestrator |

Two things 3.x needs that 2.18 did not, both found the hard way:

- **Memory.** 3.x runs ~16 processes: 4 uvicorn workers (`--workers`
  defaults to 4) plus a huey consumer pool for `mlflow.server.jobs`, at
  roughly 250 MiB each. Under the old 512M cap workers were OOM-killed on
  spawn — the log shows `Child process died` in a loop with no traceback,
  because SIGKILL leaves none. 2G was also too tight (2013 MiB anon against
  the cap, 358 forced reclaims, socket throttling). Now **3G**, sitting at
  ~2.1GiB / 70%.

  If it needs raising again, prefer cutting processes over adding RAM:
  `--workers 2` plus `MLFLOW_SERVER_ENABLE_JOB_EXECUTION=false` removes most
  of it. The job pool serves judges, scorers, online scoring and webhooks —
  none of which this pipeline uses. Watch for growth: usage climbed 1.41 →
  2.13 GiB over the first hour, so if that continues, extra RAM only buys
  time.
- **Host-header validation.** 3.x added DNS-rebinding protection that allows
  localhost and private IPs but *not* service hostnames, so in-network calls
  to `http://mlflow:5000` return
  `403 Invalid Host header - possible DNS rebinding attack detected`.
  `entrypoint.sh` now passes `--allowed-hosts` with the hosts enumerated
  (override with `MLFLOW_ALLOWED_HOSTS`).

```bash
docker compose build mlflow orchestrator
docker compose up -d
```

`mlflow server` **refuses to start against an out-of-date backend schema**
rather than migrating it, which is why `entrypoint.sh` now runs
`mlflow db upgrade "$POSTGRES_MLFLOW_DSN"` first. That command is idempotent —
a no-op once the database is current.

Verify:

```bash
docker compose logs mlflow | tail -30      # no schema complaints
docker compose run --rm orchestrator python -m scripts.smoke_mlflow
```

Then open http://127.0.0.1:25000 and confirm pre-existing runs still render.

### 1.2 Migrate the app database

```bash
make migrate
```

Adds `app.node_runs.prompts_json` (migration `0003_prompt_tracking`).

### 1.3 Seed the registry

```bash
make register-prompts
```

Registers 21 prompts and points the `production` alias at each. Dry-run first
if you want to see what it would push without writing:

```bash
docker compose run --rm orchestrator python -m scripts.register_prompts --dry-run
```

The script validates itself against `agentic_blogger/prompts/specs.py` before
pushing anything, and refuses to run if a template references a variable the
manifest does not declare.

### 1.4 Verify

```bash
make smoke        # smoke_prompts runs first
```

`scripts/smoke_prompts.py` is read-only and costs nothing. It checks that every
prompt resolves at the alias, that templates render with placeholder values,
and that schema descriptions bind onto the Pydantic models.

---

## 2. Editing a prompt

**Edit in the MLflow UI. No deploy, no code change.**

1. Open http://127.0.0.1:25000 → Prompts
2. Edit the prompt, creating a new version
3. Move the `production` alias to that version

The change takes effect on the next job within `prompts.cache_ttl_seconds`
(default 300s, `config/models.yaml`).

> **Do not re-run `register_prompts.py` to make an edit.** It pushes the seed
> texts as new versions and reverts UI edits. It is a bootstrap and a recovery
> tool only.

### Constraints on an edit

- **Variables**: you may only use `{{variables}}` that the node supplies —
  those declared for that prompt in `agentic_blogger/prompts/specs.py`.
  Introducing a new one fails preflight at the next job start, before spend.
- **Structured-output prompts** (`research_synth_user`, `outline_user`,
  `factcheck_user`, `seo_user`) carry a `response_format`. Changing the shape
  requires a matching change to the Pydantic model in `nodes/schemas.py`.
- **Schema-description prompts** (`schema_*_descriptions`) are JSON maps keyed
  `"ClassName.field"`. They are bound once per process and cached, so an edit
  needs a **worker restart**, unlike every other prompt.

---

## 3. Adding a new prompt

A new prompt needs both a code change and a registry entry.

1. Declare it in `agentic_blogger/prompts/specs.py` — name, allowed variables,
   which of them carry generated content, optional `response_format`
2. Add its text to `scripts/register_prompts.py`
3. Call it from the node: `render(S.MY_PROMPT, variables={...}, content={...})`
4. `make register-prompts`

`variables` vs `content` matters. `content` is model- or user-generated text
(a draft, a research brief, findings) and is substituted **last**, because
MLflow's `format_prompt` runs a sequential `re.sub` over the accumulating
string — text substituted early is still visible to later substitutions.
Passing content last means a draft that happens to contain `{{topic}}` is
never itself rewritten.

---

## 4. Backup and restore

### Backing up

```bash
make export-prompts                  # -> prompts_backup/
git add prompts_backup
git commit -m "chore: back up prompt registry"
git push
```

Writes one `<prompt_name>.txt` per prompt plus `manifest.json` (versions,
variables, which prompts carry a `response_format`). Reads with
`cache_ttl_seconds=0` so a snapshot reflects the registry right now rather than
a warm cache.

Options:

```bash
python -m scripts.export_prompts --all-versions   # full history, not just @production
python -m scripts.export_prompts --out-dir DIR
```

**Nothing runs this on a schedule.** The backup is only as fresh as the last
time someone ran it — if the MLflow database is lost between exports, UI edits
made since the last run are gone. Run it after any meaningful prompt change.

### Restoring

Deliberate and manual, by design:

1. Copy the texts from `prompts_backup/*.txt` back into
   `scripts/register_prompts.py`
2. `make register-prompts`

---

## 5. Configuration

`config/models.yaml`:

```yaml
prompts:
  alias: production
  cache_ttl_seconds: 300

resilience:
  max_attempts: 5
  initial_backoff_s: 1
  max_backoff_s: 30
  jitter: true
```

**`cache_ttl_seconds` is one number with two meanings**: the window in which an
edited prompt has not taken effect yet, *and* the window in which a registry
outage stays invisible. It is set explicitly rather than inheriting MLflow's
implicit 60s alias-cache default. Lower it to surface outages faster and pick
up edits sooner, at the cost of a round-trip per node per job. `0` disables
caching entirely.

`resilience:` governs Blogger publishing and LLM invocations, via
`agentic_blogger/resilience.py`. LangChain's `.with_retry()` accepts only an
attempt count and a jitter flag, so the backoff bounds apply to the Blogger
path and not to LLM calls.

**MLflow is deliberately excluded.** Its REST client already retries
internally, so wrapping it multiplied out to 35 HTTP attempts per prompt and a
registry outage took over five minutes to surface. Prompt fetches are
unwrapped; MLflow's own knobs are the policy, set on the orchestrator service
in `docker-compose.yml`:

```yaml
MLFLOW_HTTP_REQUEST_MAX_RETRIES: "4"     # default 7
MLFLOW_HTTP_REQUEST_TIMEOUT: "10"        # default 120
MLFLOW_HTTP_REQUEST_BACKOFF_FACTOR: "1"  # default 2
```

These also apply to tracing calls, which is wanted — tracing is fail-open and
should give up quickly rather than stall an otherwise-healthy job.

Preflight additionally **aborts on the first unreachable prompt** instead of
walking all 21, since the rest would fail identically after the same retry
budget each. Measured: a full-outage preflight fails in ~17s.

---

## 6. Versioning model

Prompts resolve **by alias, per node, at call time**.

Consequence: a single job can legitimately span two prompt versions if a
prompt is republished mid-run. There is therefore no single job-level prompt
version. What actually ran is recorded per node:

```sql
SELECT node_name, prompts_json
FROM app.node_runs
WHERE job_id = '<uuid>'
ORDER BY started_at;
```

```json
[{"name": "draft_user", "version": 7, "alias": "production"}]
```

`jobs.config_snapshot.prompt_alias` records which alias a job was queued
against.

> **Known gap:** `repo.py` computes `idempotency_key` from a `config_version`
> that is always the literal `"v1"`. A prompt change does **not** invalidate a
> topic's job, so re-running the same topic returns the old job via the
> `reused: True` path with the old output. Alias-only versioning cannot fix
> this on its own — it needs a separate decision.

---

## 7. Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| Job → `FAILED`, `failure_class=PromptRegistryError` | Registry unreachable after 5 retries, or preflight found a problem | Read `failure_reason` — it lists every problem found, not just the first. Fix the registry, re-create the job. |
| `preflight failed: template uses ['x'], which the node does not supply` | A prompt was edited to reference an undeclared variable | Revert the alias to the previous version, or add the variable to `specs.py` and the node |
| `could not load prompt prompts:/name@production` | Prompt missing, or alias never set | `make register-prompts`, or set the alias in the UI |
| `response_format fields [...] != Model fields [...]` | Registered `response_format` drifted from the Pydantic model | Re-register that prompt, or fix the model |
| Prompt edit has no effect | Within `cache_ttl_seconds`; or a `schema_*_descriptions` edit | Wait out the TTL; restart workers for schema descriptions |
| `mlflow server` won't start after upgrade | Backend schema out of date | `mlflow db upgrade "$POSTGRES_MLFLOW_DSN"` — `entrypoint.sh` does this automatically |

### Outage drill

Worth running once to confirm the no-fallback guarantee holds end to end:

```bash
docker compose stop mlflow
# submit a job via Telegram
docker compose logs -f orchestrator
```

Expect: retries with visible backoff, then job state `FAILED`, in roughly 20
seconds. **Not** a hang, and **not** a job that completes on stale cached text.

Verified 2026-09-09: preflight raised `PromptRegistryError` after 17.2s with
`aborted after research_system: registry unreachable, 20 prompt(s) unchecked`.

---

## 8. Registered prompts

21 total. Separate entries per message — system and user prompts are versioned
independently.

| Prompt | Used by | Notes |
|---|---|---|
| `research_system` | research | system prompt, both job types |
| `research_url_system` | research | appended for URL jobs only |
| `research_user` | research | topic jobs |
| `research_url_user` | research | URL jobs |
| `research_url_angle` | research | omitted when angle is absent or equals the URL |
| `research_synth_user` | research | → `ResearchBrief` |
| `research_synth_primary_rules_url` | research | URL-job branch |
| `research_synth_primary_rules_none` | research | topic-job branch |
| `outline_user` | outline | → `Outline` |
| `outline_source_rules` | outline | only when a primary source exists |
| `draft_user` | draft | |
| `attribution_explainer_contract` | draft | injected ahead of the draft prompt |
| `attribution_quote_block` | draft | when verbatim quotes were captured |
| `attribution_quote_block_empty` | draft | when none were |
| `factcheck_user` | factcheck | → `FactCheckReport` |
| `revise_user` | revise | |
| `seo_user` | seo | → `SeoMeta` |
| `schema_research_brief_descriptions` | schemas | JSON map; restart to apply |
| `schema_outline_descriptions` | schemas | JSON map; restart to apply |
| `schema_factcheck_descriptions` | schemas | JSON map; restart to apply |
| `schema_seo_descriptions` | schemas | JSON map; empty, ready for additions |

Conditional text is a separate prompt rather than a variable: the two branches
are different English, not one sentence with a hole in it. Code picks the
branch; the registry owns the words.

---

## 9. Files

| Path | Role |
|---|---|
| `agentic_blogger/prompts/registry.py` | `load()` / `render()`; the only load path |
| `agentic_blogger/prompts/specs.py` | manifest — names, variables, response formats |
| `agentic_blogger/prompts/preflight.py` | `check_all()`, run at job start |
| `agentic_blogger/resilience.py` | shared retry policy |
| `scripts/register_prompts.py` | bootstrap / recovery seeding |
| `scripts/export_prompts.py` | backup export (manual) |
| `scripts/smoke_prompts.py` | read-only verification |
| `migrations/versions/0003_prompt_tracking.py` | `node_runs.prompts_json` |
| `tests/test_prompt_backup_is_not_a_load_path.py` | guards the one rule |
