"""The one-query path to each thread's latest checkpoint agrees with the checkpointer's own list()."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from corpus import linear, with_subgraph
from langgraph.types import Command

from graphlock import _stores
from graphlock.scan import ThreadFilter, _latest_checkpoints

THREADS = ["refund-1", "refund-2", "refund_3", "refund%4", "other-1"]


def populate(saver: Any) -> None:
    """Threads with several checkpoints each, one paused inside a subgraph, and awkward ids."""
    graph = linear("review")(saver)
    for thread in THREADS:
        graph.invoke({"log": []}, {"configurable": {"thread_id": thread}})
    graph.invoke(None, {"configurable": {"thread_id": "refund-1"}})  # finishes: more history
    sub = with_subgraph("research")(saver)
    sub.invoke({"log": []}, {"configurable": {"thread_id": "sub-1"}})
    sub.invoke(Command(resume="later"), {"configurable": {"thread_id": "sub-2"}})


def generic(saver: Any, filt: ThreadFilter, monkeypatch: pytest.MonkeyPatch) -> Any:
    with monkeypatch.context() as m:
        m.setattr(_stores, "latest_checkpoints", lambda saver, filt: None)
        return _latest_checkpoints(saver, filt)


FILTERS = [
    ThreadFilter(),
    ThreadFilter(prefix="refund"),
    ThreadFilter(prefix="refund_"),  # "_" is a LIKE wildcard: must match refund_3 only
    ThreadFilter(prefix="refund%"),  # "%" too: must match refund%4 only
    ThreadFilter(thread_ids=("refund-2", "sub-1", "missing")),
]


@pytest.mark.parametrize("filt", FILTERS, ids=lambda f: repr((f.thread_ids, f.prefix)))
def test_fast_path_matches_list(
    filt: ThreadFilter, make_saver: Callable[[], Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    saver = make_saver()
    populate(saver)
    fast = _stores.latest_checkpoints(saver, filt)
    if type(saver).__name__ == "InMemorySaver":
        assert fast is None  # no shortcut: list() is already in memory
        return
    assert fast is not None
    assert fast == generic(saver, filt, monkeypatch)


def test_wildcards_in_prefixes_are_literal(make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    populate(saver)
    for prefix, expected in [("refund_", {"refund_3"}), ("refund%", {"refund%4"})]:
        latest = _latest_checkpoints(saver, ThreadFilter(prefix=prefix))
        assert {t for t, _ in latest} == expected


def test_subgraph_namespaces_are_kept(make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    populate(saver)
    latest = _latest_checkpoints(saver, ThreadFilter(thread_ids=("sub-1",)))
    namespaces = sorted(ns for _, ns in latest)
    assert namespaces[0] == ""
    assert len(namespaces) == 2 and namespaces[1].startswith("research:")


def test_metadata_filters_use_the_checkpointer(make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    assert _stores.latest_checkpoints(saver, ThreadFilter(where={"graph": "refunds"})) is None
