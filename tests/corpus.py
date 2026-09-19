"""Redeploy scenarios: pause a thread under graph v1, deploy v2, resume.

Each scenario records what LangGraph does today with no help (`today`), the rule `graphlock check`
must report for the change (`rule`), the issue `graphlock scan` must report for the paused thread
(`scan`), and the migrations that make the thread resume correctly (`migrations`). The tests in
test_corpus.py hold all four to account, against both the in-memory and the SQLite checkpointer.
"""

from __future__ import annotations

import dataclasses
import operator
import sys
import types
from collections.abc import Callable, Sequence
from typing import Annotated, Any

from langgraph.graph import START, StateGraph
from langgraph.types import Command, Send, interrupt
from pydantic import BaseModel
from typing_extensions import TypedDict

import graphlock as gl

CFG: dict[str, Any] = {"configurable": {"thread_id": "t1"}}

OK, SILENT, CRASH = "ok", "silent", "crash"


class S(TypedDict, total=False):
    log: Annotated[list[str], operator.add]
    amount: int


def step(name: str) -> Callable[[Any], dict[str, Any]]:
    def run(state: Any) -> dict[str, Any]:
        return {"log": [name]}

    run.__name__ = f"step_{name}"
    return run


def log_of(result: Any) -> list[str]:
    if isinstance(result, dict):
        return list(result.get("log", []))
    return list(getattr(result, "log", []))


@dataclasses.dataclass
class Scenario:
    id: str
    title: str
    v1: Callable[[Any], Any]  # saver -> compiled graph
    v2: Callable[[Any], Any]
    start: Callable[[Any], Any]  # run v1 until the thread pauses
    resume: Callable[[Any], Any]  # resume under v2; returns the final state
    expected: Callable[[Any], bool]  # is the resumed result correct?
    today: str  # OK | SILENT | CRASH: what LangGraph does with no help
    rule: str | None  # the blocking rule `check` must report; None when nothing blocks
    scan: str | None  # the issue `scan` must report for the paused thread; None when there is none
    # None: no migration can repair it. A callable is called after v2 is built (for classes v2 defines).
    migrations: Sequence[gl.Migration] | Callable[[], Sequence[gl.Migration]] | None = None
    source: str = ""  # the LangGraph issue, when one exists
    info: str | None = None  # a non-blocking rule `check` must report


def start_plain(g: Any) -> Any:
    return g.invoke({"log": []}, CFG)


def resume_none(g: Any) -> Any:
    return g.invoke(None, CFG)


def resume_yes(g: Any) -> Any:
    return g.invoke(Command(resume="yes"), CFG)


# ---------------------------------------------------------------- nodes renamed or removed


def linear(review: str, *, pause: str = "before") -> Callable[[Any], Any]:
    def build(saver: Any) -> Any:
        b = StateGraph(S)
        b.add_node("draft", step("draft"))
        b.add_node(review, step(review))
        b.add_node("issue", step("issue"))
        b.add_edge(START, "draft")
        b.add_edge("draft", review)
        b.add_edge(review, "issue")
        return b.compile(checkpointer=saver, interrupt_before=[review])

    return build


def removed(saver: Any) -> Any:
    b = StateGraph(S)
    b.add_node("draft", step("draft"))
    b.add_node("issue", step("issue"))
    b.add_edge(START, "draft")
    b.add_edge("draft", "issue")
    return b.compile(checkpointer=saver)


def approve_node(state: Any) -> dict[str, Any]:
    answer = interrupt("approve?")
    return {"log": [f"approved:{answer}"]}


def asking(name: str) -> Callable[[Any], Any]:
    def build(saver: Any) -> Any:
        b = StateGraph(S)
        b.add_node("draft", step("draft"))
        b.add_node(name, approve_node)
        b.add_node("issue", step("issue"))
        b.add_edge(START, "draft")
        b.add_edge("draft", name)
        b.add_edge(name, "issue")
        return b.compile(checkpointer=saver)

    return build


def fan_out(worker: str) -> Callable[[Any], Any]:
    def work(state: Any) -> dict[str, Any]:
        return {"log": [f"work:{state['amount']}"]}

    def build(saver: Any) -> Any:
        b = StateGraph(S)
        b.add_node("plan", step("plan"))
        b.add_node(worker, work)
        b.add_edge(START, "plan")
        b.add_conditional_edges("plan", lambda s: [Send(worker, {"amount": i}) for i in (1, 2)], [worker])
        return b.compile(checkpointer=saver, interrupt_before=[worker])

    return build


def ask_ok(state: Any) -> dict[str, Any]:
    return {"log": [f"ask:{interrupt('ok?')}"]}


def parallel(fetch: str) -> Callable[[Any], Any]:
    def build(saver: Any) -> Any:
        b = StateGraph(S)
        b.add_node("start", step("start"))
        b.add_node(fetch, step("fetch"))
        b.add_node("ask", ask_ok)
        b.add_node("done", step("done"))
        b.add_edge(START, "start")
        b.add_edge("start", fetch)
        b.add_edge("start", "ask")
        b.add_edge([fetch, "ask"], "done")
        return b.compile(checkpointer=saver)

    return build


def inner_ask(state: Any) -> dict[str, Any]:
    return {"log": [f"inner:{interrupt('q')}"]}


def with_subgraph(name: str) -> Callable[[Any], Any]:
    def build(saver: Any) -> Any:
        sb = StateGraph(S)
        sb.add_node("inner", inner_ask)
        sb.add_edge(START, "inner")
        b = StateGraph(S)
        b.add_node(name, sb.compile())
        b.add_node("after", step("after"))
        b.add_edge(START, name)
        b.add_edge(name, "after")
        return b.compile(checkpointer=saver)

    return build


def subgraph_inner(inner: str, ask: Callable[[Any], Any] = inner_ask) -> Callable[[Any], Any]:
    """A parent that runs subgraph 'research', whose node `inner` waits in interrupt()."""

    def build(saver: Any) -> Any:
        sb = StateGraph(S)
        sb.add_node(inner, ask)
        sb.add_edge(START, inner)
        b = StateGraph(S)
        b.add_node("research", sb.compile())
        b.add_node("after", step("after"))
        b.add_edge(START, "research")
        b.add_edge("research", "after")
        return b.compile(checkpointer=saver)

    return build


# ---------------------------------------------------------------- defer and fan-in


def deferred(defer: bool) -> Callable[[Any], Any]:
    def build(saver: Any) -> Any:
        b = StateGraph(S)
        b.add_node("a", step("a"))
        b.add_node("b", step("b"), defer=defer)
        b.add_edge(START, "a")
        b.add_edge("a", "b")
        return b.compile(checkpointer=saver, interrupt_before=["b"])

    return build


def fan_in(defer: bool = False, sources: Sequence[str] = ("x", "y2")) -> Callable[[Any], Any]:
    """x finishes at step 2; y -> y2 pauses before y2, so the barrier into 'join' is half full."""

    def build(saver: Any) -> Any:
        b = StateGraph(S)
        for name in ("split", "x", "y", "y2"):
            b.add_node(name, step(name))
        b.add_node("join", step("join"), defer=defer)
        b.add_edge(START, "split")
        b.add_edge("split", "x")
        b.add_edge("split", "y")
        b.add_edge("y", "y2")
        b.add_edge(list(sources), "join")
        return b.compile(checkpointer=saver, interrupt_before=["y2"])

    return build


# ---------------------------------------------------------------- state


class OrderV1(BaseModel):
    log: Annotated[list[str], operator.add] = []
    ref: str = ""


class OrderRequired(BaseModel):
    log: Annotated[list[str], operator.add] = []
    ref: str = ""
    currency: str


class OrderIntRef(BaseModel):
    log: Annotated[list[str], operator.add] = []
    ref: int = 0


def set_ref(state: Any) -> dict[str, Any]:
    return {"log": ["a"], "ref": "PO-123"}


def read_ref(state: Any) -> dict[str, Any]:
    return {"log": [f"b:{state.ref}"]}


def pydantic_graph(schema: type) -> Callable[[Any], Any]:
    def build(saver: Any) -> Any:
        b = StateGraph(schema)
        b.add_node("a", set_ref if schema is OrderV1 else step("a"))
        b.add_node("b", read_ref)
        b.add_edge(START, "a")
        b.add_edge("a", "b")
        return b.compile(checkpointer=saver, interrupt_before=["b"])

    return build


def typed_state(extra: dict[str, Any]) -> type:
    return TypedDict("State", {"log": Annotated[list[str], operator.add], **extra}, total=False)  # type: ignore[operator]


NoteState = typed_state({"note": str})
NoNoteState = typed_state({})


class ItemsPlain(TypedDict, total=False):
    items: list[str]


class ItemsAdd(TypedDict, total=False):
    items: Annotated[list[str], operator.add]


def write_note(state: Any) -> dict[str, Any]:
    return {"log": ["a"], "note": "keep me"}


def ask_b(state: Any) -> dict[str, Any]:
    return {"log": [f"b:{interrupt('b?')}"]}


def note_graph(schema: type, *, pause: str = "before") -> Callable[[Any], Any]:
    """a writes `note` (in v1), then b. `pause`: "before" b, "after" a, or inside b's interrupt()."""

    def build(saver: Any) -> Any:
        b = StateGraph(schema)
        b.add_node("a", write_note if schema is NoteState else step("a"))
        b.add_node("b", ask_b if pause == "inside" else step("b"))
        b.add_edge(START, "a")
        b.add_edge("a", "b")
        return b.compile(
            checkpointer=saver,
            interrupt_before=["b"] if pause == "before" else None,
            interrupt_after=["a"] if pause == "after" else None,
        )

    return build


def note_then_breakpoint(schema: type) -> Callable[[Any], Any]:
    """a writes `note` (in v1), b waits in interrupt(), and c has an interrupt_before breakpoint."""

    def build(saver: Any) -> Any:
        b = StateGraph(schema)
        b.add_node("a", write_note if schema is NoteState else step("a"))
        b.add_node("b", ask_b)
        b.add_node("c", step("c"))
        b.add_edge(START, "a")
        b.add_edge("a", "b")
        b.add_edge("b", "c")
        return b.compile(checkpointer=saver, interrupt_before=["c"])

    return build


def resume_yes_then_continue(g: Any) -> Any:
    g.invoke(Command(resume="yes"), CFG)  # answers b, then pauses before c
    return g.invoke(None, CFG)


def items_graph(schema: type) -> Callable[[Any], Any]:
    def build(saver: Any) -> Any:
        b = StateGraph(schema)
        b.add_node("a", lambda s: {"items": ["x"]})
        b.add_node("b", lambda s: {"items": ["y"]})
        b.add_edge(START, "a")
        b.add_edge("a", "b")
        return b.compile(checkpointer=saver, interrupt_before=["b"])

    return build


# A class stored in state, renamed between deploys. It lives in a real module so checkpoints can
# store it by import path, and "deploying v2" swaps that module's contents.
MODELS = "graphlock_corpus_models"


class _Models:
    def deploy(self, version: int) -> types.ModuleType:
        module = types.ModuleType(MODELS)
        if version == 1:
            exec(
                "from pydantic import BaseModel\nclass Order(BaseModel):\n    sku: str\n    qty: int\n",
                module.__dict__,
            )
        else:
            exec(
                "from pydantic import BaseModel\n"
                "class PurchaseOrder(BaseModel):\n    sku: str\n    qty: int\n",
                module.__dict__,
            )
        sys.modules[MODELS] = module
        return module


MODELS_MODULE = _Models()


def order_graph(version: int) -> Callable[[Any], Any]:
    def build(saver: Any) -> Any:
        module = MODELS_MODULE.deploy(version)
        cls = module.Order if version == 1 else module.PurchaseOrder
        state = TypedDict(  # noqa: UP013 - the field's class is only known at runtime
            "OrderState", {"log": Annotated[list[str], operator.add], "order": cls}, total=False
        )  # type: ignore[operator]

        def place(s: Any) -> dict[str, Any]:
            return {"log": ["a"], "order": cls(sku="X1", qty=3)}

        def ship(s: Any) -> dict[str, Any]:
            order = s.get("order")
            return {"log": [f"ship:{type(order).__name__}:{getattr(order, 'qty', None)}"]}

        b = StateGraph(state)
        b.add_node("a", place)
        b.add_node("b", ship)
        b.add_edge(START, "a")
        b.add_edge("a", "b")
        return b.compile(checkpointer=saver, interrupt_before=["b"])

    return build


# ---------------------------------------------------------------- interrupts inside a node


def ask_amount_then_approver(state: Any) -> dict[str, Any]:
    amount = interrupt("amount?")
    approver = interrupt("approver?")
    return {"log": [f"amount={amount}", f"approver={approver}"]}


def ask_approver_then_amount(state: Any) -> dict[str, Any]:
    approver = interrupt("approver?")
    amount = interrupt("amount?")
    return {"log": [f"amount={amount}", f"approver={approver}"]}


def ask_amount_approver_reason(state: Any) -> dict[str, Any]:
    amount = interrupt("amount?")
    approver = interrupt("approver?")
    reason = interrupt("reason?")
    return {"log": [f"amount={amount}", f"approver={approver}", f"reason={reason}"]}


def asks(fn: Callable[[Any], Any]) -> Callable[[Any], Any]:
    def build(saver: Any) -> Any:
        b = StateGraph(S)
        b.add_node("ask", fn)
        b.add_edge(START, "ask")
        return b.compile(checkpointer=saver)

    return build


def start_answer_amount(g: Any) -> Any:
    g.invoke({"log": []}, CFG)
    return g.invoke(Command(resume="500"), CFG)  # answered amount?, now waiting on approver?


def resume_alice(g: Any) -> Any:
    return g.invoke(Command(resume="alice"), CFG)


# ---------------------------------------------------------------- safe changes (controls)


def retarget(after: str) -> Callable[[Any], Any]:
    def build(saver: Any) -> Any:
        b = StateGraph(S)
        for name in ("a", "b", "c", "d"):
            b.add_node(name, step(name))
        b.add_edge(START, "a")
        b.add_edge("a", "b")
        b.add_edge("b", after)
        return b.compile(checkpointer=saver, interrupt_before=["b"])

    return build


def extra_node(saver: Any) -> Any:
    b = StateGraph(S)
    for name in ("draft", "review", "issue", "notify"):
        b.add_node(name, step(name))
    b.add_edge(START, "draft")
    b.add_edge("draft", "review")
    b.add_edge("review", "issue")
    b.add_edge("issue", "notify")
    return b.compile(checkpointer=saver, interrupt_before=["review"])


# ---------------------------------------------------------------- the corpus

SCENARIOS: list[Scenario] = [
    Scenario(
        "rename-paused-node",
        "Rename the node a thread is paused before",
        linear("review"),
        linear("manager_review"),
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["draft", "manager_review", "issue"],
        today=SILENT,
        rule="GL101",
        scan="GL101",
        migrations=[gl.rename_node("review", "manager_review")],
    ),
    Scenario(
        "rename-interrupting-node",
        "Rename a node that is waiting in interrupt()",
        asking("approve"),
        asking("manager_approve"),
        start_plain,
        resume_yes,
        lambda r: log_of(r) == ["draft", "approved:yes", "issue"],
        today=SILENT,
        rule="GL101",
        scan="GL101",
        migrations=[gl.rename_node("approve", "manager_approve")],
    ),
    Scenario(
        "remove-paused-node",
        "Remove the node a thread is paused before",
        linear("review"),
        removed,
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["draft", "issue"],
        today=SILENT,
        rule="GL101",
        scan="GL101",
    ),
    Scenario(
        "rename-send-target",
        "Rename a node that pending Send()s are addressed to",
        fan_out("worker"),
        fan_out("processor"),
        start_plain,
        resume_none,
        lambda r: sorted(log_of(r)) == ["plan", "work:1", "work:2"],
        today=SILENT,
        rule="GL101",
        scan="GL101",
        migrations=[gl.rename_node("worker", "processor")],
    ),
    Scenario(
        "rename-parallel-sibling",
        "Rename a node that finished in parallel with one now waiting in interrupt()",
        parallel("fetch"),
        parallel("fetch_prices"),
        start_plain,
        resume_yes,
        lambda r: sorted(log_of(r)) == ["ask:yes", "done", "fetch", "start"],
        today=SILENT,
        rule="GL101",
        scan="GL101",
        migrations=[gl.rename_node("fetch", "fetch_prices")],
    ),
    Scenario(
        "rename-subgraph-node",
        "Rename a subgraph node while a thread is paused inside it",
        with_subgraph("research"),
        with_subgraph("research_v2"),
        start_plain,
        resume_yes,
        lambda r: log_of(r) == ["inner:yes", "after"],
        today=SILENT,
        rule="GL101",
        scan="GL101",
    ),
    Scenario(
        "rename-inside-subgraph",
        "Rename the node a thread is waiting in, inside a subgraph",
        subgraph_inner("inner"),
        subgraph_inner("inner_review"),
        start_plain,
        resume_yes,
        lambda r: log_of(r) == ["inner:yes", "after"],
        today=SILENT,
        rule="GL101",
        scan="GL101",
        migrations=[gl.rename_node("inner", "inner_review", graph="research")],
    ),
    Scenario(
        "interrupts-reordered-inside-subgraph",
        "Swap two interrupt() calls inside a subgraph while a thread sits between them",
        subgraph_inner("ask", ask_amount_then_approver),
        subgraph_inner("ask", ask_approver_then_amount),
        start_answer_amount,
        resume_alice,
        lambda r: sorted(log_of(r)) == ["after", "amount=500", "approver=alice"],
        today=SILENT,
        rule="GL401",
        scan="GL401",
    ),
    Scenario(
        "defer-on",
        "Turn on defer= for a node a thread is paused before",
        deferred(False),
        deferred(True),
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["a", "b"],
        today=CRASH,
        rule="GL102",
        scan="GL102",
        migrations=[gl.defer_changed("b")],
        source="langgraph#8629",
    ),
    Scenario(
        "defer-off",
        "Turn off defer= for a node a thread is paused before",
        deferred(True),
        deferred(False),
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["a", "b"],
        today=OK,
        rule=None,
        scan=None,
        info="GL102",
    ),
    Scenario(
        "fan-in-defer-on",
        "Turn on defer= for a fan-in node while its barrier is half full",
        fan_in(defer=False),
        fan_in(defer=True),
        start_plain,
        resume_none,
        lambda r: "join" in log_of(r),
        today=CRASH,
        rule="GL102",
        scan="GL102",
        migrations=[gl.defer_changed("join")],
        source="langgraph#8618",
    ),
    Scenario(
        "fan-in-defer-off",
        "Turn off defer= for a fan-in node while its barrier is half full",
        fan_in(defer=True),
        fan_in(defer=False),
        start_plain,
        resume_none,
        lambda r: "join" in log_of(r),
        today=CRASH,
        rule="GL102",
        scan="GL102",
        migrations=[gl.defer_changed("join")],
        source="langgraph#8618",
    ),
    Scenario(
        "fan-in-reordered",
        "List a fan-in's sources in a different order while its barrier is half full",
        fan_in(sources=("x", "y2")),
        fan_in(sources=("y2", "x")),
        start_plain,
        resume_none,
        lambda r: "join" in log_of(r),
        today=SILENT,
        rule="GL103",
        scan="GL103",
        migrations=[gl.rename_channel("join:x+y2:join", "join:y2+x:join")],
    ),
    Scenario(
        "required-field-added",
        "Add a required field to Pydantic state",
        pydantic_graph(OrderV1),
        pydantic_graph(OrderRequired),
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["a", "b:PO-123"],
        today=CRASH,
        rule="GL201",
        scan="GL201",
        migrations=[gl.set_default("currency", "USD")],
    ),
    Scenario(
        "field-type-tightened",
        "Change a Pydantic state field from str to int",
        pydantic_graph(OrderV1),
        pydantic_graph(OrderIntRef),
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["a", "b:123"],
        today=CRASH,
        rule="GL202",
        scan="GL202",
        migrations=[gl.convert_field("ref", lambda v: int(str(v).removeprefix("PO-")))],
    ),
    Scenario(
        "class-renamed",
        "Rename a class whose objects are stored in state",
        order_graph(1),
        order_graph(2),
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["a", "ship:PurchaseOrder:3"],
        today=SILENT,
        rule="GL301",
        scan="GL301",
        migrations=lambda: [gl.revive(f"{MODELS}:Order", sys.modules[MODELS].PurchaseOrder)],
    ),
    Scenario(
        "interrupts-reordered",
        "Swap two interrupt() calls while a thread sits between them",
        asks(ask_amount_then_approver),
        asks(ask_approver_then_amount),
        start_answer_amount,
        resume_alice,
        lambda r: sorted(log_of(r)) == ["amount=500", "approver=alice"],
        today=SILENT,
        rule="GL401",
        scan="GL401",
    ),
    Scenario(
        "interrupt-appended",
        "Add a new interrupt() after the existing ones",
        asks(ask_amount_then_approver),
        asks(ask_amount_approver_reason),
        start_answer_amount,
        resume_alice,
        lambda r: log_of(r) == [] and len(r.get("__interrupt__", ())) == 1,
        today=OK,
        rule=None,
        scan=None,
        info="GL402",
    ),
    Scenario(
        "edge-retargeted",
        "Point the edge out of the paused node somewhere else",
        retarget("c"),
        retarget("d"),
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["a", "b", "d"],
        today=OK,
        rule=None,
        scan=None,
    ),
    Scenario(
        "node-added",
        "Add a node after the paused one",
        linear("review"),
        extra_node,
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["draft", "review", "issue", "notify"],
        today=OK,
        rule=None,
        scan=None,
    ),
    Scenario(
        "field-removed",
        "Remove a state field while a thread is paused at a breakpoint",
        note_graph(NoteState),
        note_graph(NoNoteState),
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["a", "b"],
        today=SILENT,
        rule="GL203",
        scan="GL203",
        migrations=[gl.drop_field("note")],
    ),
    Scenario(
        "field-removed-no-breakpoint",
        "Remove a state field while a thread waits in interrupt()",
        note_graph(NoteState, pause="inside"),
        note_graph(NoNoteState, pause="inside"),
        start_plain,
        resume_yes,
        lambda r: log_of(r) == ["a", "b:yes"],
        today=OK,
        rule=None,
        scan=None,
        info="GL203",
    ),
    Scenario(
        "field-removed-interrupt-after",
        "Remove a state field while a thread is paused at an interrupt_after breakpoint",
        note_graph(NoteState, pause="after"),
        note_graph(NoNoteState, pause="after"),
        start_plain,
        resume_none,
        lambda r: log_of(r) == ["a", "b"],
        today=OK,
        rule=None,
        scan=None,
        info="GL203",
    ),
    Scenario(
        "field-removed-later-breakpoint",
        "Remove a state field while a thread waits in interrupt() before a breakpoint",
        note_then_breakpoint(NoteState),
        note_then_breakpoint(NoNoteState),
        start_plain,
        resume_yes_then_continue,
        lambda r: log_of(r) == ["a", "b:yes", "c"],
        today=SILENT,
        rule="GL203",
        scan="GL203",
        migrations=[gl.drop_field("note")],
    ),
    Scenario(
        "reducer-added",
        "Add a reducer to a state field",
        items_graph(ItemsPlain),
        items_graph(ItemsAdd),
        lambda g: g.invoke({}, CFG),
        resume_none,
        lambda r: r.get("items") == ["x", "y"],
        today=OK,
        rule=None,
        scan=None,
        info="GL204",
    ),
]

BY_ID = {s.id: s for s in SCENARIOS}
