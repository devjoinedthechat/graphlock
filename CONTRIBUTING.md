# Contributing

```sh
git clone https://github.com/devjoinedthechat/graphlock && cd graphlock
uv sync

uv run pytest                                  # the whole suite, a few seconds
uv run ruff format src tests scripts examples
uv run ruff check src tests scripts examples
uv run mypy src                                # strict
uv run python scripts/evidence.py              # the README's table, against the installed LangGraph
```

Start with [tests/corpus.py](tests/corpus.py). It is the specification: every scenario says what
LangGraph does to a paused thread when one kind of change is deployed, and everything else in the
repository is held to it.

## The one rule

**Every rule and every migration ships with a corpus scenario.** A scenario pauses a thread under
`v1`, deploys `v2` and resumes, and records four things. `tests/test_corpus.py` checks all four
against the in-memory and the SQLite checkpointer:

1. `today`: what LangGraph does with no help: `ok`, `silent` or `crash`. This is measured, never
   assumed. A first probe of "remove a state field" looked safe and wasn't.
2. `rule`: the rule `graphlock check` must report as blocking. `None` for a safe change, which
   must report nothing blocking.
3. `scan`: the issue `graphlock scan` must report for the paused thread.
4. `migrations`: what makes the thread resume correctly, or `None` if nothing can.

A rule without a scenario is a claim about LangGraph that nobody has checked. A migration without
one may repair a thread in `get_state()` and still leave it stuck in `invoke()`; the corpus has
already caught that once.

If you find a change that breaks paused threads and graphlock misses it, the most useful thing
you can send is a failing scenario. Open an issue with the two graphs if writing one is more than
you have time for.

## LangGraph internals

graphlock depends on things LangGraph doesn't promise: channel names, task ids, checkpoint layout.
All of it is imported in [src/graphlock/_lg.py](src/graphlock/_lg.py) and nowhere else. Keep it
that way. When a LangGraph release changes one of them, the fix belongs in `_lg.py`. A version
check there is fine, and `channels_from_checkpoint` has one.

CI runs the suite against LangGraph 1.0.0 and the latest release. If you change `_lg.py`, run
both locally too:

```sh
uv run --isolated --with "langgraph==1.0.0" --with langgraph-checkpoint-sqlite --with pytest --with-editable . pytest -q
```

## Style

- **Messages say what happens to a thread, not what changed in the graph.** "Threads paused at it
  will resume as if finished" is the finding; "node removed" is only its name. The person reading
  it is deciding whether to deploy.
- **Say "silent" only when it is.** If LangGraph raises, the rule says so. Crashing is bad, but it
  is not the same failure as a refund quietly never being issued.
- **Comments say why.** Most lines here protect against one specific thing LangGraph does. Name
  it.

## Releasing

Bump `version` in `pyproject.toml`, add the release to `CHANGELOG.md`, and push a tag `vX.Y.Z` that
matches it. [.github/workflows/release.yml](.github/workflows/release.yml) builds, checks and
publishes to PyPI through trusted publishing, so no token is stored. The repository and its `pypi`
environment have to be registered as a trusted publisher on PyPI first.

## Things that would help

- **Carrying threads across a subgraph rename.** The subgraph's checkpoints live under a namespace
  built from the node name and a task id.
- **`interrupt()` calls reached through attributes** (`self.ask()`), for GL401. Plain helper
  functions are already followed.
- **More checkpointers for the one-query path** in `src/graphlock/_stores.py` (Redis, MongoDB).
- **The Functional API** (`@entrypoint`).

By participating you agree to the [Code of Conduct](CODE_OF_CONDUCT.md).
