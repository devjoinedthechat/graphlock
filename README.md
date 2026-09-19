<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <img src="assets/logo-light.svg" alt="" width="84" height="94">
  </picture>
</p>

<h1 align="center">graphlock</h1>

<p align="center">
  <b>Deploy LangGraph changes without breaking the threads that are waiting.</b><br>
  A CI check that catches graph changes that strand, crash or corrupt paused threads, a scan that
  names the stored threads a deploy would break, and migrations that repair them.
</p>

<p align="center">
  <a href="https://github.com/devjoinedthechat/graphlock/actions/workflows/ci.yml"><img src="https://github.com/devjoinedthechat/graphlock/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <img src="https://img.shields.io/badge/python-3.10%20%E2%80%93%203.14-blue" alt="Python 3.10–3.14">
  <img src="https://img.shields.io/badge/LangGraph-1.0%20%E2%80%93%201.2-1c3c3c" alt="LangGraph 1.0–1.2">
  <img src="https://img.shields.io/badge/tests-308-brightgreen" alt="308 tests">
  <img src="https://img.shields.io/badge/license-Apache--2.0-blue" alt="Apache-2.0">
  <img src="https://img.shields.io/badge/status-pre--alpha-orange" alt="Status: pre-alpha">
</p>

<p align="center">
  <a href="#what-langgraph-does-today">What LangGraph does today</a> ·
  <a href="#quickstart">Quickstart</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#rules">Rules</a> ·
  <a href="#migrations">Migrations</a> ·
  <a href="#evidence">Evidence</a> ·
  <a href="#scale">Scale</a> ·
  <a href="#what-it-does-not-do">Limits</a> ·
  <a href="#development">Development</a>
</p>

---

A refund agent drafts a refund, then waits in `interrupt()` for a manager. Managers take days, so
at any moment hundreds of threads sit paused in the checkpointer. A pull request renames the
approval step from `wait_for_manager_approval` to `manager_review`, the tests pass, and it ships.

When a manager approves one of the waiting refunds, LangGraph resumes the thread under the new
code. It finds no node called `wait_for_manager_approval`, treats the thread as finished, and
returns. There is no error or log line, and the refund is never issued.

graphlock catches it in the pull request:

```
$ graphlock check
refunds
  ✗ GL101 node-removed  wait_for_manager_approval
      Node 'wait_for_manager_approval' is gone. Threads paused at it, or with pending work for it,
      will resume as if finished and it will never run.
      → It looks renamed to 'manager_review': add rename_node('wait_for_manager_approval',
      'manager_review').
  ✗ GL201 required-field-added  currency
      State field 'currency' is required and has no default. Stored threads without a value for it
      fail validation at the next node.
      → Give it a default, or add set_default('currency', ...).

2 breaking.
Stored threads can break. `graphlock scan` shows which ones; add a migration, drain them, or run `graphlock lock` to accept the change.
```

It then names the stored threads the change would break:

```
$ graphlock scan --sqlite refunds.sqlite
refunds: 5 threads, 3 paused mid-run
  ✗ GL101 node-removed  wait_for_manager_approval — 3 threads
      Waiting to run 'wait_for_manager_approval', which no longer exists. The thread will resume as
      if finished and nothing after it will run.
      threads: refund-1, refund-3, refund-4
  ✗ GL201 required-field-added  currency — 3 threads
      State field 'currency': Field required. Building the state for the next node fails.
      threads: refund-1, refund-3, refund-4
  ! GL201 required-field-added  currency — 2 threads
      State field 'currency': Field required. Building the state for the next node fails. The thread
      has finished, so this only matters if it is continued.
      threads: refund-2, refund-5
  3 threads will break if you deploy this graph.
```

Two migrations repair those threads, and they resume correctly under the new graph:

```python
MIGRATIONS = [
    rename_node("wait_for_manager_approval", "manager_review"),
    set_default("currency", "USD"),  # every refund before this deploy was in dollars
]

graph = graphlock.with_migrations(builder.compile(checkpointer=saver), MIGRATIONS)
```

## What LangGraph does today

A checkpoint doesn't record where a thread is in the graph. It stores channel values under names
derived from your code:
- `branch:to:<node>` for each pending step;
- `join:<a>+<b>:<node>` for each fan-in;
- one channel per state field;
- stored objects under their class's import path.

On resume, LangGraph rebuilds the thread from whichever of those names the new code still has, and
ignores the rest.

Each row below pauses a real thread, deploys a changed graph and resumes it.
[`scripts/evidence.py`](scripts/evidence.py) produces the table by running the scenarios against
the installed LangGraph; none of it is typed by hand.

LangGraph 1.2.11, `SqliteSaver`:

| Change deployed while a thread is paused | LangGraph today | `check` | `scan` | Repair |
|---|---|---|---|---|
| Rename the node a thread is paused before | **wrong result, no error** | GL101 | GL101 | `rename_node` ✓ |
| Rename a node that is waiting in interrupt() | **wrong result, no error** | GL101 | GL101 | `rename_node` ✓ |
| Remove the node a thread is paused before | **wrong result, no error** | GL101 | GL101 | — |
| Rename a node that pending Send()s are addressed to | **wrong result, no error** | GL101 | GL101 | `rename_node` ✓ |
| Rename a node that finished in parallel with one now waiting in interrupt() | **wrong result, no error** | GL101 | GL101 | `rename_node` ✓ |
| Rename a subgraph node while a thread is paused inside it | **wrong result, no error** | GL101 | GL101 | — |
| Rename the node a thread is waiting in, inside a subgraph | **wrong result, no error** | GL101 | GL101 | `rename_node` ✓ |
| Swap two interrupt() calls inside a subgraph while a thread sits between them | **wrong result, no error** | GL401 | GL401 | — |
| Turn on defer= for a node a thread is paused before ([langgraph#8629](https://github.com/langchain-ai/langgraph/issues/8629)) | crashes on resume | GL102 | GL102 | `defer_changed` ✓ |
| Turn off defer= for a node a thread is paused before | resumes correctly | (GL102) | — | — |
| Turn on defer= for a fan-in node while its barrier is half full ([langgraph#8618](https://github.com/langchain-ai/langgraph/issues/8618)) | crashes on resume | GL102 | GL102 | `defer_changed` ✓ |
| Turn off defer= for a fan-in node while its barrier is half full ([langgraph#8618](https://github.com/langchain-ai/langgraph/issues/8618)) | crashes on resume | GL102 | GL102 | `defer_changed` ✓ |
| List a fan-in's sources in a different order while its barrier is half full | **wrong result, no error** | GL103 | GL103 | `rename_channel` ✓ |
| Add a required field to Pydantic state | crashes on resume | GL201 | GL201 | `set_default` ✓ |
| Change a Pydantic state field from str to int | crashes on resume | GL202 | GL202 | `convert_field` ✓ |
| Rename a class whose objects are stored in state | **wrong result, no error** | GL301 | GL301 | `revive` ✓ |
| Swap two interrupt() calls while a thread sits between them | **wrong result, no error** | GL401 | GL401 | — |
| Add a new interrupt() after the existing ones | resumes correctly | (GL402) | — | — |
| Point the edge out of the paused node somewhere else | resumes correctly | — | — | — |
| Add a node after the paused one | resumes correctly | — | — | — |
| Remove a state field while a thread is paused at a breakpoint | **wrong result, no error** | GL203 | GL203 | `drop_field` ✓ |
| Remove a state field while a thread waits in interrupt() | resumes correctly | (GL203) | — | — |
| Remove a state field while a thread is paused at an interrupt_after breakpoint | resumes correctly | (GL203) | — | — |
| Remove a state field while a thread waits in interrupt() before a breakpoint | **wrong result, no error** | GL203 | GL203 | `drop_field` ✓ |
| Add a reducer to a state field | resumes correctly | (GL204) | — | — |

Eighteen of the twenty-five changes break a paused thread, and **thirteen of those raise no error**:
the thread finishes early, waits forever, or carries on with wrong data. A rule in parentheses is
reported but doesn't fail the check. LangGraph 1.0.0 and 1.1.0 behave the same on every row.

Four of them are easy to walk into:
- **Reordering `add_edge(["x", "y2"], "join")` to `["y2", "x"]`** renames the fan-in's channel. A
  thread where `x` already finished waits for it forever, and `join` never runs.
- **Swapping two `interrupt()` calls** gives the new first question the answer to the old first
  question. In the corpus the amount ends up as `alice` and the approver as `500`.
- **Renaming a class** that state stores means old objects come back as plain dicts. The
  serializer catches the failed import and returns the object's fields, so the next node gets a
  dict where it expected an `Order`.
- **Removing a state field** freezes threads at `interrupt_before` breakpoints, whether they are
  paused there at deploy time or reach one later. On resume, LangGraph marks only the new graph's
  channels as seen by the breakpoint
  ([`_loop.py:948`](https://github.com/langchain-ai/langgraph/blob/daa514a98863fbe555aeb8a8c7255fc48d06e037/libs/langgraph/langgraph/pregel/_loop.py#L948)).
  But it checks every channel stored in the checkpoint
  ([`_algo.py:155`](https://github.com/langchain-ai/langgraph/blob/daa514a98863fbe555aeb8a8c7255fc48d06e037/libs/langgraph/langgraph/pregel/_algo.py#L155)),
  so the removed field always looks updated and the thread pauses again on every resume.

## Quickstart

graphlock is not on PyPI yet. Install it from GitHub into the environment your graph runs in:

```sh
pip install "graphlock @ git+https://github.com/devjoinedthechat/graphlock"
```

**1. Say where your graphs are**, in `pyproject.toml`:

```toml
[tool.graphlock]
migrations = "app.migrations:MIGRATIONS"   # optional, see Migrations

[tool.graphlock.graphs]
support = "app.graph:graph"   # a compiled graph, a StateGraph, or a function returning either
```

**2. Lock the deployed shape.** Run this on the code that is in production, and commit
`graphlock.json`:

```sh
graphlock lock
```

**3. Check every pull request.** `check` needs no database, and exits 1 when a change would break
stored threads that no migration repairs:

```yaml
# .github/workflows/graphlock.yml
- run: pip install -e . "graphlock @ git+https://github.com/devjoinedthechat/graphlock"
- run: graphlock check --format github   # annotations on the pull request, then the text report
```

**4. Before deploying, scan the real threads.** `scan` reads the checkpointer and changes
nothing:

```sh
graphlock scan --postgres "$CHECKPOINT_DB"     # or --sqlite FILE, or --checkpointer app.db:saver
graphlock scan --postgres "$CHECKPOINT_DB" --where graph=refunds --progress
```

**5. Repair what the deploy would break**, and run the graph with the migrations:

```python
import graphlock
from app.migrations import MIGRATIONS

graph = graphlock.with_migrations(builder.compile(checkpointer=saver), MIGRATIONS)
```

**6. After the deploy, lock again**, so the next pull request is compared with what is live.

[examples/refunds](examples/refunds) runs the whole flow on a SQLite database, and
[tests/test_cli.py](tests/test_cli.py) runs it on every commit.

## How it works

| Command | Needs | Answers |
|---|---|---|
| `graphlock lock` | your code | What does the deployed graph look like? Writes `graphlock.json` |
| `graphlock check` | your code and the lockfile | Which changes in this pull request can break stored threads? |
| `graphlock check --reverse` | your code and the lockfile | What would a rollback break, once this code has run? |
| `graphlock scan` | your code and the checkpointer | Which stored threads will break, and how? |
| `with_migrations()` | your migrations | Repairs those threads as LangGraph reads them |

**The lockfile** records the shape of each graph: everything about it that a stored thread
depends on.
- Every node, with its `defer` flag, the channels that trigger it, its `interrupt()` calls in
  source order and a digest of its code.
- Every state, branch and fan-in channel, with its type and reducer.
- The state schema's fields, and whether each is required.
- The classes reachable from the state annotations, since checkpoints store those by import path.
- The static breakpoints, and every subgraph, recursively.

It is plain, sorted JSON, so a change to it reads like a change to any other lockfile in review.

**`check`** compares the new shape with the locked one:
- **It suggests renames.** A new node with the same code as a removed one is reported with the
  `rename_node(...)` that repairs it.
- **Repaired findings don't fail it.** One that a configured migration repairs is marked `✓`.
- **You can accept a break.** After draining the affected threads, run `graphlock lock` again.

**`scan`** loads the latest checkpoint of every thread. It runs LangGraph's own restore and
step-planning code against the new graph; no node code runs. For each thread, it compares what the
checkpoint was waiting for under the old layout with what the new graph would do. It reports:
- a pending step, Send or fan-in the new graph can't see;
- a trigger or barrier that no longer fits its channel class;
- a stored value that fails the new schema;
- an object whose class no longer imports. LangGraph restores these without an error, so `scan`
  finds them by reading the stored bytes. It never imports anything the bytes name;
  [SECURITY.md](SECURITY.md) has the details.

It plans the next step as the run loop does, not as `get_state()` does. The two differ: the loop
only considers nodes triggered by the checkpoint's `updated_channels`. A thread can therefore look
ready in `get_state()` and still not move.

Problems on a paused thread are breaking. On a finished thread they are warnings, because they
only matter if the thread is continued.

A store often holds several graphs' threads. With the lockfile, `scan` skips a thread that mentions
none of the graph's nodes, old or new, as another graph's, and says how many it skipped. To be
explicit, use `--where KEY=VALUE`, which matches checkpoint metadata including your run config's
`metadata`, or `--thread-prefix`. Both can be set per graph in `[tool.graphlock.scan.<graph>]`.
For very large stores, `--sample N` scans a reproducible random sample. `scan` is also available
as `graphlock.scan()` and, for async checkpointers, `await graphlock.ascan()`.

## Rules

`graphlock rules` prints each rule with what LangGraph does and how to fix it.

| Rule | | What happens to a stored thread |
|---|---|---|
| GL101 | node-removed | Paused at the node, or with pending work for it: resumes as if finished. Silent |
| GL102 | defer-changed | The trigger restores into the wrong channel class, and resuming raises |
| GL103 | join-changed | A half-full fan-in is renamed and its target never runs. Silent |
| GL104 | subgraph-changed | Paused inside a node that stopped or started being a subgraph: loses the subgraph's state |
| GL201 | required-field-added | No stored value, so validation fails at the next node |
| GL202 | field-type-changed | Pydantic state fails validation at the next node; other state passes the old type on |
| GL203 | field-removed | At an `interrupt_before` breakpoint, now or later, the thread pauses again forever. Silent |
| GL204 | reducer-changed | The stored value is kept, and merged with the new reducer from the next write |
| GL205 | channel-kind-changed | The stored value restores into a channel that expects another shape |
| GL301 | stored-class-missing | Stored objects restore as a plain dict or None. Silent |
| GL401 | interrupt-order-changed | Paused inside the node: stored answers go to the wrong `interrupt()` calls. Silent |
| GL402 | interrupting-node-changed | Paused inside the node: the new code runs from the top on resume |

GL102 is informational when `defer` is turned off for a node with one predecessor, because pending
triggers restore fine. GL203 is informational in a graph with no `interrupt_before` breakpoints;
`interrupt_after` is not affected.

## Migrations

A migration looks at one stored checkpoint and decides from the data alone whether it applies: a
renamed node's trigger is still under the old name, a required field has no value, a value doesn't
fit its new type. So every migration is idempotent. There are no version numbers to keep in sync,
and the same list can run on every read.

| Migration | Repairs |
|---|---|
| `rename_node(old, new)` | GL101 for a renamed node. Moves its triggers, fan-in slots and pending Sends, and re-keys its pending writes and interrupts to the new task ids |
| `rename_channel(old, new)` | GL103, and any other renamed channel |
| `rename_field(old, new)` | A renamed state field |
| `drop_field(field)` | GL203. Forgets a removed field's stored value and version |
| `set_default(field, value)` | GL201. Gives threads a value they never had |
| `convert_field(field, fn)` | GL202 and GL205. Converts only the stored values that fail the field's new type |
| `revive(old_path, NewClass)` | GL301. Rebuilds objects of a renamed or moved class |
| `defer_changed(node)` | GL102. Converts stored triggers to the node's new channel class |

Each one takes `graph="research"` to target a subgraph. Subclass `Migration` for anything else.

`with_migrations(graph, migrations)` wraps the graph's checkpointer. Stored checkpoints are never
rewritten in place:
- **Threads are repaired when read.** The repair reaches storage with the next checkpoint LangGraph
  writes for that thread.
- **Repairs are written through.** Checkpointers that store each channel separately (in-memory,
  Postgres) persist only what a step wrote. Without write-through, a repaired value the step didn't
  touch would be lost the next time the thread paused. [tests/test_saver.py](tests/test_saver.py)
  shows that loss on both.
- **Postgres keeps the first value it stored at a version.** A repair that renames or adds a channel
  reaches storage. A repair made in place (a revived object, a converted value, a reshaped trigger)
  is applied again on each read, until the thread next writes that field. `scan` counts those threads
  as still needing the migration.
- **Memory is bounded.** The wrapper remembers a thread's repairs from its read until its next
  write, for at most `max_tracked` threads (10,000 by default). LangGraph reads a thread right before
  writing it, so a resumed thread is always tracked. A dashboard polling `get_state()` on every
  thread costs nothing once those threads fall out.
- **You can see it work.** `graph.checkpointer.stats` counts the reads each migration repaired, and
  each repair is logged at DEBUG on the `graphlock` logger.
- **`scan` says when a migration can go.** It counts the stored threads each migration still
  changes, and tells you when none are left.

### Rolling back

A rollback is a deploy too, in the other direction. The checkpoints written before your deploy are
untouched, so the old code reads them as it always did. But a thread that moved on under the new
code is stored in the new layout. For example, it may be paused at `manager_review`, which the old
code doesn't have. `graphlock check --reverse` reports what a rollback to the locked version would
break:

```
$ graphlock check --reverse
Rollback check: what threads that ran on this code would hit if you rolled back to graphlock.json.
Migrations don't run backwards; a repair for a rollback has to ship in the code you roll back to.

refunds
  ✗ GL101 node-removed  manager_review
      Node 'manager_review' is gone. Threads paused at it, or with pending work for it, will resume
      as if finished and it will never run.
```

To make a rename safe to roll back, expand before you contract:
1. Deploy code that has both nodes but still routes to the old one.
2. Switch the routing to the new node. Rolling back to step 1 is safe, because step 1 has both nodes.
3. Remove the old node once `graphlock scan` shows no thread paused there.

## Evidence

graphlock rests on claims about what LangGraph does, so those claims are tests.

[tests/corpus.py](tests/corpus.py) holds the 25 redeploy scenarios in the table above. Each pauses
a thread under one graph, deploys another and resumes. [tests/test_corpus.py](tests/test_corpus.py)
checks four things for every scenario, against `InMemorySaver`, `SqliteSaver` and `PostgresSaver`:

| Test | Asserts |
|---|---|
| `test_what_langgraph_does_today` | What LangGraph does with no help. It fails if a LangGraph release fixes one of these, so the table can't go stale |
| `test_check_reports_the_change` | `check` reports the right rule as blocking, and nothing blocking for the seven safe changes |
| `test_scan_reports_the_paused_thread` | `scan` reports the right rule for that thread, and for no other |
| `test_migration_repairs_the_thread` | With the migration, the thread resumes correctly and `check` and `scan` mark the rule repaired. Once the thread moves on, nothing needs the migration any more |

Around the corpus:

| Test file | Holds graphlock to |
|---|---|
| [test_langgraph_contract.py](tests/test_langgraph_contract.py) | The task ids, interrupt ids and channel names that migrations recompute, against LangGraph's own |
| [test_saver.py](tests/test_saver.py) | Write-through: without it, in-memory and Postgres stores lose a repair; with it, they keep it. Memory stays bounded |
| [test_async.py](tests/test_async.py) | `ainvoke` through `with_migrations`, and `ascan`, on async SQLite and Postgres |
| [test_security.py](tests/test_security.py) | `scan` imports nothing a checkpoint names, and nothing a strict serializer blocks |
| [test_stores.py](tests/test_stores.py) | The one-query path to each thread's latest checkpoint agrees with the checkpointer's own `list()` |
| [test_filters.py](tests/test_filters.py) | In a store shared by several graphs, only the graph's own threads are counted |
| [test_cli.py](tests/test_cli.py) | The whole flow on [examples/refunds](examples/refunds), including rollback checks |

The suite passes on LangGraph 1.0.0, 1.1.0 and 1.2.11, and on Python 3.10 to 3.14. The CI workflow
runs it against LangGraph 1.0.0 and the latest release, and writes the evidence table to the job
summary.

Building the corpus corrected graphlock five times:
- **`get_state()` was the wrong oracle.** After `rename_node`, `get_state()` reported the thread as
  ready to run, but `invoke` did nothing. The run loop plans from the checkpoint's
  `updated_channels`, which still named the old channel. Migrations now rename it, and `scan`
  plans the step the way the loop does.
- **Repairs could be lost.** With `InMemorySaver`, a repaired fan-in barrier disappeared when the
  thread paused a second time, because per-channel checkpointers store only what a step wrote.
  [tests/test_saver.py](tests/test_saver.py) shows the loss without write-through, and the fix.
- **A change assumed safe wasn't.** Removing a state field looked harmless in a first probe, which
  never checked the resumed result. The corpus showed that a thread at an `interrupt_before`
  breakpoint never gets past it.
- **A rule was too broad.** GL203 first flagged `interrupt_after` breakpoints too. Testing it showed
  they are unaffected, so the rule now blocks only on `interrupt_before`, and a scenario for each
  pins the difference.
- **Postgres doesn't overwrite.** Running the corpus on Postgres showed that a repair made in place is
  never persisted, because Postgres keeps the first value stored at a version. The docs now say so,
  and the tests assert what each checkpointer does.

## Scale

`scan` finds each thread's latest checkpoint with one query on SQLite and Postgres. With other
checkpointers it uses their own `list()`, which loads every checkpoint of every thread. It then
loads one checkpoint per thread and analyses it in memory.

[`scripts/bench_scan.py`](scripts/bench_scan.py) builds a store in which every thread has 13
checkpoints and is paused at a breakpoint that the deploy renames, so every thread is reported. On
an Apple M4 Pro, with Postgres 16 in Docker on the same machine:

| Store | Threads | One query | Checkpointer's `list()` | `--sample 1000` | Peak memory |
|---|---|---|---|---|---|
| SQLite | 10,000 | 0.52s | 2.28s | 0.08s | 92 MB |
| SQLite | 100,000 (5 GB) | 10.95s | — | 0.26s | 199 MB |
| Postgres | 10,000 | 5.46s | 12.65s | 0.62s | 1.7 GB with `list()` |
| Postgres | 100,000 | 72.25s | — | 1.65s | 223 MB |

On Postgres most of the time is one round trip per thread. Memory stays flat with the one-query
path; `list()` fetches every checkpoint first.

## What it does not do

- **The Functional API.** `@entrypoint` graphs have no builder to read, so graphlock reports them
  as unsupported.
- **Every way of reaching `interrupt()`.** GL401 reads the node's source and follows calls to plain
  functions the node can see, whether module globals or closure variables, two levels deep. Calls
  through attributes (`self.ask()`, `approvals.ask()`) are not followed. Nodes whose source can't be
  read, such as a lambda in the middle of a multi-line call, are skipped rather than guessed at.
- **Renaming a subgraph node itself.** A subgraph's checkpoints are stored under a namespace that
  contains the node's name and a task id, so `rename_node` can't carry a thread paused inside one.
  `check` and `scan` report it; drain those threads before deploying. Changes *inside* a subgraph
  are repaired like any others, with `graph="research"`.
- **Breakpoints passed at call time.** `invoke(..., interrupt_before=[...])` isn't part of the
  graph, so `check` can't see it. `scan` reads stored checkpoints and is unaffected.
- **Checkpointers you can't wrap.** `with_migrations` runs in your process. A platform that owns
  the checkpointer can still be locked, checked and scanned, but not migrated this way.
- **Fast lookups for every store.** Checkpointers other than SQLite and Postgres are scanned
  through their own `list()`, which reads all history. Use `--sample` on large ones.
- **Stable LangGraph APIs.** Channel names, task ids and checkpoint layout are not public API.
  graphlock imports all of them in one module, [`src/graphlock/_lg.py`](src/graphlock/_lg.py), and
  the corpus pins the behaviour each one is used for. A LangGraph release that changes one fails
  the corpus, not your deploy.

## Development

```sh
uv sync
uv run pytest                            # 308 tests, about ten seconds with Postgres
uv run ruff check . && uv run mypy src   # strict
uv run python scripts/evidence.py        # the table above, against the installed LangGraph
uv run python scripts/bench_scan.py      # the Scale table (add --postgres URL for Postgres)
```

The layout:
- **`shape.py`**: the graph's shape and the lockfile format.
- **`check.py`**: the rules, as a diff of two shapes.
- **`scan.py`**: stored threads against a new graph.
- **`migrations.py`** and **`saver.py`**: repairs, and applying them on read.
- **`_lg.py`**: every LangGraph internal graphlock depends on.
- **`tests/corpus.py`**: the redeploy scenarios everything above is held to.

See [CONTRIBUTING.md](CONTRIBUTING.md). The one rule: **every rule and every migration ships with
a corpus scenario** that shows what LangGraph does without it.

## License

[Apache-2.0](LICENSE)

graphlock is an independent project. It is not produced or endorsed by LangChain, Inc.
