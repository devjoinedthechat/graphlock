"""Replay a LangGraph app's git history through `graphlock check`.

    uv run python scripts/history.py https://github.com/langchain-ai/react-agent --workdir /tmp/gl-history

For every commit that touches Python files (oldest first), it reads langgraph.json, extracts the
shape of each graph in the app's own environment, and runs `check` between consecutive shapes of
the same graph. The result is every change in the app's history that `check` would have flagged,
written to <workdir>/<repo>.json for review. The environment is the app's current dependencies, so
commits old enough not to import with them are counted and skipped.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from graphlock import check  # noqa: E402


def run(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=True).stdout


def prepare(url: str, work: Path) -> tuple[Path, Path]:
    name = url.rstrip("/").rsplit("/", 1)[-1]
    clone = work / name
    if not clone.exists():
        run("git", "clone", "-q", url, str(clone))
    run("git", "checkout", "-q", "-f", run("git", "rev-parse", "origin/HEAD", cwd=clone).strip(), cwd=clone)
    venv = work / f"{name}-venv"
    if not venv.exists():
        run("uv", "venv", "-q", "-p", "3.12", str(venv))
        run("uv", "pip", "install", "-q", "-p", str(venv), "-e", str(clone), "-e", str(ROOT))
    return clone, venv / "bin" / "python"


def commits(clone: Path, limit: int | None) -> list[tuple[str, str, str]]:
    log = run(
        "git",
        "log",
        "--reverse",
        "--format=%H%x09%ad%x09%s",
        "--date=short",
        "HEAD",
        "--",
        "*.py",
        "langgraph.json",
        cwd=clone,
    )
    rows = [tuple(line.split("\t", 2)) for line in log.splitlines() if line]
    return rows[-limit:] if limit else rows  # type: ignore[return-value]


def shapes_at(clone: Path, python: Path, sha: str) -> dict[str, Any]:
    run("git", "checkout", "-q", "-f", sha, cwd=clone)
    if not (clone / "langgraph.json").exists():
        return {}
    proc = subprocess.run(
        [str(python), str(ROOT / "scripts" / "_extract_shapes.py"), str(clone)],
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    if proc.returncode != 0:
        return {
            "*": {"error": proc.stderr.strip().splitlines()[-1][:200] if proc.stderr.strip() else "failed"}
        }
    return json.loads(proc.stdout.strip().splitlines()[-1])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("url")
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--limit", type=int, help="only the most recent N commits")
    args = parser.parse_args()
    args.workdir.mkdir(parents=True, exist_ok=True)
    clone, python = prepare(args.url, args.workdir)
    history = commits(clone, args.limit)

    last: dict[str, tuple[str, Any]] = {}
    report: dict[str, Any] = {"repo": args.url, "commits": len(history), "importable": 0, "changes": []}
    for sha, date, subject in history:
        shapes = shapes_at(clone, python, sha)
        ok = {name: shape for name, shape in shapes.items() if "error" not in shape}
        report["importable"] += bool(ok) and len(ok) == len(shapes)
        for name, shape in ok.items():
            previous = last.get(name)
            last[name] = (sha, shape)
            if previous is None or previous[1] == shape:
                continue
            findings = check(previous[1], shape)
            report["changes"].append(
                {
                    "graph": name,
                    "from": previous[0][:10],
                    "to": sha[:10],
                    "date": date,
                    "subject": subject,
                    "findings": [f.to_json() for f in findings],
                }
            )
        print(f"{sha[:10]} {date} {len(ok)}/{len(shapes)} graphs  {subject[:70]}", file=sys.stderr)
    run("git", "checkout", "-q", "-f", history[-1][0] if history else "HEAD", cwd=clone)
    out = args.workdir / f"{clone.name}.json"
    out.write_text(json.dumps(report, indent=2))
    blocking = [c for c in report["changes"] if any(f["blocking"] for f in c["findings"])]
    print(
        f"{clone.name}: {report['commits']} commits, {report['importable']} importable, "
        f"{len(report['changes'])} shape changes, {len(blocking)} with blocking findings -> {out}"
    )


if __name__ == "__main__":
    main()
