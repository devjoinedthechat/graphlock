"""Every redeploy scenario in corpus.py, held to account four ways."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from corpus import CRASH, OK, SCENARIOS, SILENT, Scenario

import graphlock as gl


def outcome(sc: Scenario, graph: Any) -> str:
    try:
        result = sc.resume(graph)
    except Exception:
        return CRASH
    return OK if sc.expected(result) else SILENT


def migrations_of(sc: Scenario) -> list[gl.Migration]:
    m = sc.migrations
    assert m is not None
    return list(m() if callable(m) else m)


def paused(sc: Scenario, saver: Any) -> tuple[Any, gl.GraphShape]:
    """Run v1 until the thread pauses; return v2 on the same checkpointer, and v1's shape."""
    g1 = sc.v1(saver)
    sc.start(g1)
    lock = gl.extract_shape(g1)
    return sc.v2(saver), lock


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_what_langgraph_does_today(sc: Scenario, make_saver: Callable[[], Any]) -> None:
    """Pins LangGraph's own behaviour. If a release fixes one of these, this fails: update the corpus."""
    g2, _ = paused(sc, make_saver())
    assert outcome(sc, g2) == sc.today


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_check_reports_the_change(sc: Scenario) -> None:
    before = gl.extract_shape(sc.v1(None))
    after = gl.extract_shape(sc.v2(None))
    findings = gl.check(before, after)
    blocking = {f.code for f in findings if f.blocking}
    if sc.rule:
        assert sc.rule in blocking, findings
    else:
        assert not blocking, findings
    if sc.info:
        assert sc.info in {f.code for f in findings if not f.blocking}, findings


@pytest.mark.parametrize("sc", SCENARIOS, ids=lambda s: s.id)
def test_scan_reports_the_paused_thread(sc: Scenario, make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    g2, lock = paused(sc, saver)
    report = gl.scan(g2, saver, lock=lock)
    blocking = {i.code for i in report.blocking}
    if sc.scan:
        assert sc.scan in blocking, report.issues
        assert {i.thread_id for i in report.blocking} == {"t1"}
    else:
        assert not blocking, report.issues
    assert report.threads == 1


@pytest.mark.parametrize("sc", [s for s in SCENARIOS if s.migrations is not None], ids=lambda s: s.id)
def test_migration_repairs_the_thread(sc: Scenario, make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    g2, lock = paused(sc, saver)
    migrations = migrations_of(sc)

    # check and scan both see the change as repaired
    findings = gl.check(lock, gl.extract_shape(g2), migrations)
    assert sc.rule in {f.code for f in findings if f.handled_by}, findings
    assert not [f for f in findings if f.blocking], findings
    report = gl.scan(g2, saver, migrations=migrations, lock=lock)
    assert not report.blocking, report.issues
    assert sc.scan in {i.code for i in report.issues if i.handled_by}, report.issues
    assert all(n == {"paused": 1, "finished": 0} for n in report.migrations_needed.values()), report

    # and the thread really resumes correctly
    assert outcome(sc, gl.with_migrations(g2, migrations)) == OK

    # once it has moved on, no paused thread needs the migrations any more
    after = gl.scan(g2, saver, migrations=migrations, lock=lock)
    assert all(n["paused"] == 0 for n in after.migrations_needed.values()), after.migrations_needed
    if type(saver).__name__ != "PostgresSaver":
        assert all(n["finished"] == 0 for n in after.migrations_needed.values()), after.migrations_needed
    # Postgres never overwrites a stored value at the same version, so a repair made in place (a
    # revived object, a converted value) is re-applied on each read until the thread writes that field.


def test_every_corpus_rule_is_catalogued() -> None:
    used = {c for s in SCENARIOS for c in (s.rule, s.scan, s.info) if c}
    assert used <= set(gl.RULES)


def test_silent_failures_are_the_majority_of_breaks() -> None:
    """The README's headline: most breaking changes fail silently. Keep it true or change the README."""
    breaks = [s for s in SCENARIOS if s.today != OK]
    silent = [s for s in breaks if s.today == SILENT]
    assert len(silent) > len(breaks) / 2
