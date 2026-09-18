"""The command line, end to end, on examples/refunds: the flow the README walks through."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from graphlock.cli import EXIT_BREAKING, EXIT_ERROR, EXIT_OK, main

EXAMPLE = Path(__file__).resolve().parents[1] / "examples" / "refunds"


def flat(text: str) -> str:
    """Text output wraps at 100 columns; compare it with the wrapping taken out."""
    return " ".join(text.split())


@pytest.fixture
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The refund example with five refunds seeded under v1 and v1's shape locked."""
    for f in EXAMPLE.glob("*.py"):
        shutil.copy(f, tmp_path)
    shutil.copy(EXAMPLE / "pyproject.toml", tmp_path)
    monkeypatch.chdir(tmp_path)
    subprocess.run([sys.executable, "seed.py"], check=True, capture_output=True)
    assert main(["lock", "--graph", "refunds=refunds_v1:graph"]) == EXIT_OK
    return tmp_path


def test_check_fails_on_unrepaired_breaking_changes(
    project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["check", "--no-migrations"]) == EXIT_BREAKING
    out = flat(capsys.readouterr().out)
    assert "✗ GL101 node-removed wait_for_manager_approval" in out
    assert "add rename_node('wait_for_manager_approval', 'manager_review')" in out
    assert "✗ GL201 required-field-added currency" in out
    assert "2 breaking." in out


def test_check_passes_when_migrations_repair_them(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check"]) == EXIT_OK
    out = flat(capsys.readouterr().out)
    assert "repaired by rename_node('wait_for_manager_approval', 'manager_review')" in out
    assert "repaired by set_default('currency', 'USD')" in out
    assert "2 handled." in out


def test_check_json_and_github_formats(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["check", "--no-migrations", "--format", "json"]) == EXIT_BREAKING
    doc = json.loads(capsys.readouterr().out)
    assert doc["blocking"] == 2
    assert {f["code"] for f in doc["graphs"]["refunds"]} == {"GL101", "GL201"}

    assert main(["check", "--no-migrations", "--format", "github"]) == EXIT_BREAKING
    lines = capsys.readouterr().out.splitlines()
    errors = [line for line in lines if line.startswith("::error file=graphlock.json,title=")]
    assert len(errors) == 2


def test_check_strict_fails_on_warnings(project: Path, tmp_path: Path) -> None:
    Path("refunds_v3.py").write_text(
        Path("refunds_v1.py")
        .read_text()
        .replace("log: Annotated[list[str], operator.add] = []", "log: list[str] = []")
    )
    assert main(["check", "--graph", "refunds=refunds_v3:graph"]) == EXIT_OK  # a reducer change warns
    assert main(["check", "--graph", "refunds=refunds_v3:graph", "--strict"]) == EXIT_BREAKING


def test_scan_names_the_threads_that_would_break(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", "--sqlite", "refunds.sqlite", "--no-migrations"]) == EXIT_BREAKING
    out = flat(capsys.readouterr().out)
    assert "refunds: 5 threads, 3 paused mid-run" in out
    assert "threads: refund-1, refund-3, refund-4" in out
    assert "3 threads will break if you deploy this graph." in out


def test_scan_with_migrations_then_resume(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", "--sqlite", "refunds.sqlite"]) == EXIT_OK
    assert "still changes 3 paused and 2 finished thread(s)" in flat(capsys.readouterr().out)

    resumed = subprocess.run([sys.executable, "resume.py"], check=True, capture_output=True, text=True)
    assert resumed.stdout.strip().splitlines() == [
        "drafted refund of 40 for A-101",
        "manager said yes",
        "issued 40 USD",
    ]

    assert main(["scan", "--sqlite", "refunds.sqlite", "--format", "json"]) == EXIT_OK
    doc = json.loads(capsys.readouterr().out)["refunds"]
    assert doc["paused"] == 2
    # refund-1 has moved on and its new checkpoint stores the currency: it no longer needs the migration
    assert doc["migrations_needed"]["set_default('currency', 'USD')"] == {"paused": 2, "finished": 2}


def test_scan_one_thread(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["scan", "--sqlite", "refunds.sqlite", "--no-migrations", "--thread", "refund-2"]) == EXIT_OK
    out = flat(capsys.readouterr().out)
    assert "1 thread, 0 paused mid-run" in out
    assert "The thread has finished, so this only matters if it is continued." in out


def test_rules_and_show(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rules"]) == EXIT_OK
    assert "GL401 interrupt-order-changed (breaking)" in capsys.readouterr().out
    assert main(["show"]) == EXIT_OK
    shape = json.loads(capsys.readouterr().out)["refunds"]
    assert shape["nodes"]["manager_review"]["interrupts"] == [
        "interrupt(f'Approve a refund of {state.amount} for {state.order_id}?')"
    ]


def test_errors_say_what_to_do(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.chdir(tmp_path)
    assert main(["check"]) == EXIT_ERROR
    assert "No graphs to check" in capsys.readouterr().err
    assert main(["check", "--graph", "g=nowhere:graph"]) == EXIT_ERROR
    assert "graphlock.json not found" in capsys.readouterr().err
    assert main(["lock", "--graph", "g=nowhere:graph"]) == EXIT_ERROR
    assert "Importing nowhere failed" in capsys.readouterr().err
    assert main(["scan", "--graph", "g=nowhere:graph"]) == EXIT_ERROR
    assert "Pass exactly one of" in capsys.readouterr().err


def test_without_migrations_the_refund_is_never_issued(project: Path) -> None:
    """The example README's claim: the same approval returns cleanly and stops after the draft."""
    code = (
        "from langgraph.checkpoint.sqlite import SqliteSaver\n"
        "from langgraph.types import Command\n"
        "import refunds_v2\n"
        "with SqliteSaver.from_conn_string('refunds.sqlite') as saver:\n"
        "    graph = refunds_v2.builder.compile(checkpointer=saver)\n"
        "    result = graph.invoke(Command(resume='yes'), {'configurable': {'thread_id': 'refund-1'}})\n"
        "    print(result['log'])\n"
    )
    out = subprocess.run([sys.executable, "-c", code], check=True, capture_output=True, text=True).stdout
    assert out.strip() == "['drafted refund of 40 for A-101']"
