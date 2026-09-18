"""Print the README's evidence table by running every corpus scenario against LangGraph as installed.

    uv run python scripts/evidence.py

Nothing in the table is typed by hand: each row is what happened just now.
"""

from __future__ import annotations

import sqlite3
import sys
from importlib.metadata import version
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))

from corpus import CRASH, OK, SCENARIOS, SILENT

import graphlock as gl

LABEL = {OK: "resumes correctly", SILENT: "**wrong result, no error**", CRASH: "crashes on resume"}


def saver() -> SqliteSaver:
    return SqliteSaver(sqlite3.connect(":memory:", check_same_thread=False))


def run(sc: object, graph: object) -> str:
    try:
        result = sc.resume(graph)  # type: ignore[attr-defined]
    except Exception:
        return CRASH
    return OK if sc.expected(result) else SILENT  # type: ignore[attr-defined]


def source_link(source: str) -> str:
    if not source:
        return ""
    repo, _, number = source.partition("#")
    return f" ([{source}](https://github.com/langchain-ai/{repo}/issues/{number}))"


def rows() -> list[str]:
    """The table's rows, one per corpus scenario, as they come out of running it now."""
    out = []
    for sc in SCENARIOS:
        s = saver()
        g1 = sc.v1(s)
        sc.start(g1)
        lock = gl.extract_shape(g1)
        g2 = sc.v2(s)
        findings = gl.check(lock, gl.extract_shape(g2))
        report = gl.scan(g2, s, lock=lock)
        today = run(sc, g2)

        blocking = sorted({f.code for f in findings if f.blocking})
        info = sorted({f.code for f in findings if not f.blocking})
        check_cell = ", ".join(blocking) or (f"({', '.join(info)})" if info else "—")
        scan_cell = ", ".join(sorted({i.code for i in report.blocking})) or "—"
        repair = "—"
        if sc.migrations is not None:
            s2 = saver()
            sc.start(sc.v1(s2))
            g2m = sc.v2(s2)  # before the migrations: they may name classes only v2 defines
            migrations = list(sc.migrations() if callable(sc.migrations) else sc.migrations)
            g2m = gl.with_migrations(g2m, migrations)
            fixed = run(sc, g2m) == OK
            names = ", ".join(f"`{m.describe().split('(')[0]}`" for m in migrations)
            repair = f"{names} {'✓' if fixed else '✗'}"
        out.append(
            f"| {sc.title}{source_link(sc.source)} | {LABEL[today]} | {check_cell} | {scan_cell} | {repair} |"
        )
    return out


def main() -> None:
    print(f"LangGraph {version('langgraph')}, `SqliteSaver`:\n")
    print("| Change deployed while a thread is paused | LangGraph today | `check` | `scan` | Repair |")
    print("|---|---|---|---|---|")
    print("\n".join(rows()))


if __name__ == "__main__":
    main()
