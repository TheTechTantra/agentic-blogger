# 1. Agentic Architecture: Eight Nodes, One Loop, Two Containers

*Part 1 of the [Agentic Blogger series](README.md).*

---

## The shape of the problem

"Write a blog post about X" is not one LLM call. It is:

- find out what is actually true about X, from primary sources, today
- decide what a post about X should contain and in what order
- write it
- check whether the writing invented anything
- fix what it invented
- produce metadata
- turn it into safe HTML
- put it somewhere, exactly once

Each step has a different cost profile, a different failure mode, and a different
right-sized model. Research wants a model with server-side web search. Drafting
wants the strongest model you can afford. Producing an SEO description wants the
cheapest model that can count to 160.

A single prompt cannot express that. A linear chain cannot express the one place
the work loops back on itself. A graph can.

## Why LangGraph and not a chain

Three properties decided it:

**1. A conditional edge.** Fact-check either sends the draft back for revision or
lets it through. That is a runtime branch on model output, not a static
pipeline step.

**2. Durable execution.** These runs cost real money — a draft node on Opus with
adaptive thinking and 32K output tokens is not something you want to re-run
because the container restarted at the publish step. LangGraph's
`PostgresSaver` checkpoints state after every node, so a resumed job re-enters
at the last completed node, not at the top.

**3. A typed state contract between steps.** Nodes return only what they change,
and LangGraph merges the delta.

```python
class BlogState(TypedDict, total=False):
    job_id: str
    topic: str
    source_url: Optional[str]
    sources: Annotated[list[dict], operator.add]   # appended, never replaced
    research_brief: Optional[dict]
    outline_plan: Optional[dict]
    draft_markdown: Optional[str]
    draft_title: Optional[str]
    draft_id: Optional[str]
    factcheck_report: Optional[dict]
    revision_count: int
    seo_meta: Optional[dict]
    html: Optional[str]
    published: Optional[dict]
```

Two details in there are load-bearing.

`sources` is `Annotated[list[dict], operator.add]`. Every other key is
last-write-wins; this one accumulates. Research can run twice (retry, resumed
checkpoint) and the source list grows rather than being clobbered.

And the state keys deliberately do not match the node names. There is no
`research` key, there is `research_brief`. LangGraph rejects a state key equal to
a node name, and the error you get when you try is not obvious.

Sub-objects are plain dicts, not Pydantic models. The checkpointer serializes
dicts without ceremony, and nodes validate at their own boundary anyway — the
model classes exist for `.with_structured_output()`, not for passing state
around.

## The topology

```text
                                       ┌────────────────┐
                                       │                ▼
research ──► outline ──► draft ──► factcheck ──► revise ─┘
                                       │
                                  needs_revision()
                                       │ "continue"
                                       ▼
                                     seo ──► format ──► publish ──► END
```

```python
def build_graph() -> StateGraph:
    g = StateGraph(BlogState)

    for name, fn in NODES.items():
        g.add_node(name, _traced(name, fn), retry=RetryPolicy(max_attempts=3))

    g.set_entry_point("research")
    g.add_edge("research", "outline")
    g.add_edge("outline", "draft")
    g.add_edge("draft", "factcheck")
    g.add_conditional_edges("factcheck", _traced_router(needs_revision),
                             {"revise": "revise", "continue": "seo"})
    g.add_edge("revise", "factcheck")
    g.add_edge("seo", "format")
    g.add_edge("format", "publish")
    g.add_edge("publish", END)

    return g
```

Note `build_graph()` returns an **uncompiled** `StateGraph`. The caller compiles
it with a checkpointer, because the `PostgresSaver` connection has to stay open
for the whole life of the graph's use — so the worker owns it for the process
lifetime:

```python
with PostgresSaver.from_conn_string(psycopg_url()) as checkpointer:
    checkpointer.setup()              # idempotent
    compiled = graph.compile(checkpointer=checkpointer)
    runner = JobRunner(compiled, WORKER_ID)
```

## The nodes

| Node | Model role | LLM? | What it does |
|------|-----------|------|--------------|
| research | `search_tool` (Sonnet 5) → `research_synth` (Sonnet 5) | 2 calls | Server-side web search / fetch, then structure the result into a `ResearchBrief` |
| outline | `outline` (Haiku 4.5) | 1 | 3 title candidates, 4–7 sections, each tagged with a reader tier |
| draft | `draft` (Opus 5, adaptive thinking) | 1 | 1200–1800 words of Markdown, no title heading |
| factcheck | `factcheck` (Opus 5, adaptive thinking) | 1 | Every checkable claim → `supported` / `unsupported` / `contradicted` |
| revise | `draft` role (Opus 5) | 1 | Rewrites against the flagged findings only |
| seo | `seo` (Haiku 4.5) | 1 | Title ≤70 chars, description ≤160, labels, slug |
| format | — | **0** | Markdown → sanitized HTML + beacon comment |
| publish | — | **0** | Blogger insert with beacon reconcile |

Two of the eight nodes make no model call at all. That is not an accident of
scope — sanitization and idempotent publishing are exactly the jobs you do *not*
want a model doing.

## The one loop, and why it is bounded at one

```python
MAX_REVISIONS = 1

def needs_revision(state: dict) -> str:
    findings = (state.get("factcheck_report") or {}).get("findings", [])
    flagged = [f for f in findings if f.get("verdict") in ("unsupported", "contradicted")]
    if flagged and state.get("revision_count", 0) < MAX_REVISIONS:
        return "revise"
    return "continue"
```

The router is four lines. The design decision behind it is bigger than the code:
**fact-check advises, it does not gate.**

An unbounded critique loop against a model that will always find *something* is
a money pump with no exit condition. So the loop runs at most once. If findings
remain after the revision pass, the post ships anyway — with the tally embedded
in the HTML where a human reviewing the Blogger draft will see it:

```html
<!-- factcheck: 2 unsupported, 0 contradicted -->
```

The system's job is to raise the floor and surface doubt. It is not to be the
last word. Since posts land as Blogger **drafts**, never live, a human is always
the final gate.

## The part LangGraph does not do

This is the architectural decision I would most want another team to copy.

LangGraph's checkpointer answers *"where was this run when it stopped?"* It says
nothing about *"which run should I pick up next?"* Those are separate problems,
and conflating them is how you end up with a queue implemented in a framework
that is not a queue.

So job claiming stays in SQL:

```sql
UPDATE app.jobs SET state='RUNNING', lease_owner=:worker_id,
       lease_expires_at = now() + make_interval(mins => :lease_minutes),
       attempt = attempt + 1,
       started_at = COALESCE(started_at, now()),
       updated_at = now()
WHERE id = (
  SELECT id FROM app.jobs
   WHERE (state = 'QUEUED' OR (state = 'RUNNING' AND lease_expires_at < now()))
     AND cancel_requested = false
   ORDER BY priority, created_at
   FOR UPDATE SKIP LOCKED LIMIT 1)
RETURNING id, topic_id, pipeline, attempt, max_attempts, config_snapshot, thread_id
```

`FOR UPDATE SKIP LOCKED` is the whole concurrency story. Two workers racing for
the queue each get a different row or nothing. The lease means a worker that
dies does not strand its job — after 15 minutes the row becomes claimable again,
and because `thread_id` is the LangGraph checkpoint key, the next claim resumes
from the last completed node rather than from `research`.

The retry semantics fall out of that cleanly. On an exception with attempts
remaining, the runner deliberately leaves the job **RUNNING**:

```python
if claimed["attempt"] >= claimed["max_attempts"]:
    logger.error("attempts exhausted — marking FAILED")
    repo.mark_job_state(job_id, "FAILED", failure_class=type(e).__name__,
                         failure_reason=str(e))
else:
    # Leave RUNNING; lease will expire and another claim
    # attempt resumes it from the last good checkpoint.
    logger.info("will be retried on lease expiry (attempt %d/%d, ...)", ...)
```

There is no re-queue step, no retry scheduler, no dead-letter mover. The lease
expiry *is* the retry mechanism. One concept does two jobs.

## Three job outcomes, not two

```text
QUEUED ──► RUNNING ──┬──► PUBLISHED   graph completed (post is a Blogger DRAFT)
                     ├──► FAILED      exception, attempts exhausted
                     ├──► BLOCKED     needs a human, not a retry
                     └──► CANCELLED   /cancel
```

`BLOCKED` is the state most pipelines are missing. An expired Blogger OAuth
grant is not a transient error — retrying it five times with exponential backoff
accomplishes nothing except delay. So the publish node raises a distinct
exception type:

```python
class JobBlocked(Exception):
    """Raised for failures that need a human, not a retry (e.g. expired OAuth grant)."""
```

and the runner catches it before the generic handler and terminates the job
immediately. Recovery is a human re-running the OAuth flow, then `/retry` — and
because the checkpoint survives, the job re-enters at `publish` without
re-spending the Opus draft.

## Two containers that share only a database

The Telegram bot and the orchestrator are separate services, and the boundary is
enforced by import discipline:

> `ingestion/telegram_bot.py` imports only `db/`, `config/`, `secrets/`,
> `security/` — never graph or node code.

The bot writes rows. The worker reads rows. Nothing else crosses. Consequences:

- The bot restarts without touching in-flight jobs.
- A graph-code bug cannot take down the ingestion path.
- Scaling them is independent (the worker's single-replica constraint is a
  worker constraint, not a system constraint).

The cost, paid knowingly: there is no propagated trace context between them.
Correlation across the boundary is by `job_id` in the database, not by a span
link. For a system where the bot's entire job is "insert a row", that is a fine
trade.

## What "agentic" means here

The pipeline is deterministic in structure and non-deterministic in content. The
graph is fixed; there is no planner deciding at runtime which node to run next.
Agency lives in three specific places:

1. **Tool use.** The research node binds Anthropic's `web_search_20260209` and
   `web_fetch_20260318` server tools and lets the model decide what to search
   for and how many times (up to `max_uses: 12`).
2. **The conditional edge.** The model's fact-check output selects the next node.
3. **Structured output as a contract.** Nodes get Pydantic models back, not
   prose to re-parse.

That is a deliberate position. A free-form agent loop that picks its own next
action is harder to price, harder to trace, and harder to explain when it does
something strange. A fixed graph with agentic nodes gives you most of the
capability and all of the auditability.

---

**Next:** [Code Walkthrough](02-code-walkthrough.md) — the segments worth stealing.
