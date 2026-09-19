# Changelog

All notable changes to graphlock are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - Unreleased

The first release.

### Commands

- `graphlock lock` records the shape of each deployed graph in `graphlock.json`.
- `graphlock check` compares a pull request's graphs with the lockfile, and exits 1 on changes that
  would break stored threads.
- `graphlock check --reverse` reports what rolling back would break for threads that ran on the new
  code.
- `graphlock scan` reports which stored threads a deploy would break, on SQLite, Postgres or any
  checkpointer.
- `graphlock rules` lists the rules, and `graphlock show` prints a graph's shape.
- Text, JSON and GitHub Actions output. `--lockfile` selects a per-environment lockfile.

### Rules

- Twelve rules, GL101 to GL402, covering removed or renamed nodes, `defer` changes, fan-ins, state
  fields, reducers, stored classes and `interrupt()` calls. Each one is backed by redeploy scenarios
  in `tests/corpus.py`.

### Migrations

- `rename_node`, `redirect_node`, `drop_node`, `rename_channel`, `rename_field`, `drop_field`,
  `set_default`, `convert_field`, `revive` and `defer_changed`.
- `with_migrations()` repairs threads as LangGraph reads them and writes repairs through with the
  next checkpoint. It bounds its memory, counts repairs in `stats`, and logs them at DEBUG.

### Scanning

- `scan` and `ascan` for sync and async checkpointers.
- It skips other graphs' threads when it has the lockfile, and takes `--where`, `--thread-prefix`,
  `--sample` and `--progress`.
- On SQLite and Postgres it finds each thread's latest checkpoint with one query. 100,000 threads
  scan in 11s on SQLite and 72s on Postgres.
- It never imports a module because stored data names it.

### Integrations

- A GitHub Action, a pre-commit hook, and a release workflow that publishes through PyPI trusted
  publishing.

### Verification

- 30 redeploy scenarios, checked against `InMemorySaver`, `SqliteSaver` and `PostgresSaver`.
- Property tests over random graphs, refactors and pause points.
- A replay of seven public LangGraph apps' histories.
- Tested on LangGraph 1.0.0, 1.1.0 and 1.2.11, and Python 3.10 to 3.14.

[0.1.0]: https://github.com/devjoinedthechat/graphlock/releases/tag/v0.1.0
