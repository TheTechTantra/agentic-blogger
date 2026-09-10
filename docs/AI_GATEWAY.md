# AI Gateway — Operator Guide

How to register, operate, and recover the MLflow AI Gateway, which is now the
only path from this pipeline to an LLM provider.

Architecture rationale lives in [ARCHITECTURE.md §5C](ARCHITECTURE.md). This
document is the runbook. Its sibling is [PROMPT_REGISTRY.md](PROMPT_REGISTRY.md);
the two describe the same MLflow server wearing different hats.

---

## The one rule

**No LLM call leaves this pipeline except through the gateway.**

The worker processes hold no provider API key. They cannot reach Anthropic or
Ollama directly even if something tried to — the clients are constructed with a
placeholder key and a `base_url` pointing at `http://mlflow:5000/gateway`, and
the real credentials live encrypted in the gateway's store.

If the gateway is unreachable, the job **fails**, in about two seconds. That is
the same contract as the Prompt Registry and for the same reason: a bypass
would mean spend that no budget sees and calls that no usage table records.

---

## 1. One-time setup

Run in order.

### 1.1 Seed the KEK passphrase

The gateway encrypts every stored provider credential with a key derived from
`MLFLOW_CRYPTO_KEK_PASSPHRASE`. MLflow falls back to a hardcoded development
default when it is unset — a value every MLflow installation shares — so
`services/mlflow/entrypoint.sh` refuses to start without a real one.

```bash
./scripts/seed_secrets.sh     # generates and stores it if absent; never overwrites
```

**This passphrase is effectively not rotatable.** Change it and every secret
already in the gateway store becomes undecryptable, which surfaces as endpoints
that exist and resolve but fail at invocation. If it is ever lost, the recovery
is to delete the gateway secrets and re-run `make register-gateway`, which
re-pushes them from TinyDB. The passphrase and the gateway database are a
single unit of backup and restore — see §6.

### 1.2 Build and start

```bash
docker compose build mlflow orchestrator telegram
docker compose up -d
```

The MLflow image installs `mlflow[genai]`, not bare `mlflow`. The gateway REST
routes answer either way, which makes the difference easy to miss — without the
extra, registration succeeds and every *invocation* fails.

### 1.3 Register

```bash
make register-gateway
```

Reads `config/models.yaml`, pulls each provider key from TinyDB, and converges
the gateway. Idempotent — see §3.

### 1.4 Verify

```bash
docker compose run --rm orchestrator python -m scripts.smoke_gateway
```

Checks every role reaches its model through the gateway with `usage_metadata`
intact, that Anthropic server tools still execute, and that the Ollama fallback
answers. Add `--skip-ollama` if no local Ollama is running.

---

## 2. Endpoint naming — the convention everything rests on

Gateway endpoints are named after the **model id**, not the role:

| `config/models.yaml` | gateway endpoint | provider model sent upstream |
|---|---|---|
| `anthropic:claude-opus-5` | `claude-opus-5` | `claude-opus-5` |
| `anthropic:claude-sonnet-5` | `claude-sonnet-5` | `claude-sonnet-5` |
| `anthropic:claude-haiku-4-5` | `claude-haiku-4-5` | `claude-haiku-4-5` |
| `ollama:qwen3.5:9b` | `qwen3.5-9b` | `qwen3.5:9b` |

Both gateway call surfaces read the request's `model` field as the **endpoint
name**. Naming endpoints after model ids means the chat client keeps sending
exactly what it always sent, so `provider:model` keys and the per-node cost
attribution in `llm/callbacks.py` keep working with `base_url` as the only
thing that changed.

The Ollama row is the one exception: gateway endpoint names allow only letters,
digits, `_`, `-` and `.`, and Ollama tags contain a colon. `llm/gateway.py`
holds the single `endpoint_name()` function that rewrites it, imported by both
`scripts/register_gateway.py` (which names endpoints) and `llm/registry.py`
(which addresses them). **If those two ever disagree, every call fails with
"endpoint not found" and neither file looks wrong** — which is why the rule
lives in one function and nowhere else.

---

## 3. Two call surfaces

`config/models.yaml`:

```yaml
gateway:
  enabled: true
  base_url: ""            # empty = derive from MLFLOW_TRACKING_URI
  surfaces:
    anthropic: passthrough
    gemini: openai
    openai: openai
    ollama: openai
```

**`passthrough`** → `{base}/anthropic`, forwarding the provider-native request
body untouched. Anthropic must use this. Verified to survive the hop:

- `web_search_20260209` / `web_fetch_20260318` server tools — the response
  comes back with `server_tool_use` and `web_search_tool_result` blocks, which
  is the entire research node.
- Adaptive thinking — `response.content` is still a block list containing
  `thinking`, which `llm/text.py:extract_text` depends on.
- `usage_metadata` including `input_token_details` with the `cache_read` /
  `cache_creation` buckets that the gateway prices separately.

A translating proxy would drop all three while still returning plausible text,
so `scripts/smoke_gateway.py` asserts each one explicitly.

**`openai`** → `{base}/mlflow/v1/chat/completions`, the unified
OpenAI-compatible surface, addressed by endpoint name and spoken by
`langchain-openai`. Everything that is not Anthropic uses it. Gemini is here
rather than on its own passthrough because `langchain-google-genai` cannot
address the gateway's `/gemini/v1beta/models/{endpoint}:generateContent` route
without reaching into its transport, and nothing in the Gemini request shape
needs preserving the way Anthropic's does.

---

## 4. Rotating a provider key

TinyDB stays the source of truth for provider credentials; the gateway holds a
copy.

```bash
./scripts/seed_secrets.sh     # enter the new ANTHROPIC_API_KEY
make register-gateway         # push it into the gateway
```

`register_gateway.py` matches the secret by name and calls `secrets/update`, so
the `secret_id` is unchanged and every model definition and endpoint built on it
keeps working. No restart needed.

Unlike `make register-prompts` — which deliberately creates a new version every
run — `make register-gateway` is idempotent. Running it twice in a row prints
`updated` for everything and changes nothing. That difference is intentional:
prompts are content people edit in the UI, gateway wiring is infrastructure
that must converge on the config file.

It does **not** delete objects that have dropped out of `config/models.yaml`.
Removing a model leaves an orphan endpoint behind, harmless but visible in the
UI; delete those by hand.

---

## 5. Usage and budgets

### Usage

Every endpoint is registered with `usage_tracking: true`, so the gateway
records tokens, cost, latency and error rate per endpoint. Because endpoint
name == model id, the AI Gateway usage view reads as a per-model spend table
with no extra mapping. Gateway calls also produce their own traces, named
`gateway/<endpoint>`, in the `agentic-blogger/blog` experiment alongside the
client-side `LangGraph` job traces.

**The gateway is the only thing that prices a call.** Nothing in this repo has
a rate table, a price catalog client, or a cost formula. `llm/cost.py` reads
the gateway's own figure back and `llm/callbacks.py` writes it to
`node_runs.cost_usd`, so the job ledger and the budget cannot disagree — they
are the same number.

How it works:

1. The gateway prices each call server-side and writes `total_cost` into
   MLflow's `span_metrics` table, against a span named
   `provider/<provider>/<model>`.
2. `MLFLOW_ENABLE_ASYNC_TRACE_LOGGING: "false"` on the mlflow service makes
   that write land *before* the gateway answers. With the default async
   exporter it arrives up to `MLFLOW_ASYNC_TRACE_LOGGING_MAX_INTERVAL_MILLIS`
   (5s) later — measured at 4.7s, which would be 4.7s added to every call.
   Synchronous, it is ~20ms.
3. `llm/cost.py` sums that metric over the wall-clock window of the call via
   `POST /api/3.0/mlflow/traces/metrics`.

Three properties of that query are load-bearing:

- **The span-name filter is not optional.** MLflow's server prices *every*
  trace carrying a model and token usage, so the client-side
  `mlflow.langchain.autolog()` span (named `ChatAnthropic`) has a cost of its
  own for the same call. Summing without filtering to `provider/...`
  double-counts. Only `=` is supported on that filter, hence one query per
  candidate model rather than a prefix match.
- **Attribution is by time window**, because the gateway returns neither a
  trace id nor a cost header. That is exact only because the orchestrator is a
  single-replica worker running one node at a time. **A second concurrent
  worker breaks it** — two overlapping calls would each claim the other's
  spend. Read this section before scaling the worker out.
- **Every model the call could reach is queried**, not just the primary, so a
  retry or a fallback to another model lands on the same `node_runs` row. It
  spent real money on that node's behalf.

`$0` is a legitimate reading, not only a failure. Ollama is in no price
catalog, so a locally served call genuinely costs nothing — and because "no
cost row" and "cost row not written yet" are indistinguishable, those calls pay
the full 1s poll guard in `llm/cost.py` before concluding zero.

Verified as genuinely the gateway's number rather than a lookalike: the reading
was compared against the delta in the gateway's own budget tracker
(`budgets/windows`) across the same call, and matched to float precision on
both Haiku and Sonnet.

Two hand-maintained price tables were deleted on the way here. `config/pricing.yaml`
had drifted on **6 of the 8 models it listed** — Sonnet 5 at $3/$15 (the
2026-09-01 increase that was cancelled; real $2/$10), gpt-4o at $5 (real
$2.50), gemini-2.0-flash at $0.075 (real $0.10), and cache reads at 0.25x base
input against Anthropic's [published](https://platform.claude.com/docs/en/about-claude/pricing)
0.1x. The code reading it also double-counted cache tokens, because LangChain's
`usage_metadata["input_tokens"]` *includes* `cache_read` and `cache_creation`.
Together those reported **3.75x the real cost** on a cache-heavy call. An
intermediate version priced from MLflow's model catalog locally, which fixed
the numbers but still had two components computing the same thing; reading the
gateway's figure removes the second computation entirely.

Still not counted anywhere: **web search at $10 per 1,000 searches**. The
research node binds `web_search` with `max_uses: 12`, so up to $0.12 per
research call is missing from `node_runs.cost_usd`. The gateway does not bill
it either — it is a server-tool charge on Anthropic's side, invisible to both.

### Budgets

Contrary to what the MLflow docs suggest, budget policies *are* in the REST
API (`3.0/mlflow/gateway/budgets/*`), so `register_gateway.py` creates one
rather than leaving it to be clicked together in the UI:

| Setting | Value | Why |
|---|---|---|
| Scope | `GLOBAL` | The question worth answering is what the whole pipeline spends. A per-endpoint budget silently allows N times the intended total once a role is repointed. |
| Amount | `$50` / month | Override with `GATEWAY_BUDGET_USD`. |
| Action | `REJECT` | Not `ALERT`. An unattended pipeline that keeps spending after an email nobody reads is the failure this exists to prevent. |

Once the threshold is crossed, calls return **HTTP 429** with
`Budget limit exceeded. Limit: $X USD per 1 month. Budget resets at <date>.`
The request that crosses the line still completes; the next one is rejected.

Current spend:

```bash
curl -s http://127.0.0.1:25000/api/3.0/mlflow/gateway/budgets/windows | python3 -m json.tool
```

**The gateway runs `--workers 1`, and budgets are half the reason.** The
default budget tracker strategy is `local`, which accumulates spend inside each
uvicorn worker process — with N workers a $50 budget is enforced at roughly
$50×N. The other half of the reason is memory (see the note on the memory limit
in `docker-compose.yml`). Raising the worker count requires adding Redis and
`MLFLOW_GATEWAY_BUDGET_REDIS_URL` first; `MLFLOW_SERVER_WORKERS` exists as the
knob but do not turn it alone.

`MLFLOW_GATEWAY_BUDGET_REFRESH_INTERVAL` is set to `60` in `docker-compose.yml`
rather than left at its 600s default, for the same reason
`prompts.cache_ttl_seconds` is set explicitly: it is the window in which a
tightened budget is not yet being enforced.

Per-endpoint **rate limits** (calls per second/minute/hour/day) are a separate
control and the right tool for runaway-loop protection. The budget is the
dollar backstop, not the first line of defence.

---

## 6. Persistence — what survives a restart

Gateway configuration is **not** in a config file and **not** in the image. It
lives in the MLflow backend database, written there by `make register-gateway`.
Knowing which of the two it is decides whether a restart costs you anything.

### Where the state actually lives

| State | Storage | Backed by |
|---|---|---|
| Secrets, model definitions, endpoints, budget policies | Postgres DB `mlflow`, tables `secrets`, `model_definitions`, `endpoints`, `endpoint_model_mappings`, `endpoint_bindings`, `endpoint_tags`, `budget_policies` | bind mount `./data/postgres` |
| KEK passphrase (decrypts the above secrets) | TinyDB credential store | bind mount `./data/vault/db.json.enc` |
| Traces, spans, `total_cost` metrics that `llm/cost.py` reads | same Postgres DB `mlflow` | bind mount `./data/postgres` |
| Trace artifacts | filesystem | bind mount `./data/mlartifacts` |
| `entrypoint.sh` (`--workers 1`, allowed-hosts, KEK fetch, `mlflow db upgrade`) | baked into `agentic-blogger/mlflow:local` | image — edit requires a rebuild |
| Gateway env (`MLFLOW_ENABLE_ASYNC_TRACE_LOGGING`, `MLFLOW_GATEWAY_BUDGET_REFRESH_INTERVAL`, `MLFLOW_SERVER_ENABLE_JOB_EXECUTION`) | `docker-compose.yml` | re-applied on every `up` |

Every one of those is a **bind mount, not a named Docker volume**. That is the
detail that makes `docker compose down -v` safe here — `-v` removes anonymous
and named volumes, and this stack has neither for stateful data.

### Restart matrix

| Action | Gateway registration survives |
|---|---|
| `docker compose restart mlflow` | yes |
| `docker compose down` then `up -d` | yes |
| `docker compose build mlflow && docker compose up -d` | yes — state is in Postgres, not the image |
| `make clean` (`docker compose down -v`) | yes — see the bind-mount note above |
| `make nuke` (`down -v` **plus** `rm -rf data/`) | **no** — destroys Postgres, vault, and artifacts |
| `rm -rf data/postgres` alone | no, and see the coupling warning below |

Recovery after any loss is one idempotent command:

```bash
make register-gateway
```

It matches every object by name and updates in place, so running it against a
half-populated gateway converges it rather than duplicating anything.

### The coupling that bites: `./data/postgres` + `./data/vault`

These two directories are a **single unit of backup and restore**. The
provider credentials are stored in Postgres encrypted under a key derived from
the passphrase in the vault. Restore one without the other and you get the
worst failure shape in the system: endpoints that exist, list cleanly, and
resolve — then 500 on every invocation, because their secrets cannot be
decrypted. Nothing fails at startup to warn you.

If you restore `./data/postgres` from a backup and the vault has moved on,
don't debug the endpoints — delete the gateway secrets and re-run
`make register-gateway` to re-push them under the current passphrase.

### Verifying a change actually reached the running stack

Image build timestamps are not proof; a build can succeed against a stale
context or a cached layer. Compare bytes.

```bash
# Orchestrator: does the container hold the same source as the working tree?
for f in agentic_blogger/llm/gateway.py agentic_blogger/llm/cost.py \
         agentic_blogger/llm/registry.py agentic_blogger/llm/callbacks.py \
         agentic_blogger/config/loader.py config/models.yaml; do
  docker exec agentic-blogger-orchestrator cat "/app/$f" | diff -q - "$f" >/dev/null \
    && echo "SAME  $f" || echo "DIFF  $f"
done

# MLflow: entrypoint is baked, so checksum it
docker exec agentic-blogger-mlflow md5sum /app/entrypoint.sh
md5 -q services/mlflow/entrypoint.sh          # macOS; md5sum on Linux

# What the server is actually running, flags and all
docker exec agentic-blogger-mlflow sh -c 'tr "\0" "\n" < /proc/1/cmdline'

# Is the KEK passphrase present in the server process (not just in compose)?
docker exec agentic-blogger-mlflow sh -c 'tr "\0" "\n" < /proc/1/environ' \
  | grep -c MLFLOW_CRYPTO_KEK_PASSPHRASE     # must print 1
```

`ps` is unavailable in the MLflow image (`python:3.12-slim`), which is why
`/proc/1/cmdline` is used above.

Gateway-side state, straight from the management API:

```bash
for p in endpoints model-definitions secrets budgets; do
  echo "== $p =="
  curl -s "http://127.0.0.1:25000/api/3.0/mlflow/gateway/$p/list" | python3 -m json.tool
done
```

A converged gateway holds one endpoint per entry in `_model_keys()` — every
`roles[*].model` plus every fallback key and target in `config/models.yaml`.
Fewer endpoints than that means a provider credential was missing when
`register-gateway` ran; it logs `✗ <provider> no credential in TinyDB` and
skips every endpoint built on it rather than failing.

### Verified state (2026-09-09)

Recorded so a future drift is visible as a diff rather than a guess.

- Endpoints: `claude-opus-5`, `claude-sonnet-5`, `claude-haiku-4-5`,
  `qwen3.5-9b` — all `usage_tracking: true`. Four, matching three role models
  plus one Ollama fallback.
- Model definitions: the same four names.
- Secrets: `agentic-blogger-anthropic`, `agentic-blogger-ollama`.
- Budget: one `GLOBAL` policy, $50/month, action `REJECT`.
- MLflow server: `--workers 1`, allowed-hosts enumerated, KEK passphrase
  present in the process environment.
- Orchestrator image: byte-identical to the working tree, with `llm/pricing.py`
  and `config/pricing.yaml` absent — the local price table is gone, as intended.

### Hardening note — the DSN on the command line

`entrypoint.sh` passes the backend store as a flag, so the Postgres password is
visible in the MLflow server's command line: `docker inspect`, any in-container
`/proc/1/cmdline` read, and `ps` where available all expose it.

MLflow reads `MLFLOW_BACKEND_STORE_URI` from the environment, so exporting it
instead of passing `--backend-store-uri` keeps the credential out of the
process table. `mlflow db upgrade` still needs the value as an argument; use
the variable there too rather than re-inlining the literal.

---

## 7. Failure modes

| Symptom | Cause | Fix |
|---|---|---|
| `APIConnectionError: Connection error` ~2s into a job | Gateway down. This is the fail-closed contract working. | `docker compose up -d mlflow` |
| `404 page not found` from an Ollama endpoint | `api_base` missing the `/v1` suffix. The gateway's Ollama adapter is an OpenAI-compatible client. | `make register-gateway` — it appends `/v1` |
| `Invalid endpoint name '...'` on registration | Model id contains a character the gateway rejects in a name | Handled by `llm/gateway.py:endpoint_name()`; extend `_INVALID` if a new provider needs it |
| Endpoints resolve but every invocation 500s | Credentials encrypted under a different KEK passphrase | Restore the original passphrase, or delete the gateway secrets and re-run `make register-gateway` |
| Registration succeeds, invocations fail on a missing SDK | Image built with bare `mlflow`, not `mlflow[genai]` | Rebuild `services/mlflow` |
| `node_runs.cost_usd` is 0 for a model that should cost money | Async trace export re-enabled, so the cost row lands after `llm/cost.py` looks | Set `MLFLOW_ENABLE_ASYNC_TRACE_LOGGING: "false"` on the mlflow service |
| Costs look roughly doubled | Something summed cost without the `span.name = 'provider/...'` filter, picking up the client-side autolog span too | Filter to `provider/...` spans |
| `500 Internal Server Error` from `/gateway/anthropic/v1/messages` ~301s into a call, `TimeoutError` in the mlflow log | The gateway's own upstream timeout, `MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS` (MLflow default 300s), is shorter than the call. Not a provider error — see the timeout layering note below | Set `MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS` above `defaults.timeout_s`; already `900` in `docker-compose.yml` |
| A model in `config/models.yaml` has no endpoint | Its provider credential was missing when `register-gateway` ran — it logs `✗ <provider> no credential in TinyDB` and skips, rather than failing | Seed the key, re-run `make register-gateway` |
| Endpoints and budget gone after a teardown | `make nuke` — it runs `rm -rf data/` on top of `down -v`. A plain `down`, `down -v`, or image rebuild does not do this (§6) | `make register-gateway` |
| HTTP 429, `Budget limit exceeded` | Working as designed | Raise `GATEWAY_BUDGET_USD` and re-run `make register-gateway`, or wait for the window to reset |
| `Failed to send trace to MLflow backend ... tracking URI ... postgresql+` | The server process overwrites `MLFLOW_TRACKING_URI` with its backend store URI, so `mlflow-artifacts:` cannot resolve in-process. Affects some server-side trace exports only. | Cosmetic. Usage and budget accounting are written directly to the database and are unaffected — verified by watching `budgets/windows` increment while this warning was firing. |

### Timeout layering

Three timeouts sit on one LLM call. They must stay in this order, longest
outermost, or a call gets severed by the wrong one and reports a fault that
did not happen.

| Bound | Where | Value |
|---|---|---|
| Client HTTP timeout | `config/models.yaml` `defaults.timeout_s`, applied in `llm/registry.py:_construct()` | 600s |
| Gateway upstream timeout | `MLFLOW_GATEWAY_ROUTE_TIMEOUT_SECONDS`, mlflow service env | 900s |
| Job lease | `graph/runner.py` heartbeat | 15min, renewed while the node runs |

The client must be the one that gives up first. When the gateway's timeout is
the shorter of the two — which is what MLflow's 300s default produces — the
call dies inside the gateway and the client sees a bare `500`, indistinguishable
from a misconfigured endpoint or a dead credential. Observed 2026-09-09 on a
research node: the `web_search` loop at `max_uses: 12` ran 300.9s and was cut
at exactly the 300s default.

The gateway's value is a ceiling, not a target. It holds its single uvicorn
worker for the whole call, so it is equally the longest one stuck call can
block every other gateway request. Raising it much further needs `--workers`
raised too, which needs a Redis budget tracker first (§5).

**Retries multiply the cost of any of these firing.** The provider SDKs retry
internally by default (Anthropic: 2), and that count multiplies with
`resilience.max_attempts` (5) wrapped around it — 15 upstream calls for one
node. Every one is a real, billed provider generation that is then thrown away,
and the gateway writes no usage row for a call it never received a response
from, so **that spend is invisible to the budget**. `llm/registry.py` therefore
constructs every client with `max_retries=0`: retry is `with_resilience()`'s
job and only its job. This is the same stacking hazard as
`MLFLOW_HTTP_REQUEST_MAX_RETRIES` on the prompt path.

### Outage drill

```bash
docker compose stop mlflow
# submit a job via Telegram
docker compose start mlflow
```

Expected: the job fails within seconds with a connection error, and nothing
reaches a provider. Measured: 2.2s. Confirm no provider credential is present
in the worker:

```bash
docker compose exec orchestrator env | grep -E 'ANTHROPIC|OPENAI|GEMINI'   # must print nothing
```

---

## 8. Files

| Path | Role |
|---|---|
| `config/models.yaml` | `gateway:` block — enabled, base URL, per-provider surface |
| `agentic_blogger/config/loader.py` | `gateway_spec()`, `timeout_s()` |
| `agentic_blogger/llm/gateway.py` | `endpoint_name()` — the one naming rule, shared by both sides |
| `agentic_blogger/llm/cost.py` | Reads each call's cost back from the gateway; the only cost source |
| `agentic_blogger/llm/registry.py` | `_gateway_route()` and `_construct()` — where every client gets its `base_url` |
| `scripts/register_gateway.py` | Idempotent convergence: secrets, model definitions, endpoints, budget |
| `scripts/smoke_gateway.py` | Verification |
| `services/mlflow/Dockerfile` | `mlflow[genai]` |
| `services/mlflow/entrypoint.sh` | KEK passphrase fetch, `--workers 1` |
| `docker-compose.yml` | Gateway env (incl. synchronous trace export), `extra_hosts` for Ollama, memory limit |
| `./data/postgres` | Where all gateway registration and usage state actually lives — back up with `./data/vault` or not at all (§6) |
| `./data/vault` | TinyDB store holding `MLFLOW_CRYPTO_KEK_PASSPHRASE`, which decrypts the credentials in `./data/postgres` |
