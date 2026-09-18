# Building Agentic Blogger — a Series

An eight-part write-up of a production LangGraph pipeline that researches, drafts,
fact-checks, formats and publishes blog posts, with every LLM call priced, traced
and budgeted.

This is not a tutorial built from a blank repo. It is a tour of a system that
runs, including the parts that broke first and the decisions taken because
something broke.

## The system in one paragraph

A Telegram bot writes a row into Postgres. A single-replica worker claims that
row, runs an eight-node LangGraph pipeline against it, and publishes the result
to Blogger as a draft. Every prompt comes from the MLflow Prompt Registry, every
model call goes through the MLflow AI Gateway, and every call's real USD cost is
read back out of the gateway and written to a per-node ledger. Nothing in the
worker holds a provider API key.

## Reading order

| # | Post | What you get |
|---|------|--------------|
| 1 | [Agentic Architecture](01-agentic-architecture.md) | Why a graph, not a chain. The eight nodes, the state contract, the one conditional loop, and why job claiming is deliberately *not* LangGraph's job |
| 2 | [Code Walkthrough](02-code-walkthrough.md) | The segments worth stealing: node tracing decorator, two-pass research, per-call cost ledger, gateway routing, sanitizing formatter |
| 3 | [Observability](03-observability.md) | Fail-open tracing vs fail-closed dependencies, MLflow autolog, the `node_runs` ledger, ContextVar log correlation, secret redaction, poll-noise collapsing |
| 4 | [Reliability and Idempotency](04-reliability-and-idempotency.md) | Leases, heartbeats, checkpoint resume, the beacon that gives Blogger an idempotency key it does not have, and how stacked retries turned 1 call into 15 |
| 5 | [Prompts and Models as Configuration](05-prompts-and-models-as-config.md) | Prompt text in a registry with aliases and preflight; role→model routing in one YAML file; why schema field descriptions are prompts too |
| 6 | [Security and Secrets](06-security-and-secrets.md) | TOTP-on-every-message, an HTTP secrets vault, zero provider credentials in the worker, HTML sanitization, log redaction |
| 7 | [What I'd Improve Next](07-improvement-roadmap.md) | A ranked backlog with real code pointers — the gaps, the half-wired features, and the scaling work that must happen in a specific order |
| 8 | [Going Fully Agentic](08-going-fully-agentic.md) | The transition from a fixed graph to a true agent loop: dynamic control flow, termination as a subsystem, invariants relocated into tool boundaries, trajectory observability, and multi-agent decomposition |

## Companion operator docs

The series explains *why*. These explain *how to run it*:

- [ARCHITECTURE.md](../ARCHITECTURE.md) — full system reference
- [AI_GATEWAY.md](../AI_GATEWAY.md) — gateway setup, key rotation, budgets, restart matrix
- [PROMPT_REGISTRY.md](../PROMPT_REGISTRY.md) — migration, editing, backup, outage drill
- [TELEGRAM_USAGE.md](../TELEGRAM_USAGE.md) — command reference and end-to-end walkthrough

## Five ideas that carry the whole series

1. **Fail open on observability, fail closed on anything that changes what the pipeline does.** Losing traces costs a graph; running on absent prompt text costs a plausible wrong post.
2. **The job queue and the durable execution engine are different problems.** LangGraph's checkpointer resumes a node sequence. It says nothing about which job to pick up next.
3. **Fact-check advises, it does not gate.** One bounded revision pass, then ship with the finding counts embedded in the HTML.
4. **One component prices a call, and it is the same one that enforces the budget.** So the ledger and the budget cannot disagree.
5. **Provider swaps are config, not code.** Nodes take a role name. They never see a model string.

Post 8 then takes the fifth idea further than the first seven allow: what has to
change when the model — not `builder.py` — decides what happens next.
