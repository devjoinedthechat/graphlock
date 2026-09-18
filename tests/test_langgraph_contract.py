"""The LangGraph internals graphlock recomputes, checked against what LangGraph itself produces.

`rename_node` re-keys pending writes and interrupts by recomputing task and interrupt ids. If a
LangGraph release changes how they are derived, these fail before any migration silently misfires.
"""

from __future__ import annotations

from typing import Any

from corpus import CFG, S, step
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import Send, interrupt

from graphlock import _lg


def ask(state: Any) -> dict[str, Any]:
    return {"log": [f"ask:{interrupt('ok?')}"]}


def paused_inside_ask() -> tuple[Any, Any]:
    saver = InMemorySaver()
    b = StateGraph(S)
    b.add_node("a", step("a"))
    b.add_node("ask", ask)
    b.add_edge(START, "a")
    b.add_edge("a", "ask")
    graph = b.compile(checkpointer=saver)
    graph.invoke({"log": []}, CFG)
    return graph, saver.get_tuple(CFG)


def test_pull_task_and_interrupt_ids() -> None:
    graph, saved = paused_inside_ask()
    state = graph.get_state(CFG)
    step_ = saved.metadata["step"] + 1
    task_id = _lg.pull_task_id(saved.checkpoint, "", step_, "ask", graph.nodes["ask"].triggers)
    assert [t.id for t in state.tasks] == [task_id]
    assert [i.id for i in state.interrupts] == [_lg.interrupt_id("", "ask", task_id)]


def test_push_task_ids() -> None:
    saver = InMemorySaver()
    b = StateGraph(S)
    b.add_node("plan", step("plan"))
    b.add_node("work", step("work"))
    b.add_edge(START, "plan")
    b.add_conditional_edges("plan", lambda s: [Send("work", {"amount": i}) for i in (1, 2)], ["work"])
    graph = b.compile(checkpointer=saver, interrupt_before=["work"])
    graph.invoke({"log": []}, CFG)
    saved = saver.get_tuple(CFG)
    step_ = saved.metadata["step"] + 1
    expected = {_lg.push_task_id(saved.checkpoint, "", step_, "work", i) for i in (0, 1)}
    assert {t.id for t in graph.get_state(CFG).tasks} == expected


def test_channel_names() -> None:
    b = StateGraph(S)
    for name in ("x", "y", "join"):
        b.add_node(name, step(name))
    b.add_edge(START, "x")
    b.add_edge(START, "y")
    b.add_edge(["x", "y"], "join")
    graph = b.compile()
    assert _lg.branch_channel("x") in graph.channels
    joins = [c for c in graph.channels if _lg.parse_join(c)]
    assert joins == [_lg.join_channel(["x", "y"], "join")]
    assert _lg.parse_join(joins[0]) == (["x", "y"], "join")


def test_next_task_names_plans_like_the_run_loop() -> None:
    graph, saved = paused_inside_ask()
    channels, _ = _lg.channels_from_checkpoint(graph.channels, saved.checkpoint)
    names = _lg.next_task_names(
        saved.checkpoint,
        saved.pending_writes,
        graph,
        channels=channels,
        config=saved.config,
        step=saved.metadata["step"] + 1,
    )
    assert list(names.values()) == ["ask"]
