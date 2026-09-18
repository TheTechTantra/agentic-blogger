# 5. Prompts and Models as Configuration

*Part 5 of the [Agentic Blogger series](README.md).*

Two questions decide how fast you can iterate on an LLM product:

- How long does it take to change a prompt?
- How long does it take to change a model?

In this system both answers are "no deploy". This post is how, and what each
choice costs.

---

## Part 1 — Prompt text lives in a registry, not in Python

### The rule

> **The registry is the only source of truth for prompt text. No prompt string
> lives in Python.**

`agentic_blogger/prompts/` is the only way to get one. Twenty-one prompts are
registered: one per message (system and user are separate entries), conditional
fragments, and four JSON maps carrying schema field descriptions.

A node reads like this:

```python
prompt = render(S.FACTCHECK_USER, content={"brief": brief, "draft": draft})
```

`S` is a manifest module of name constants. The node knows a prompt's *name* and
the variables it supplies. It never knows the text.

### No fallback, deliberately

```python
"""There is deliberately NO fallback: no bundled defaults, no local cache we
maintain, no last-known-good copy. If the registry cannot be reached the job
fails and someone fixes the registry."""
```

The argument, in one line: *a job that ran on absent prompt text produces a
plausible wrong post; a failed job produces an alert.*

A backup mirror exists in `prompts_backup/` for disaster recovery — and there is
a test whose entire job is to stop it becoming a runtime path:

```text
tests/test_prompt_backup_is_not_a_load_path.py
```

Encoding an architectural constraint as an executable test is the only way it
survives contact with a future contributor who finds the backup files and
thinks "we could fall back to these."

### Versioning by alias

Prompts resolve by alias (default `production`), per node, at call time. That
has a consequence worth stating plainly:

> One job can legitimately span two prompt versions if a prompt is republished
> mid-run, so there is no single job-level version.

So versions are recorded per node instead, in `node_runs.prompts_json`:

```json
[{"name": "draft_user", "version": 7, "alias": "production"}]
```

Editing a prompt is: edit in the MLflow UI, move the alias. No deploy, no code
change, no restart. Re-running `register_prompts.py` would push the seed texts as
new versions and revert UI edits — it is a bootstrap and recovery tool, not part
of the edit loop.

### The TTL is one number with two meanings

```yaml
prompts:
  alias: production
  cache_ttl_seconds: 300
```

```yaml
# cache_ttl_seconds is set explicitly rather than inheriting MLflow's implicit
# 60s alias-cache default. It is the window in which an edited prompt has not
# taken effect yet, and equally the window in which a registry outage stays
# invisible — those are the same number, so tune it deliberately.
```

Lower it and edits land faster but outages surface faster too. Raise it and you
get the reverse. There is no setting that is good at both, and pretending
otherwise is how you end up debugging an edit that "didn't apply" for five
minutes.

### The `variables` / `content` split

This one is a genuinely non-obvious bug class:

```python
def render(name: str, variables: dict | None = None, content: dict | None = None) -> str:
    """`variables` are short, code-controlled values (tier names, a topic, a
    citation line). `content` carries model- or user-generated text (a draft,
    a research brief, fact-check findings).

    The split exists because MLflow's format_prompt runs a sequential re.sub
    over the accumulating string, so text substituted early is still visible
    to later substitutions. Passing content last means a draft that happens to
    contain "{{topic}}" is never itself rewritten."""
```

Sequential substitution means substituted text is itself a substitution target.
A model-written draft containing `{{topic}}` would get rewritten. Ordering
code-controlled values first and generated text last closes it.

This is a **template injection** concern, and it is the same shape as SQL
injection: untrusted content reaching a templating engine's own syntax. The
mitigation is ordering rather than escaping, and the docstring is careful about
scope — it guards against generated text that mimics template syntax, not
against ordinary punctuation.

### Missing variables fail loudly

```python
"""Missing variables raise: PromptVersion.format defaults to
allow_partial=False, so a prompt edited to reference a variable the node
does not supply fails loudly here rather than reaching a model with an
unsubstituted placeholder in it."""
```

An unsubstituted `{{brief}}` reaching a model does not error. It produces a
confidently wrong post. Strict mode is not optional here.

### Preflight: catch the edit before the spend

Because prompts can be edited without a deploy, the pipeline can be broken
without a code change. So every job verifies the registry before invoking the
graph:

```python
"""Three checks per prompt:
  - it resolves at the configured alias;
  - every {{variable}} in the template is one the node code supplies
    (declared in specs.py). Extra declared-but-unused variables are fine —
    the reverse is what breaks at render time;
  - a prompt that backs structured output carries a response_format whose
    top-level properties match the Pydantic model the node passes."""
```

Two design details:

**Report everything, not the first problem.**

```python
"""Raises PromptRegistryError listing every problem found, rather than
stopping at the first — one restart should surface the whole mess."""
```

**Except on an outage, where the first failure is decisive**, as covered in
[post 4](04-reliability-and-idempotency.md).

The asymmetry is right: validation problems are independent and you want them
all; connectivity problems are the same problem 21 times.

### Schema descriptions are prompts too

The best idea in this subsystem. `.with_structured_output()` compiles Pydantic
`Field(description=...)` text into the tool JSON schema the model reads — which
means those descriptions steer output exactly like prompt text does.

So they are not in the Python file:

```python
"""Field descriptions are prompt surface: .with_structured_output() compiles them
into the tool JSON schema the model reads, so they steer output exactly like
prompt text does. They therefore live in the MLflow Prompt Registry with every
other prompt, not in this file — the classes below carry structure (names,
types, defaults, requiredness) and nothing the model reads as instruction.

Call bound(SomeModel) rather than passing a model class straight to
.with_structured_output(), or the model will see an undescribed schema."""
```

A clean separation: **the class owns the shape, the registry owns the
instruction.** Every node uses `bound(...)`:

```python
llm = build_llm("outline").with_structured_output(bound(Outline), include_raw=True)
```

One accepted cost, written down:

> Descriptions are applied by `bind_descriptions()` on first use and cached for
> the life of the process. Consequence, accepted deliberately: a description
> edit needs a worker restart to take effect, unlike the message prompts, which
> honour the alias within the configured cache TTL.

Two different reload semantics in one system is a wart. Documenting it beats
discovering it.

---

## Part 2 — Model routing is one YAML file

### Roles, not model names

Nodes ask for a **role**. They never name a model:

```python
spec = role_spec("draft")
llm = with_resilience(build_llm("draft"), "draft")
```

```yaml
roles:
  research_synth:
    model: "anthropic:claude-sonnet-5"
    max_tokens: 16000
    effort: low

  outline:
    model: "anthropic:claude-haiku-4-5"
    max_tokens: 4000
    effort: low

  draft:
    model: "anthropic:claude-opus-5"
    max_tokens: 32000
    effort: high

  factcheck:
    model: "anthropic:claude-opus-5"
    max_tokens: 16000
    effort: high

  seo:
    model: "anthropic:claude-haiku-4-5"
    max_tokens: 4000
    effort: low
```

Seven roles across three model tiers. The economics are the point: Haiku writes
the SEO description, Opus writes the post. Changing that is a one-line edit.

The comments carry the constraints that would otherwise be lost:

```yaml
  # Bound to the web_search_20260209 / web_fetch_20260209 server tools, which
  # require Opus 4.6+ or Sonnet 4.6+ — Haiku 4.5 rejects both type strings.
  # No fallback is declared on purpose: a local model cannot run server tools,
  # so falling back would produce an unsourced brief instead of a failure.
  search_tool:
    model: "anthropic:claude-sonnet-5"
```

**A fallback that silently produces worse output is worse than no fallback.** An
unsourced research brief looks exactly like a sourced one until you check the
citations.

### Fallback chains

```yaml
fallbacks:
  "anthropic:claude-opus-5":
    - "anthropic:claude-sonnet-5"

  "anthropic:claude-haiku-4-5":
    - "ollama:qwen3.5:9b"
```

Opus degrades to Sonnet. Haiku degrades to a local model — so a cheap role
survives a provider outage entirely. And a rule that is easy to get wrong:

> Fallback targets are registered as gateway endpoints too — a fallback that
> exists in config but not in the gateway is worse than none, since it only
> fails once the primary is already failing.

### Effort mapping, which is honest about doing nothing

```python
def _provider_kwargs(provider: str, spec: dict) -> dict:
    effort = spec.get("effort", "low")
    kwargs: dict = {}
    if provider == "anthropic" and effort == "high":
        # Adaptive thinking on Claude 5-family models. NEVER pass
        # budget_tokens — it 400s on Opus 5.
        kwargs["thinking"] = {"type": "adaptive"}
    logger.debug("effort_requested=%s effort_effective=%s provider=%s",
                 effort, bool(kwargs), provider)
    return kwargs
```

Only `anthropic` + `effort: high` produces anything. Every other combination is a
no-op — and it is *logged as requested-vs-effective* so the config never appears
to do something it does not.

A config knob that silently does nothing on some providers is a trust problem.
Logging the effective value fixes it for the price of one line.

### Parsing gotcha

```python
"""str.split(':', 1) — never bare split(':') — Ollama
tags legitimately contain colons (e.g. 'ollama:qwen3.5:9b')."""
```

Small, and it will bite you exactly once.

---

## Part 3 — One proxy in front of everything

```yaml
gateway:
  enabled: true
  base_url: ""     # derive from MLFLOW_TRACKING_URI + "/gateway"
  surfaces:
    anthropic: passthrough
    gemini: openai
    openai: openai
    ollama: openai
```

Every LLM call leaves the worker addressed to `http://mlflow:5000/gateway`. What
that buys:

1. **Credentials off the workers.** Keys live encrypted in the gateway, pushed
   there from TinyDB by `make register-gateway`.
2. **One place that prices a call** — so the ledger and the budget agree by
   construction ([post 3](03-observability.md)).
3. **Budgets and rate limits** enforced where no code path can bypass them.
4. **Per-endpoint usage** in one UI.

The empty `base_url` is a small nice touch — the MLflow host is configured in
exactly one place (`docker-compose.yml`) and everything else derives from it.

Two surfaces exist because Anthropic's native body carries three things a
translating proxy would drop: server tools, adaptive-thinking blocks, and
prompt-cache token buckets. `scripts/smoke_gateway.py` asserts all three, *"so a
regression is caught rather than silently degrading cost reporting."*

### The operational constraints this creates

Worth knowing before you copy the design:

- **`--workers 1`.** Budget policies are enforced per uvicorn worker under the
  default `local` tracker strategy. Raising the worker count requires a Redis
  budget tracker first.
- **`MLFLOW_ENABLE_ASYNC_TRACE_LOGGING: "false"`.** Otherwise the cost row lands
  ~4.7s after the response and the per-call read finds nothing.
- **Fail-closed.** Gateway down means jobs fail, measured at 2.2s.
- **Time-window cost attribution**, which is exact only for a single-replica
  worker.

That is a real list of constraints, and they compound — see
[post 7](07-improvement-roadmap.md) for the order in which they have to be
resolved before the worker can scale out.

---

## What you can change without a deploy

| Change | How | Takes effect |
|---|---|---|
| Prompt wording | MLflow UI, move alias | ≤ `cache_ttl_seconds` (300s) |
| Schema field description | MLflow UI | Worker restart |
| Role → model | `config/models.yaml` | Restart |
| Fallback chain | `config/models.yaml` (+ register endpoint) | Restart |
| Effort / max_tokens | `config/models.yaml` | Restart |
| Provider API key | Rotate in TinyDB, `make register-gateway` | Immediate |
| Budget / rate limits | Gateway UI | Immediate |

The top row is the one that matters. Prompt iteration — the thing you do twenty
times a day while tuning an LLM product — costs an alias move and five minutes.

---

**Next:** [Security and Secrets](06-security-and-secrets.md).
