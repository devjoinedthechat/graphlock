# Changelog

## Unreleased

First version.

- `graphlock lock`, `check`, `scan`, `rules` and `show`, with text, JSON and GitHub
  Actions output.
- Twelve rules, GL101 to GL402, each backed by a redeploy scenario in `tests/corpus.py`.
- Eight migrations and `with_migrations()`: repairs are applied on read and written through with
  the thread's next checkpoint.
- Tested against LangGraph 1.0.0, 1.1.0 and 1.2.11, and Python 3.10 to 3.14.
