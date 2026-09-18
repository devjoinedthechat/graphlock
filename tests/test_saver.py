"""MigratingSaver: repairs reach storage, and everything else passes straight through."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

import pytest
from corpus import CFG, S, log_of, step
from langgraph.graph import START, StateGraph

import graphlock as gl
from graphlock.saver import MigratingSaver


def two_breakpoints(sources: tuple[str, ...]) -> Callable[[Any], Any]:
    """x finishes early; y -> y2 -> y3 pauses before y2 and again before y3, then 'join' fires."""

    def build(saver: Any) -> Any:
        b = StateGraph(S)
        for name in ("split", "x", "y", "y2", "y3", "join"):
            b.add_node(name, step(name))
        b.add_edge(START, "split")
        b.add_edge("split", "x")
        b.add_edge("split", "y")
        b.add_edge("y", "y2")
        b.add_edge("y2", "y3")
        b.add_edge(list(sources), "join")
        return b.compile(checkpointer=saver, interrupt_before=["y2", "y3"])

    return build


MIGRATIONS = [gl.rename_channel("join:x+y3:join", "join:y3+x:join")]


def test_a_repair_survives_the_thread_pausing_again(make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    two_breakpoints(("x", "y3"))(saver).invoke({"log": []}, CFG)  # barrier holds {x}; paused before y2
    graph = gl.with_migrations(two_breakpoints(("y3", "x"))(saver), MIGRATIONS)
    graph.invoke(None, CFG)  # runs y2, pauses before y3: a new checkpoint is written
    result = graph.invoke(None, CFG)
    assert log_of(result)[-1] == "join"


def test_without_write_through_the_repair_is_lost(
    monkeypatch: pytest.MonkeyPatch, make_saver: Callable[[], Any]
) -> None:
    """Why write-through exists. SQLite stores whole checkpoints, so only per-channel savers lose it."""
    monkeypatch.setattr(MigratingSaver, "_write_through", lambda self, config, checkpoint, versions: versions)
    saver = make_saver()
    two_breakpoints(("x", "y3"))(saver).invoke({"log": []}, CFG)
    graph = gl.with_migrations(two_breakpoints(("y3", "x"))(saver), MIGRATIONS)
    graph.invoke(None, CFG)
    result = graph.invoke(None, CFG)
    per_channel_storage = type(saver).__name__ == "InMemorySaver"
    assert ("join" in log_of(result)) is not per_channel_storage


def test_writes_and_other_calls_pass_through(make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    graph = gl.with_migrations(two_breakpoints(("x", "y3"))(saver), MIGRATIONS)
    graph.invoke({"log": []}, CFG)
    assert saver.get_tuple(CFG) is not None  # written to the inner saver
    graph.checkpointer.delete_thread("t1")
    assert saver.get_tuple(CFG) is None


def test_with_migrations_needs_a_checkpointer() -> None:
    with pytest.raises(TypeError, match="checkpointer"):
        gl.with_migrations(two_breakpoints(("x", "y3"))(None), MIGRATIONS)


def test_wrapping_twice_replaces_the_migrations(make_saver: Callable[[], Any]) -> None:
    saver = make_saver()
    graph = gl.with_migrations(two_breakpoints(("x", "y3"))(saver), MIGRATIONS)
    graph = gl.with_migrations(graph, [])
    assert isinstance(graph.checkpointer, MigratingSaver)
    assert graph.checkpointer.inner is saver
    assert graph.checkpointer.migrations == []


def test_memory_is_bounded_and_resumes_still_write_through(make_saver: Callable[[], Any]) -> None:
    """Reading many threads doesn't grow memory; an evicted thread is re-tracked by the resume's own read."""
    saver = make_saver()
    old = two_breakpoints(("x", "y3"))(saver)
    for n in range(20):
        old.invoke({"log": []}, {"configurable": {"thread_id": f"t{n}"}})
    graph = gl.with_migrations(two_breakpoints(("y3", "x"))(saver), MIGRATIONS, max_tracked=5)
    for n in range(20):
        graph.get_state({"configurable": {"thread_id": f"t{n}"}})  # a dashboard polling every thread
    assert len(graph.checkpointer._repaired) == 5

    config = {"configurable": {"thread_id": "t0"}}  # long since evicted
    graph.invoke(None, config)
    assert log_of(graph.invoke(None, config))[-1] == "join"


def test_repairs_are_counted_and_logged(
    make_saver: Callable[[], Any], caplog: pytest.LogCaptureFixture
) -> None:
    saver = make_saver()
    two_breakpoints(("x", "y3"))(saver).invoke({"log": []}, CFG)
    graph = gl.with_migrations(two_breakpoints(("y3", "x"))(saver), MIGRATIONS)
    with caplog.at_level(logging.DEBUG, logger="graphlock"):
        graph.get_state(CFG)
    assert graph.checkpointer.stats == {"rename_channel('join:x+y3:join', 'join:y3+x:join')": 1}
    assert "repaired thread t1" in caplog.text
