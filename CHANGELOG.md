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
- Tested against LangGraph 1.0.0, 1.1.0 and 1.2.11, and Python 3.10 to 3.14.
