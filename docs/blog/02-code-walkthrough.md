# 2. Code Walkthrough: The Segments Worth Stealing

*Part 2 of the [Agentic Blogger series](README.md).*

Seven pieces of real code from this repo, each one solving a problem you will hit
if you build something similar. All of it is running code, not illustration.

---

## 1. Trace every node without touching any node

The naive approach is a logging line at the top and bottom of each node
function. Eight nodes, sixteen lines, and the ninth node someone adds next month
forgets both.

Instead, wrap at registration time:

```python
def _traced(name: str, fn):
    """Wrap a node with entry/exit/failure logging."""
    def wrapper(state: dict):
        job_id = state.get("job_id", "")
        with bind_job(job_id), bind_node(name):
            started = time.monotonic()
            logger.info("start in: %s", summarize_state(state, _INPUTS.get(name, ())))
            try:
                result = fn(state)
            except Exception as e:
                logger.error("failed after %.1fs: %s: %s",
                             time.monotonic() - started, type(e).__name__, e, exc_info=True)
                raise
            elapsed = time.monotonic() - started
            logger.info("done in %.1fs out: %s", elapsed, summarize_state(result or {}))
            return result

    wrapper.__name__ = f"{name}_traced"
    return wrapper


for name, fn in NODES.items():
    g.add_node(name, _traced(name, fn), retry=RetryPolicy(max_attempts=3))
```

A node cannot opt out, because opting out would mean not being registered.

The `_INPUTS` table is the part people skip, and it is the part that pays:

```python
_INPUTS = {
    "research": ("topic", "source_url"),
    "outline": ("topic", "research_brief"),
    "draft": ("topic", "outline_plan", "research_brief"),
    "factcheck": ("draft_markdown", "research_brief"),
    "revise": ("draft_markdown", "factcheck_report", "revision_count"),
    "seo": ("draft_title", "draft_markdown"),
    "format": ("draft_markdown", "factcheck_report"),
    "publish": ("draft_id", "draft_title", "seo_meta", "html"),
}
```

Declaring what each node *reads* means the entry log answers "was the input
already broken?" before the traceback answers "what broke". If `draft` starts
with `outline_plan=none`, the failure is upstream, and the line before the
stack trace says so.

Sizes, not contents. A draft is tens of kilobytes; the signal is that it exists
and roughly how big it got:

```text
[job=99639782-… node=draft]: done in 41.2s out: draft_markdown=18244ch draft_title=63ch draft_id=…
```

And the router gets its own wrapper, because "which way did fact-check send it"
is the single most useful line for explaining why a run has two factcheck
entries or none:

```python
logger.info("factcheck -> %s (findings=%d flagged=%d revision_count=%d)",
            choice, len(findings), len(flagged), state.get("revision_count", 0))
```

---

## 2. Two-pass research, because one call cannot do both

The research node is the most interesting node in the graph and the only one
that makes two model calls. The reason is a hard constraint:

> A server-tool response and `.with_structured_output()` cannot share one
> invocation.

So pass 1 gathers, pass 2 structures.

```python
_SEARCH_TOOL = {
    "type": "web_search_20260209",
    "name": "web_search",
    "max_uses": 12,
}

_FETCH_TOOL = {
    "type": "web_fetch_20260318",
    "name": "web_fetch",
    "max_uses": 5,
    "max_content_tokens": 120000,
    "citations": {"enabled": True},
}
```

Note what is **absent**: `allowed_callers: ["direct"]`. That omission is a
deliberate three-way trade recorded in the source:

- With it, the tools are directly callable and can be forced via `tool_choice`.
- Without it, dynamic filtering stays on — the tools' internal code-execution
  step discards irrelevant content *before* it reaches the context window, which
  is the only thing bounding a large PDF's token cost (`max_content_tokens` does
  not apply to binary content).
- The cost of leaving it off: these tool versions are not zero-data-retention
  eligible with filtering on, and a forced `tool_choice` naming one is rejected
  with a 400 — *"Tools specified in tool_choice must allow 'direct' calls from
  the model."*

So the node binds the tools unforced and relies on the prompt to make the model
search. Which means it needs a detector for the day a prompt revision loses that
instruction:

```python
if not sources:
    logger.warning("research produced no extractable sources")
```

### Parsing the response defensively

Server-tool responses come back as a list of content blocks. Three separate
failure shapes have actually occurred here, and each has a guard:

```python
def _handle_search_result(block: dict, sources: list[dict]) -> None:
    results = block.get("content", [])
    # A server-tool error arrives as HTTP 200 with content as a dict
    # ({"error_code": ...}) rather than a list — never index it.
    if isinstance(results, dict):
        logger.warning("web_search returned an error block: %s", results.get("error_code"))
        return
    if not isinstance(results, list):
        logger.warning("web_search result content was %s, not a list — no sources extracted",
                       type(results).__name__)
        return
```

And the walk is recursive, not a flat loop over top-level blocks:

```python
def _collect_sources(node, sources: list[dict], depth: int = 0) -> None:
    """Walk the response content for tool-result blocks.

    Recursive rather than a flat loop over top-level blocks: with dynamic
    filtering enabled, the web tools run inside a code-execution container and
    their result blocks arrive nested inside that call's blocks. A flat scan
    finds nothing there, which would look identical to a failed fetch.
    """
```

"Looks identical to a failed fetch" is the reason this matters. A parser bug that
returns zero sources and a model that never searched produce the same empty
list. So the node logs both numbers and lets you tell them apart:

```python
block_count = len(response.content) if isinstance(response.content, list) else 1
logger.info("research returned %d content blocks, %dch prose, %d sources (%d primary_url)",
            block_count, len(raw), len(sources),
            sum(1 for s in sources if s.get("source_type") == "primary_url"))
```

Many blocks and no sources means the parse shape changed. Few blocks and no
sources means the model did not search. Both have happened.

### Attribution enforced in code, not in a prompt

A URL-sourced job explains someone else's work. Two invariants are enforced with
`raise`, not with polite prompt instructions:

```python
if source_url and not any(s.get("source_type") == "primary_url" for s in sources):
    # The whole premise of a /url job is that the page was read. If the
    # fetch failed we have a blind-search brief wearing a URL job's label —
    # fail loudly rather than publish it.
    raise RuntimeError(f"web_fetch did not return content for {source_url}")
```

```python
exemplars = brief.get("code_exemplars") or []
attributed = [e for e in exemplars if e.get("source_url")]
if len(attributed) != len(exemplars):
    logger.warning("dropped %d unattributed code exemplars",
                   len(exemplars) - len(attributed))
    brief["code_exemplars"] = attributed
```

A code example with no traceable origin is exactly what this pipeline must not
publish, so it is dropped at the research boundary rather than trusted not to
survive the draft node.

---

## 3. Per-call cost tracking that survives retries and fallbacks

The obvious design is a global LangChain callback handler. It does not work
here, and the docstring says why:

```python
"""Cost tracking — applied per node call after invoke(), not as a global
LangChain callback. `.with_fallbacks()`/`.with_retry()` wrap the Runnable
opaquely enough that a global handler can't reliably attribute usage back
to (job_id, node_name); reading `response.usage_metadata` directly after
each call is simpler and exactly as accurate.
"""
```

The result is a context manager used identically at every call site:

```python
with track_llm_call(job_id, "factcheck", "factcheck", spec["model"]) as record:
    result = llm.invoke([HumanMessage(content=prompt)])
    record(result["raw"])
```

Three things that make it worth copying:

**It logs before the call, not just after.**

```python
# Printed before the call, not after: a 12-minute research call is
# otherwise a 12-minute gap in the log with nothing saying what is running.
logger.info("llm call start node=%s role=%s model=%s", node_name, role, model_key)
```

**It writes a ledger row on failure too.**

```python
except Exception as e:
    latency_ms = int((time.monotonic() - started) * 1000)
    repo.record_node_run(
        job_id, node_name, provider=provider, model=model, role=role,
        latency_ms=latency_ms, prompts=drain(),
        error_class=type(e).__name__, error_message=str(e),
    )
    raise
```

A failed call is data. Losing it means your ledger silently under-reports
exactly the runs you most want to investigate.

**It asks about every model the call could have reached, not just the primary.**

```python
cost = (
    cost_for_call([model_key, *fallback_specs(model_key)], started_ms, ended_ms)
    if usage else Decimal("0")
)
```

A Haiku call that fell back to Ollama spent money under both names, and both
belong on the node's row.

---

## 4. Reading cost back from the gateway instead of computing it

There is no rate table in this repo. Not one. The gateway prices every call
server-side, and `llm/cost.py` reads that number back:

```python
body = {
    "experiment_ids": [experiment_id],
    "view_type": "SPANS",
    "metric_name": "total_cost",
    "aggregations": [{"aggregation_type": "SUM"}],
    # Only '=' is supported here, which is why one query per model is
    # needed rather than a prefix match on 'provider/'.
    "filters": [f"span.name = '{span_name}'"],
    "start_time_ms": start_ms,
    "end_time_ms": end_ms,
}
r = httpx.post(f"{_tracking_uri()}/api/3.0/mlflow/traces/metrics", json=body, timeout=15.0)
```

The span-name filter is what keeps the number honest, and the reason is subtle:

> MLflow's server prices *every* trace that carries a model and token usage, so
> the client-side `mlflow.langchain.autolog()` spans (named `ChatAnthropic`)
> carry a cost of their own for the very same call. Summing without the filter
> double-counts. Only `provider/...` spans are the gateway's.

Two more things make this work in practice.

**Synchronous trace export.** The mlflow service runs with
`MLFLOW_ENABLE_ASYNC_TRACE_LOGGING: "false"`. Without it the cost row lands ~4.7
seconds after the response, and the read finds nothing.

**A short poll with a documented reason for being short:**

```python
# It is deliberately short. A model the gateway cannot price — Ollama, which is
# in no price catalog — produces a span with no cost row at all, and that is
# indistinguishable from a row that has not been written yet. So every locally
# served call pays this timeout in full. One second is long enough to cover a
# slow write and short enough not to matter on a fallback path.
_POLL_TIMEOUT_S = 1.0
```

And the failure policy is stated outright:

```python
"""Returns Decimal("0") rather than raising on any failure. A cost read must
never fail a call that already succeeded: the money is spent either way,
and losing the figure is strictly better than losing the work."""
```

The honest limitation, documented in the module itself: attribution is by
**time window**, not request id, because the gateway returns neither a trace id
nor a cost header. That is exact only while the orchestrator is a single-replica
worker running one node at a time. It is written down in the source precisely so
nobody scales the worker without reading it.

---

## 5. Routing every call through one proxy, with two surfaces

Every model built in this system is addressed to the MLflow AI Gateway. The
worker holds no provider key at all:

```python
# Handed to every client as its API key. The real credential lives in the
# gateway's encrypted store and is attached server-side; the clients only need
# a non-empty string to pass their own constructor validation.
_GATEWAY_PLACEHOLDER_KEY = "mlflow-gateway"
```

The interesting function is the router, which decides not just *where* a call
goes but *which client library speaks for it*:

```python
def _gateway_route(provider: str, model: str) -> tuple[str, dict]:
    gw = gateway_spec()
    base = gw["base_url"]
    surface = (gw["surfaces"] or {}).get(provider, "openai")

    if surface == "passthrough":
        return provider, {"base_url": f"{base}/{provider}"}
    return "openai", {"base_url": f"{base}/mlflow/v1"}
```

Anthropic gets the passthrough surface because three things the pipeline depends
on exist only in the provider-native request body and would be lost to a
translating proxy:

1. the `web_search` / `web_fetch` **server tools** that *are* the research node
2. **adaptive thinking** block lists, which `llm/text.py:extract_text` parses
3. the **prompt-cache token buckets** the gateway prices separately

Everything else takes the unified OpenAI-compatible surface, where one client
speaks for all of them.

The convention that makes the whole thing cheap: **gateway endpoints are named
after model ids, not roles.** Both call surfaces read a request's `model` field
as the endpoint name, so the clients keep sending exactly what they always sent,
and `base_url` is the only thing that changed. Cost attribution keyed on
`provider:model` keeps working untouched.

One exception, and it lives in its own module for a reason:

```python
def endpoint_name(model: str) -> str:
    """Ollama is the exception. Its tags contain a colon ('qwen3.5:9b') and the
    gateway rejects colons in names, so those get rewritten to a hyphen."""
    return _INVALID.sub("-", model)
```

```python
"""There is exactly one thing in here, and it exists because two places must agree
on it: scripts/register_gateway.py, which names the endpoints, and
llm/registry.py, which addresses them. If those two ever disagree, every call
fails with "endpoint not found" and nothing in either file looks wrong."""
```

That last sentence is the whole argument for extracting a four-line function
into its own module.

---

## 6. Wrapping order, which is not optional

This bit bites everyone once:

```python
def build_llm(role: str) -> Runnable:
    """Build the raw model Runnable for a config role — NOT wrapped in
    retry/fallbacks yet. Call .bind_tools()/.with_structured_output() on
    this first if the node needs it, then pass the result to
    with_resilience(). Wrapping order matters: RunnableRetry and
    RunnableWithFallbacks don't forward bind_tools()/with_structured_output()
    — those only exist on the underlying chat model."""
```

So every node follows the same three-step shape:

```python
llm = build_llm("factcheck").with_structured_output(bound(FactCheckReport), include_raw=True)
llm = with_resilience(llm, "factcheck")
```

And a fallback that cannot even be constructed never takes the primary path down:

```python
for fb in fallback_specs(spec["model"]):
    try:
        fallback_llms.append(_build_raw(fb, max_tokens, effort))
    except Exception:
        # A fallback that can't even be constructed (missing package,
        # unset key, unreachable host) must never take down the primary
        # path — log and skip it instead.
        logger.warning("Skipping unavailable fallback %r for role=%s", fb, role, exc_info=True)
```

### The retry multiplication bug

Worth its own callout, because the arithmetic is brutal:

```python
# Retry is with_resilience()'s job, and only its job. The provider SDKs
# retry internally by default (the Anthropic client, 2 by default), and
# that count MULTIPLIES with the `resilience.max_attempts` retry wrapped
# around it -- 5 x 3 = 15 upstream calls for one node. On a gateway
# timeout each of those is a full provider generation that is billed and
# then abandoned, and the gateway records no usage row for a call it
# never got a response from, so the spend is invisible to the budget too.
provider_init_kwargs.setdefault("max_retries", 0)
```

Fifteen billed Opus generations for one node, none of them visible to the budget.
The fix is one line; finding it is not.

The same shape appears on the prompt path — MLflow's REST client retries
internally, so wrapping it in the shared policy produced 35 HTTP attempts per
prompt and turned a registry outage into a five-minute hang. That path calls
MLflow unwrapped and tunes `MLFLOW_HTTP_REQUEST_*` instead.

**Rule of thumb: audit every layer between you and the socket for its own retry
default before you add yours.**

---

## 7. The formatter, which is a security boundary

The `format` node makes no model call. It turns Markdown into HTML that is about
to be posted to a public platform, which makes it the place sanitization belongs:

```python
_md = MarkdownIt("commonmark", {"html": False}).enable("table")

raw_html = _md.render(markdown)
safe_html = bleach.clean(raw_html, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)
```

`{"html": False}` blocks raw HTML in the Markdown; `bleach.clean` with an
allowlist catches whatever the renderer emits. Belt and braces, because the
input is model-generated text influenced by fetched web pages.

Then the two comments that matter downstream:

```python
beacon = f"<!-- agentic-blogger:job={job_id} v=1 -->"
factcheck_comment = f"<!-- factcheck: {unsupported} unsupported, {contradicted} contradicted -->"
full_html = f"{beacon}\n{factcheck_comment}\n{safe_html}"
```

And a single log line that makes a silent failure loud:

```python
# Sanitization is silent by design, so the only way to notice bleach
# eating content (a tag outside the allowlist) is the before/after delta.
logger.info("formatted markdown=%dch html=%dch (sanitizer dropped %dch) ...",
            len(markdown), len(full_html), len(raw_html) - len(safe_html), ...)
```

Sanitizers do not raise. They delete. If you do not measure the delta, a
mysteriously truncated post has no evidence trail at all.

---

## Bonus: the one-liner that prevents a real bug

Adaptive thinking turns `response.content` into a list of blocks. Calling `str()`
on it stringifies the thinking block — *including its signature* — into your
published post.

```python
def extract_text(content) -> str:
    if isinstance(content, str):
        return content

    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "\n".join(parts)
```

Every node that reads free text from a response goes through this. None of them
touch `response.content` directly.

---

**Next:** [Observability](03-observability.md) — what you can actually see when a run goes wrong.
