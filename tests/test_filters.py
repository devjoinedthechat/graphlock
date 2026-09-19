"""A store shared by several graphs: scan counts only the graph's own threads."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from corpus import ask_amount_then_approver, asks, linear

import graphlock as gl


def shared_store(saver: Any) -> tuple[Any, Any]:
    """Three refund threads (paused at review) and two threads of an unrelated graph, in one store."""
    refunds = linear("review")(saver)
    other = asks(ask_amount_then_approver)(saver)
    for n in (1, 2, 3):
        refunds.invoke(
            {"log": []}, {"configurable": {"thread_id": f"refund-{n}"}, "metadata": {"graph": "refunds"}}
        )
    for n in (1, 2):
        other.invoke(
            {"log": []}, {"configurable": {"thread_id": f"survey-{n}"}, "metadata": {"graph": "survey"}}
        )
    return refunds, other


def test_with_a_lockfile_other_graphs_threads_are_skipped(make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    refunds, _ = shared_store(saver)
    lock = gl.extract_shape(refunds)
    renamed = linear("manager_review")(saver)
    report = gl.scan(renamed, saver, lock=lock)
    assert report.foreign == 2
    assert report.threads == 3
    assert {i.thread_id for i in report.blocking} == {"refund-1", "refund-2", "refund-3"}


def test_without_a_lockfile_they_are_flagged_not_skipped(make_saver: Callable[[], Any]) -> None:
    """A complete rename looks like another graph's thread, so without the lockfile nothing is skipped."""
    saver = make_saver()
    shared_store(saver)
    report = gl.scan(linear("review")(saver), saver)
    assert report.foreign == 0
    assert report.unrelated == 2


def test_metadata_and_prefix_filters(make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    shared_store(saver)
    renamed = linear("manager_review")(saver)
    by_metadata = gl.scan(renamed, saver, where={"graph": "refunds"})
    assert by_metadata.threads == 3 and by_metadata.unrelated == 0
    by_prefix = gl.scan(renamed, saver, thread_prefix="refund-")
    assert by_prefix.threads == 3 and by_prefix.unrelated == 0
    assert {i.thread_id for i in by_prefix.blocking} == {"refund-1", "refund-2", "refund-3"}


def test_sample_is_reproducible(make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    shared_store(saver)
    renamed = linear("manager_review")(saver)
    first = gl.scan(renamed, saver, thread_prefix="refund-", sample=2, seed=7)
    again = gl.scan(renamed, saver, thread_prefix="refund-", sample=2, seed=7)
    assert first.threads == 2 and first.stored_threads == 3
    assert {i.thread_id for i in first.issues} == {i.thread_id for i in again.issues}


def test_progress_is_reported(make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    shared_store(saver)
    calls: list[tuple[int, int]] = []
    gl.scan(linear("review")(saver), saver, on_progress=lambda done, total: calls.append((done, total)))
    assert calls[-1] == (5, 5)
