# 4. Reliability and Idempotency: Leases, Checkpoints, and a Beacon

*Part 4 of the [Agentic Blogger series](README.md).*

A pipeline where a single run costs dollars and takes fifteen minutes has a
different reliability profile from a web request. Re-running is expensive.
Double-publishing is embarrassing. Hanging forever is indistinguishable from
working. This post is the four mechanisms that handle that, and the bugs that
produced each one.

---

## 1. Leases: how a dead worker gives its job back

Claiming is a single atomic statement:

```sql
UPDATE app.jobs SET state='RUNNING', lease_owner=:worker_id,
       lease_expires_at = now() + make_interval(mins => :lease_minutes),
       attempt = attempt + 1, ...
WHERE id = (
  SELECT id FROM app.jobs
   WHERE (state = 'QUEUED' OR (state = 'RUNNING' AND lease_expires_at < now()))
     AND cancel_requested = false
   ORDER BY priority, created_at
   FOR UPDATE SKIP LOCKED LIMIT 1)
```

Read the `WHERE` clause carefully — it defines two things at once:

- a **queue** (`state = 'QUEUED'`)
- a **recovery mechanism** (`state = 'RUNNING' AND lease_expires_at < now()`)

A worker that segfaults mid-draft does not need a supervisor to notice. Its
lease simply stops being renewed, and fifteen minutes later the row is claimable
again. `FOR UPDATE SKIP LOCKED` makes concurrent claims safe without a
distributed lock.

This is also, as covered in [post 1](01-agentic-architecture.md), the retry
mechanism: on a retryable failure the runner leaves the job `RUNNING` and lets
the lease expire.

---

## 2. The heartbeat, and the bug it fixed

The lease is 15 minutes. The research node can spend 12 minutes inside a single
API call. Add a draft node on Opus and the total run comfortably exceeds the
lease — which meant a job that was running perfectly became claimable while
still running.

The bug report is in the source:

```python
# claim_job stamps a 15-minute lease and nothing renewed it, so any job whose
# nodes together outran that window became claimable while still running. The
# single-threaded worker loop was the only thing hiding it: run_one() blocks
# until the graph returns, so one worker never polled while busy. A second
# replica would have double-run the job — duplicate LLM spend, and a publish
# node racing itself against Blogger.
```

**A latent bug masked by a deployment accident.** Single-replica was a
constraint adopted for a different reason (secrets vault concurrency), and it
happened to hide this. Adding one replica would have surfaced it as duplicate
posts and duplicate spend.

The fix is a daemon thread spanning the whole invocation:

```python
_LEASE_MINUTES = 15
_HEARTBEAT_SECONDS = 300

@contextlib.contextmanager
def _lease_heartbeat(job_id: str, worker_id: str):
    """Keep a job's lease alive for as long as the body is running."""
```

Note the scope decision:

> The renewal spans the whole `invoke()` rather than firing between nodes,
> because a single node can outlast the lease on its own.

An inter-node heartbeat would not have fixed it.

### Losing the race gracefully

The renewal is conditional on still owning the lease:

```sql
UPDATE app.jobs SET lease_expires_at = now() + make_interval(mins => :m), updated_at = now()
WHERE id = :id AND state = 'RUNNING' AND lease_owner = :worker_id
```

```python
"""The worker_id guard matters: claim_job hands a RUNNING job whose lease has
expired to whoever asks next, so a worker that stalled past its lease can
find the job reassigned underneath it. Renewing unconditionally would let
that stalled worker yank the lease back from the worker now legitimately
running the job, and both would drive the same graph. A False return means
we lost the race and must stop touching the job."""
```

```python
if not repo.heartbeat_lease(job_id, worker_id, _LEASE_MINUTES):
    logger.error("lease lost by worker=%s — stopping heartbeat", worker_id)
    return
```

And a transient DB blip must not kill a job that is otherwise fine:

```python
except Exception:
    # A transient DB blip must not kill the job that is running
    # fine; the next tick retries well inside the lease window.
    logger.warning("lease heartbeat failed — will retry", exc_info=True)
```

300-second beat against a 900-second lease gives two free misses. That ratio is
the whole tuning story.

The heartbeat also doubles as proof of life:

```python
logger.info("lease renewed (beat=%d, +%dmin, elapsed=%dmin)", ...)
```

> Proof of life for a job that is otherwise silent for the 12 minutes the
> research node spends inside one API call.

---

## 3. Checkpoints: making resume cheap

`PostgresSaver` persists state after each node, keyed by `thread_id` — a column
on the job row. Resume is then trivial:

```python
if resumed:
    logger.info("invoking graph (resuming from checkpoint)")
    final_state = self.compiled_graph.invoke(None, config=config)
else:
    initial_state = {...}
    final_state = self.compiled_graph.invoke(initial_state, config=config)
```

`invoke(None, config)` means "continue from wherever this thread got to."

The economics: a job that dies at `publish` resumes at `publish`. Research,
draft, fact-check and revise — the entire expensive half — are not re-run. For a
`BLOCKED` job waiting on an OAuth re-authorization, that is the difference
between a free retry and paying for an Opus draft twice.

The checkpointer connection is opened once and held for the process lifetime,
which is why `build_graph()` returns an *uncompiled* graph and the worker does
the compiling.

---

## 4. The beacon: inventing an idempotency key

Here is the hardest problem in the system.

`posts.insert` accepts **no idempotency key**. And a crash between the API call
and the checkpoint write leaves no local record that it happened. So after a
timeout you are stuck with a genuinely undecidable question: did that post get
created or not?

Retrying risks a duplicate. Not retrying risks losing the work.

The answer is to put the key *in the content*, because the content is the only
thing that survives into the remote object. The format node embeds it:

```python
beacon = f"<!-- agentic-blogger:job={job_id} v=1 -->"
```

and the publish path looks for it before inserting:

```python
def _find_by_beacon(service, blog_id: str, job_id: str, title: str) -> dict | None:
    """Reconcile path: look for a DRAFT post already carrying this job's
    beacon (a prior attempt that succeeded remotely but crashed before we
    recorded it)."""
    beacon = f"agentic-blogger:job={job_id}"
    resp = service.posts().list(blogId=blog_id, status=["DRAFT"], fetchBodies=True,
                                 view="AUTHOR").execute()
```

This is why the architecture doc insists the beacon *"is not cosmetic"* and why
`format` must run before `publish` with its output reaching Blogger unmodified.
An HTML comment is load-bearing infrastructure.

### Retry the reconcile, not the insert

The subtlety that makes it correct:

```python
def publish_draft(job_id: str, title: str, html: str, labels: list[str]) -> dict:
    """Retries the WHOLE reconcile-then-insert attempt, not just the insert
    call — each retry re-checks the beacon first, so a prior attempt that
    actually succeeded server-side (but errored client-side) is adopted
    instead of double-posted."""
```

Wrapping only the insert would double-post on the first retry. Wrapping the pair
makes every attempt self-correcting.

### Turning off the library's retry

```python
.execute(num_retries=0)
```

> `num_retries=0` is mandatory: googleapiclient's built-in retry does a blind
> re-POST, which is precisely the double-post this design prevents.

A library retry that does not know about your idempotency scheme is worse than
no retry. Same lesson as the LLM client's `max_retries=0` in
[post 2](02-code-walkthrough.md) — **audit every layer for its own retry
default.**

### The database half

The publish node commits a `PENDING` row *before* touching Google:

```python
existing = repo.get_pending_publication(job_id)
if existing and existing["state"] in ("DRAFT", "LIVE"):
    logger.info("already published (state=%s post_id=%s) — skipping insert", ...)
    return {"published": {...}}

blog_id = SecretStore().get("BLOGGER_BLOG_ID")
repo.insert_publication_intent(job_id, draft_id, blog_id, request_id=job_id)
```

Two guards, deliberately at different layers: the local row catches the common
case cheaply, the remote beacon catches the case where the local row never got
written.

---

## 5. Retry policy, in one place, with one documented exception

```yaml
resilience:
  max_attempts: 5
  initial_backoff_s: 1
  max_backoff_s: 30
  jitter: true
```

Two adapters, because the call sites use two different mechanisms:

```python
"""  - `tenacity_kwargs()` for plain function calls (the Blogger client).
  - `runnable_retry_kwargs()` for LangChain Runnables, whose `.with_retry()`
    accepts only an attempt count and a jitter flag. The backoff bounds are
    honoured on the tenacity path and silently unavailable on the LangChain
    one — that asymmetry is LangChain's, not ours."""
```

Naming the limitation beats pretending the config applies uniformly.

MLflow is excluded on purpose:

```python
"""MLflow is the deliberate exception. Its REST client retries internally
(MLFLOW_HTTP_REQUEST_MAX_RETRIES, default 7), so wrapping it here multiplied
out to 35 attempts per call and made a registry outage take minutes to
surface."""
```

And preflight aborts on the first unreachable prompt rather than grinding
through all 21:

```python
# A load failure means the registry is unreachable, and the
# remaining lookups would fail identically after the same retry
# budget each. Stop here: 21 serial timeouts turn a fast failure
# into a multi-minute hang.
break
```

Full outage now fails in ~17 seconds instead of minutes. **Fast failure is a
reliability feature** — a system that takes five minutes to tell you it is down
is one you cannot operate.

### Predicates stay at the call site

```python
"""Retry predicates stay with the call site. A uniform "retry everything"
policy would be wrong in at least one place that matters: the Blogger client
must not retry an expired OAuth grant."""
```

The Blogger predicate contains a genuinely surprising entry:

```python
_RETRYABLE_HTTP_STATUS = {400, 403, 429, 500, 502, 503, 504}
```

A retryable 400. The reason is empirical:

> Google-side throttling was observed empirically to surface as HTTP 400
> `reason=badRequest` ("Request contains an invalid argument") after heavy
> `posts.insert` traffic, with identical minimal payloads failing uniformly once
> volume climbed.

Which forced a diagnostic investment, because that body names no field:

```python
def _describe(exc: HttpError) -> str:
    """Everything Blogger told us about a failure, on one line.

    The generic 400 body ("Request contains an invalid argument") names no
    field, so the reason code and the raw body are the only discriminators
    between a throttle and a malformed payload"""
```

plus logging the payload dimensions before every insert:

```python
# The payload dimensions are what any future 400 investigation needs, and
# they are not recoverable after the fact — the request is not stored.
logger.info("job=%s inserting draft blog_id=%s title=%dch html=%dch labels=%d/%dch", ...)
```

---

## 6. Timeouts: the failure mode with no error

```yaml
defaults:
  timeout_s: 600
```

```python
# Client-side ceiling on a call that now crosses an extra hop. Unset, a
# gateway that accepts the connection and then stalls hangs the worker for
# as long as the process lives; the pipeline's longest legitimate call
# (draft, factcheck) is minutes, not unbounded.
provider_init_kwargs.setdefault("timeout", timeout_s())
```

This config existed and was *dead* until the gateway landed. Adding a proxy hop
turned an unset timeout from a theoretical issue into an unbounded hang with no
exception, no log line, and a held lease.

---

## 7. Distinguishing "retry" from "get a human"

Not every failure benefits from backoff:

```python
class JobBlocked(Exception):
    """Raised for failures that need a human, not a retry (e.g. expired OAuth grant)."""
```

```python
except InvalidGrantError as e:
    logger.error("publish blocked on expired/revoked Blogger grant")
    repo.update_publication(job_id, state="FAILED", error_message=str(e))
    raise JobBlocked(
        "Blogger refresh token expired/revoked — re-run scripts/blogger_authorize.py"
    ) from e
```

The exception message names the recovery script. When it surfaces via `/status`
at 2am, the operator does not have to go find the runbook.

The runner has a third terminal class too — a prompt registry failure is not
retryable either:

```python
except PromptRegistryError as e:
    # Registry is the only source of prompt text and has no
    # fallback by design: a job that cannot read its prompts is
    # terminal, not retryable. Fix the registry, re-create the job.
```

Three failure classes, three behaviours: retry with backoff, fail permanently,
wait for a human. Most systems have one.

### Preflight: fail before you spend

```python
"""Runs before the graph is invoked. The point is spend: without it, a prompt
edited in the MLflow UI to rename a variable would sail through research on
Opus and only fail at the draft node, after the expensive calls. Failing here
costs one round-trip per prompt and nothing else."""
```

```python
logger.info("prompt preflight starting")
check_all()
logger.info("prompt preflight ok")
```

**Order your validations by the cost of the work they protect.**

---

## The full defence, in order

| Failure | Caught by | Cost of recovery |
|---|---|---|
| Bad prompt template | Preflight, before any spend | One round trip |
| Transient provider error | `with_resilience` (5 attempts, jitter) | Seconds |
| Node exception | LangGraph `RetryPolicy(max_attempts=3)` | In-process |
| Worker crash | Lease expiry + checkpoint resume | ≤15 min, no re-spend |
| Long-running job | Lease heartbeat every 300s | None |
| Gateway stall | 600s client timeout | One node |
| Timed-out-but-succeeded publish | Beacon reconcile | One `posts.list` |
| Expired OAuth | `JobBlocked` → state `BLOCKED` | Human + `/retry` |
| Registry outage | `PromptRegistryError` → `FAILED` | Fix registry, re-create job |

Nine layers. Each one exists because of a specific incident or a specific piece
of arithmetic, and each one is documented at the place it acts.

---

**Next:** [Prompts and Models as Configuration](05-prompts-and-models-as-config.md) — changing what the system writes without deploying anything.
