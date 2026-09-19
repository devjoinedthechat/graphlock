# Changelog

## Unreleased

First version.

- `graphlock lock`, `check`, `scan`, `rules` and `show`, with text, JSON and GitHub
  Actions output.
- Twelve rules, GL101 to GL402, each backed by a redeploy scenario in `tests/corpus.py`.
- Eight migrations and `with_migrations()`: repairs are applied on read and written through with
  the thread's next checkpoint.
- `graphlock check --reverse`: what a rollback would break for threads that ran on the new code.
- GL401 follows `interrupt()` calls into helper functions, two levels deep.
- `with_migrations` bounds its memory (`max_tracked`), counts repairs in `stats` and logs them.
- `scan` never imports a module because stored data names it (tests/test_security.py).
- Postgres and async: the corpus runs on `PostgresSaver` too; `with_migrations` works through
  `ainvoke`; `ascan` scans async checkpointers.
- `scan` skips other graphs' threads when it has the lockfile, and takes `--where`, `--thread-prefix`,
  `--sample` and `--progress`. On SQLite and Postgres it finds each thread's latest checkpoint with
  one query: 100,000 threads in 11s on SQLite and 72s on Postgres.
- Corpus scenarios for changes inside a subgraph.
- Property tests (Hypothesis): random graphs, refactors and pause points, checked against what
  LangGraph does. They found four silent failure classes and three kinds of false alarm, all now
  fixed and pinned in the corpus. Turning `defer` off is now breaking (GL102).
- `redirect_node` and `drop_node` migrations for removed nodes.
- Validated against the history of seven public LangGraph apps (`scripts/history.py`). That review
  refined three rules: GL401 now tells a moved `interrupt()` call (breaking) from reworded prompts
  (info) and calls removed from the end (warning); a type change together with a reducer change is
  breaking (GL204), and `scan` tries the new reducer on each stored value; a type that only widens is
  informational (GL202).
- Tested against LangGraph 1.0.0, 1.1.0 and 1.2.11, and Python 3.10 to 3.14.
