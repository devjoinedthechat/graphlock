"""Property tests: random graphs, random refactors, random pause points.

The corpus pins the failure classes someone thought of. These look for the ones nobody did. Each
example builds a random graph (a DAG with fan-ins, nodes that wait in `interrupt()`, `defer` flags,
a static breakpoint, an extra state field), pauses a thread somewhere in it, deploys a refactor that
should not change what the thread does, and resumes. Every node logs a label that survives renames,
so the oracle is simple: the resumed thread must log the same labels as the graph run straight
through.

The properties:
- `check` never misses a break: if the thread ends wrong, `check` reported something blocking;
- `scan` never misses one, and never cries wolf: it flags the thread exactly when it ends wrong;
- the migration for the refactor makes the thread end right;
- `check --reverse` never misses a break when the thread is paused under the new graph and resumed
  under the old one.

`HYPOTHESIS_PROFILE=deep` runs thousands of examples instead of the default hundred.
"""

from __future__ import annotations

import dataclasses
import operator
from collections import Counter
from collections.abc import Callable
from typing import Annotated, Any

from hypothesis import HealthCheck, assume, given, note, settings
from hypothesis import strategies as st
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

import graphlock as gl

# One task at a time: parallel tasks finish in whatever order the thread pool allows, which changes
# what an interrupted step stores and makes a failing example impossible to replay.
CFG: dict[str, Any] = {"configurable": {"thread_id": "t"}, "max_concurrency": 1}
MAX_RESUMES = 12
OK, WRONG, CRASH = "ok", "wrong", "crash"


class WithNote(TypedDict, total=False):
    log: Annotated[list[str], operator.add]
    note: str


class WithoutNote(TypedDict, total=False):
    log: Annotated[list[str], operator.add]


@dataclasses.dataclass(frozen=True)
class Spec:
    order: tuple[str, ...]  # node names, in a topological order
    preds: dict[str, tuple[str, ...]]  # node -> its predecessors (one edge, or a fan-in of two)
    labels: dict[str, str]  # node -> what it logs; stable across renames
    askers: frozenset[str]  # nodes that wait in interrupt()
    deferred: frozenset[str]
    breakpoint: str | None  # interrupt_before
    note: bool  # the first node writes an extra state field

    def build(self, saver: Any) -> Any:
        b = StateGraph(WithNote if self.note else WithoutNote)
        for name in self.order:
            b.add_node(
                name,
                _node(self.labels[name], name in self.askers, name == self.order[0] and self.note),
                defer=name in self.deferred,
            )
        b.add_edge(START, self.order[0])
        for name in self.order[1:]:
            preds = self.preds[name]
            if len(preds) == 1:
                b.add_edge(preds[0], name)
            else:
                b.add_edge(list(preds), name)
        return b.compile(checkpointer=saver, interrupt_before=[self.breakpoint] if self.breakpoint else None)


def _node(label: str, asks: bool, writes_note: bool) -> Callable[[Any], dict[str, Any]]:
    def run(state: Any) -> dict[str, Any]:
        entry = f"{label}:{interrupt(label)}" if asks else label
        return {"log": [entry], **({"note": "kept"} if writes_note else {})}

    return run


@st.composite
def specs(draw: st.DrawFn) -> Spec:
    n = draw(st.integers(3, 6))
    order = tuple(f"n{i}" for i in range(n))
    preds: dict[str, tuple[str, ...]] = {}
    for i in range(1, n):
        k = draw(st.integers(1, min(2, i)))
        preds[order[i]] = tuple(
            draw(st.lists(st.sampled_from(order[:i]), min_size=k, max_size=k, unique=True))
        )
    return Spec(
        order=order,
        preds=preds,
        labels={name: f"L{i}" for i, name in enumerate(order)},
        askers=frozenset(draw(st.lists(st.sampled_from(order), unique=True, max_size=2))),
        deferred=frozenset(draw(st.lists(st.sampled_from(order[1:]), unique=True, max_size=1))),
        breakpoint=draw(st.none() | st.sampled_from(order[1:])),
        note=draw(st.booleans()),
    )


@dataclasses.dataclass(frozen=True)
class Refactor:
    kind: str
    target: str | None
    after: Spec
    migrations: tuple[gl.Migration, ...]


def _rename(spec: Spec, old: str) -> Refactor:
    new = f"{old}_v2"

    def swap(name: str) -> str:
        return new if name == old else name

    after = dataclasses.replace(
        spec,
        order=tuple(swap(n) for n in spec.order),
        preds={swap(n): tuple(swap(p) for p in ps) for n, ps in spec.preds.items()},
        labels={swap(n): label for n, label in spec.labels.items()},
        askers=frozenset(swap(n) for n in spec.askers),
        deferred=frozenset(swap(n) for n in spec.deferred),
        breakpoint=swap(spec.breakpoint) if spec.breakpoint else None,
    )
    return Refactor("rename", old, after, (gl.rename_node(old, new),))


def _reorder(spec: Spec, node: str) -> Refactor:
    old = spec.preds[node]
    new = tuple(reversed(old))
    after = dataclasses.replace(spec, preds={**spec.preds, node: new})
    migration = gl.rename_channel(f"join:{'+'.join(old)}:{node}", f"join:{'+'.join(new)}:{node}")
    return Refactor("reorder", node, after, (migration,))


def _toggle_defer(spec: Spec, node: str) -> Refactor:
    after = dataclasses.replace(spec, deferred=spec.deferred ^ {node})
    return Refactor("defer", node, after, (gl.defer_changed(node),))


def _drop_note(spec: Spec) -> Refactor:
    return Refactor("drop_note", "note", dataclasses.replace(spec, note=False), (gl.drop_field("note"),))


@st.composite
def refactors(draw: st.DrawFn, spec: Spec) -> Refactor:
    options: list[Callable[[], Refactor]] = [lambda: _rename(spec, draw(st.sampled_from(spec.order)))]
    fan_ins = [n for n, ps in spec.preds.items() if len(ps) == 2]
    if fan_ins:
        options.append(lambda: _reorder(spec, draw(st.sampled_from(fan_ins))))
    options.append(lambda: _toggle_defer(spec, draw(st.sampled_from(spec.order[1:]))))
    if spec.note:
        options.append(lambda: _drop_note(spec))
    return draw(st.sampled_from(options))()


def answer(state: Any) -> Any:
    """What a person sends back: "ok" to every pending interrupt (by id when there are several)."""
    if not state.interrupts:
        return None
    if len(state.interrupts) == 1:
        return Command(resume="ok")
    return Command(resume={i.id: "ok" for i in state.interrupts})


def drive(graph: Any, start: Any = None, resumes: int = MAX_RESUMES) -> Any:
    """Run or resume until the thread finishes, answering every interrupt "ok"."""
    result = graph.invoke(start, CFG) if start is not None else None
    for _ in range(resumes):
        state = graph.get_state(CFG)
        if not state.next:
            return state.values
        result = graph.invoke(answer(state), CFG)
    return None if graph.get_state(CFG).next else result


def labels(values: Any) -> Counter[str]:
    return Counter((values or {}).get("log", []))


def expected(spec: Spec) -> Counter[str]:
    return labels(drive(spec.build(InMemorySaver()), {"log": []}))


def pause(spec: Spec, saver: Any, advance: int) -> bool:
    """Start a thread under `spec` and resume it `advance` times. Whether it is still paused."""
    graph = spec.build(saver)
    graph.invoke({"log": []}, CFG)
    for _ in range(advance):
        state = graph.get_state(CFG)
        if not state.next:
            break
        graph.invoke(answer(state), CFG)
    return bool(graph.get_state(CFG).next)


def outcome(graph: Any, want: Counter[str]) -> str:
    try:
        values = drive(graph)
    except Exception:
        return CRASH
    return OK if values is not None and labels(values) == want else WRONG


PROPERTY = settings(
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)


@st.composite
def cases(draw: st.DrawFn) -> tuple[Spec, Refactor, int]:
    spec = draw(specs())
    return spec, draw(refactors(spec)), draw(st.integers(0, 2))


@PROPERTY
@given(cases())
def test_graphlock_agrees_with_what_langgraph_does(case: tuple[Spec, Refactor, int]) -> None:
    spec, refactor, advance = case
    saver = InMemorySaver()
    assume(pause(spec, saver, advance))
    before = gl.extract_shape(spec.build(None))
    after_graph = refactor.after.build(saver)
    after = gl.extract_shape(after_graph)
    report = gl.scan(after_graph, saver, lock=before)
    result = outcome(after_graph, expected(spec))
    blocking = [f for f in gl.check(before, after) if f.blocking]
    note(
        f"{refactor.kind} {refactor.target}: {result}; check {[f.code for f in blocking]}; "
        f"scan {[(i.code, i.subject) for i in report.blocking]}"
    )

    if result != OK:
        assert blocking, "check missed a break"
        assert report.blocking, "scan missed a break"
    else:
        assert not report.blocking, "scan flagged a thread that resumes correctly"


@PROPERTY
@given(cases())
def test_the_migration_repairs_the_thread(case: tuple[Spec, Refactor, int]) -> None:
    spec, refactor, advance = case
    saver = InMemorySaver()
    assume(pause(spec, saver, advance))
    graph = gl.with_migrations(refactor.after.build(saver), list(refactor.migrations))
    assert outcome(graph, expected(spec)) == OK


@PROPERTY
@given(cases())
def test_rollback_check_misses_nothing(case: tuple[Spec, Refactor, int]) -> None:
    spec, refactor, advance = case
    saver = InMemorySaver()
    assume(pause(refactor.after, saver, advance))
    result = outcome(spec.build(saver), expected(refactor.after))
    if result != OK:
        rollback = gl.check_rollback(
            gl.extract_shape(refactor.after.build(None)), gl.extract_shape(spec.build(None))
        )
        assert [f for f in rollback if f.blocking], "check --reverse missed a break"


@PROPERTY
@given(specs(), st.integers(0, 2))
def test_an_unchanged_graph_is_left_alone(spec: Spec, advance: int) -> None:
    """With no change deployed, every paused thread ends as if it had never paused (the harness is
    sound), and graphlock reports nothing blocking (no false alarms on ordinary threads)."""
    saver = InMemorySaver()
    assume(pause(spec, saver, advance))
    graph = spec.build(saver)
    shape = gl.extract_shape(graph)
    assert not [f for f in gl.check(shape, shape) if f.blocking]
    assert not gl.scan(graph, saver, lock=shape).blocking
    assert outcome(graph, expected(spec)) == OK
