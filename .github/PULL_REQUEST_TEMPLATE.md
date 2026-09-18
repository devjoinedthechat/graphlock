## What this changes

<!-- One or two sentences. -->

## Why

<!-- What a paused thread did before, and what it does now. -->

## Checklist

- [ ] `uv run pytest` passes
- [ ] `uv run ruff check src tests scripts examples` and `uv run mypy src` pass
- [ ] A new or changed rule or migration has a scenario in `tests/corpus.py`, and its `today`
      field was measured rather than assumed
- [ ] Any new LangGraph internal is imported in `src/graphlock/_lg.py` and nowhere else
- [ ] If the evidence table changed, the README shows the new output of `scripts/evidence.py`
