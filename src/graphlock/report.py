"""Render check and scan results as text, JSON or GitHub Actions annotations."""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from typing import Any

from graphlock.findings import RULES, Finding, Severity
from graphlock.scan import ScanReport, group_issues

_MAX_THREADS_SHOWN = 5


def _mark(level: Severity, handled: bool) -> str:
    if handled:
        return "✓"
    return {Severity.BREAKING: "✗", Severity.WARNING: "!", Severity.INFO: "·"}[level]


def _wrap(text: str, indent: str, width: int = 100) -> str:
    words, lines, line = text.split(), [], ""
    for word in words:
        if line and len(indent) + len(line) + 1 + len(word) > width:
            lines.append(indent + line)
            line = word
        else:
            line = f"{line} {word}" if line else word
    if line:
        lines.append(indent + line)
    return "\n".join(lines)


def check_text(
    results: dict[str, list[Finding]], notes: Sequence[str] = (), header: str | None = None
) -> str:
    out: list[str] = [_wrap(header, "") + "\n"] if header else []
    totals: Counter[str] = Counter()
    for graph, findings in results.items():
        out.append(f"{graph}")
        if not findings:
            out.append("  no changes that affect stored threads\n")
            continue
        for f in findings:
            handled = f.handled_by is not None
            out.append(f"  {_mark(f.level, handled)} {f.code} {f.rule.name}  {f.where}")
            out.append(_wrap(f.message, "      "))
            if handled:
                out.append(f"      repaired by {f.handled_by}")
            elif f.hint:
                out.append(_wrap("→ " + f.hint, "      "))
            totals["handled" if handled and f.level is Severity.BREAKING else str(f.level)] += 1
        out.append("")
    out.extend(notes)
    parts = [f"{totals[k]} {k}" for k in ("breaking", "handled", "warning", "info") if totals[k]]
    summary = ", ".join(parts) if parts else "nothing to report"
    out.append(summary + ".")
    if totals["breaking"]:
        out.append(
            "Stored threads can break. `graphlock scan` shows which ones; add a migration, drain them, or "
            "run `graphlock lock` to accept the change."
        )
    return "\n".join(out) + "\n"


def check_json(
    results: dict[str, list[Finding]], notes: Sequence[str] = (), header: str | None = None
) -> str:
    doc = {
        "mode": "rollback" if header else "deploy",
        "graphs": {g: [f.to_json() for f in fs] for g, fs in results.items()},
        "notes": list(notes),
        "blocking": sum(f.blocking for fs in results.values() for f in fs),
    }
    return json.dumps(doc, indent=2) + "\n"


def _escape(text: str) -> str:
    return text.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")


def check_github(results: dict[str, list[Finding]], lockfile: str) -> str:
    """GitHub Actions workflow commands: each finding becomes an annotation on the lockfile."""
    lines = []
    for graph, findings in results.items():
        for f in findings:
            if f.handled_by is not None:
                level = "notice"
            else:
                level = {Severity.BREAKING: "error", Severity.WARNING: "warning", Severity.INFO: "notice"}[
                    f.level
                ]
            title = f"{f.code} {f.rule.name}: {graph}/{f.where}"
            body = f.message + (f" Repaired by {f.handled_by}." if f.handled_by else f" {f.hint or ''}")
            lines.append(f"::{level} file={lockfile},title={_escape(title)}::{_escape(body.strip())}")
    return "\n".join(lines) + ("\n" if lines else "")


def scan_text(name: str, report: ScanReport) -> str:
    out = [
        f"{name}: {report.threads} thread{'s' if report.threads != 1 else ''}, "
        f"{report.paused} paused mid-run",
    ]
    if not report.issues:
        out.append("  every stored thread resumes correctly under this graph")
    for (code, subject, level), issues in group_issues(report.issues).items():
        first = issues[0]
        threads = sorted({i.thread_id for i in issues})
        handled = all(i.handled_by for i in issues)
        count = f"{len(threads)} thread{'s' if len(threads) != 1 else ''}"
        out.append(f"  {_mark(level, handled)} {code} {RULES[code].name}  {subject} — {count}")
        out.append(_wrap(first.message, "      "))
        shown = ", ".join(threads[:_MAX_THREADS_SHOWN])
        more = f" (+{len(threads) - _MAX_THREADS_SHOWN} more)" if len(threads) > _MAX_THREADS_SHOWN else ""
        out.append(f"      threads: {shown}{more}")
        if handled:
            repairers = sorted({m for i in issues for m in i.handled_by})
            out.append(_wrap("repaired by " + ", ".join(repairers), "      "))
    if report.migrations_needed:
        out.append("  migrations:")
        for migration, counts in report.migrations_needed.items():
            paused, finished = counts.get("paused", 0), counts.get("finished", 0)
            if paused or finished:
                state = f"still changes {paused} paused and {finished} finished thread(s)"
            else:
                state = "no stored thread needs it any more; safe to delete"
            out.append(f"    {migration}: {state}")
    blocking = len({i.thread_id for i in report.blocking})
    if blocking:
        out.append(f"  {blocking} thread{'s' if blocking != 1 else ''} will break if you deploy this graph.")
    return "\n".join(out) + "\n"


def scan_json(results: dict[str, ScanReport]) -> str:
    doc: dict[str, Any] = {name: r.to_json() for name, r in results.items()}
    return json.dumps(doc, indent=2) + "\n"


def rules_text() -> str:
    out = []
    for rule in RULES.values():
        out.append(f"{rule.code} {rule.name} ({rule.severity})")
        out.append(_wrap(rule.langgraph_does, "    "))
        out.append(_wrap("Fix: " + rule.fix, "    "))
        out.append("")
    return "\n".join(out)
