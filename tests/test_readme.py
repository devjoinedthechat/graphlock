"""The README's evidence table is what the corpus does now, not what it did when the README was written."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_table_matches_the_corpus() -> None:
    spec = importlib.util.spec_from_file_location("evidence", ROOT / "scripts" / "evidence.py")
    assert spec is not None and spec.loader is not None
    evidence = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(evidence)

    readme = (ROOT / "README.md").read_text().splitlines()
    start = (
        readme.index(
            "| Change deployed while a thread is paused | LangGraph today | `check` | `scan` | Repair |"
        )
        + 2
    )
    table = []
    for line in readme[start:]:
        if not line.startswith("|"):
            break
        table.append(line)
    assert table == evidence.rows(), "README table is stale: paste the output of scripts/evidence.py"
