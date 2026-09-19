"""What graphlock reports, and the catalogue of rules behind it."""

from __future__ import annotations

import dataclasses
import enum
from typing import Any


class Severity(str, enum.Enum):
    BREAKING = "breaking"  # stored threads crash, stop, or resume with wrong data
    WARNING = "warning"  # stored threads resume, but may behave differently than they would have
    INFO = "info"  # worth knowing; nothing breaks

    def __str__(self) -> str:
        return self.value


@dataclasses.dataclass(frozen=True)
class Rule:
    code: str
    name: str
    severity: Severity
    langgraph_does: str  # what LangGraph does to a stored thread, as measured in tests/corpus.py
    fix: str


RULES: dict[str, Rule] = {
    r.code: r
    for r in [
        Rule(
            "GL101",
            "node-removed",
            Severity.BREAKING,
            "A thread paused before or inside the node, or holding a pending Send or a finished parallel "
            "write for it, resumes as if it were finished: the node and everything after it never run. "
            "No error is raised.",
            "If the node was renamed, add `rename_node(old, new)`. If it was removed, drain the threads "
            "that `graphlock scan` lists first.",
        ),
        Rule(
            "GL102",
            "defer-changed",
            Severity.BREAKING,
            "The node's trigger channel changes class and its stored value no longer fits: resuming "
            "raises TypeError or ValueError (langgraph#8629, langgraph#8618).",
            "Add `defer_changed(node)`.",
        ),
        Rule(
            "GL103",
            "join-changed",
            Severity.BREAKING,
            "The fan-in channel is renamed, so a thread where some of the sources already finished waits "
            "for the rest forever: the target node never runs. No error is raised. Reordering the list "
            "passed to `add_edge([...], node)` is enough to cause it.",
            "Keep the original order of sources, or add `rename_channel(old, new)`.",
        ),
        Rule(
            "GL104",
            "subgraph-changed",
            Severity.BREAKING,
            "The node stopped or started being a subgraph. A thread paused inside the old subgraph loses "
            "its subgraph state.",
            "Drain threads paused inside the node before deploying.",
        ),
        Rule(
            "GL201",
            "required-field-added",
            Severity.BREAKING,
            "Stored threads have no value for the field, so building the state for the next node raises "
            "a validation error.",
            "Give the field a default, or add `set_default(field, value)`.",
        ),
        Rule(
            "GL202",
            "field-type-changed",
            Severity.BREAKING,
            "Pydantic state validates restored values against the new type, so a stored value that no "
            "longer fits raises a validation error at the next node. TypedDict and dataclass state pass "
            "the old value through unchanged.",
            "Add `convert_field(field, fn)`; it only touches values that no longer fit.",
        ),
        Rule(
            "GL203",
            "field-removed",
            Severity.BREAKING,
            "The stored value stays in the checkpoint under a channel the graph no longer has. On resume "
            "LangGraph marks only the new graph's channels as seen by the interrupt_before check, so the "
            "removed one always looks updated: a thread holding a value for it that is paused at an "
            "interrupt_before breakpoint, or reaches one later, pauses again on every resume and never "
            "gets past it. No error is raised. interrupt_after is not affected, and in a graph without "
            "interrupt_before the value is simply no longer read.",
            "Add `drop_field(field)`.",
        ),
        Rule(
            "GL204",
            "reducer-changed",
            Severity.WARNING,
            "The stored value is kept, but the next write to it is merged with the new reducer. When the "
            "type changed too, a stored value of the old type can make that write raise: "
            "operator.add('text', ['more']) is a TypeError. That case is breaking.",
            "Check that the stored values make sense under the new reducer, or add "
            "`convert_field(field, fn)`.",
        ),
        Rule(
            "GL205",
            "channel-kind-changed",
            Severity.BREAKING,
            "The channel's stored value is restored into a channel class that expects a different shape.",
            "Add `convert_field(field, fn)` or drain the threads that hold a value for it.",
        ),
        Rule(
            "GL301",
            "stored-class-missing",
            Severity.BREAKING,
            "Checkpoints store objects by import path. When the class is renamed or moved, stored "
            "objects come back as a plain dict or None instead. No error is raised.",
            "Keep an alias at the old path, or add `revive(OldPath, NewClass, fields=[...])`.",
        ),
        Rule(
            "GL401",
            "interrupt-order-changed",
            Severity.BREAKING,
            "A thread paused inside the node resumes by running the node again from the top and handing "
            "its stored answers to the `interrupt()` calls by position. When a call moves (reordered, or "
            "one inserted or removed before it), answers go to the wrong questions, with no error. When a "
            "call is removed from the end, the node runs on without it and the waiting answer is dropped "
            "(a warning). Reworded prompts at the same positions are fine (GL402).",
            "Keep existing `interrupt()` calls in their order and add new ones after them, or drain the "
            "threads paused inside the node.",
        ),
        Rule(
            "GL402",
            "interrupting-node-changed",
            Severity.INFO,
            "A thread paused inside the node will run the new code from the top when it resumes, "
            "including anything before the `interrupt()` call it is waiting on. Stored answers still go to "
            "the calls at the same positions, so reworded prompts are safe.",
            "Make sure the code before the `interrupt()` is safe to run again.",
        ),
    ]
}


@dataclasses.dataclass(frozen=True)
class Finding:
    code: str
    subject: str  # the node, field, channel or class the finding is about
    message: str
    graph: str = ""  # "" for the root graph, "research" for a subgraph node, "a/b" when nested
    severity: Severity | None = None  # defaults to the rule's severity
    hint: str | None = None
    handled_by: str | None = None  # the migration that repairs stored threads, if one is registered

    @property
    def rule(self) -> Rule:
        return RULES[self.code]

    @property
    def level(self) -> Severity:
        return self.severity or self.rule.severity

    @property
    def blocking(self) -> bool:
        return self.level is Severity.BREAKING and self.handled_by is None

    @property
    def where(self) -> str:
        return f"{self.graph}/{self.subject}" if self.graph else self.subject

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "rule": self.rule.name,
            "severity": str(self.level),
            "graph": self.graph,
            "subject": self.subject,
            "message": self.message,
            "hint": self.hint,
            "handled_by": self.handled_by,
            "blocking": self.blocking,
        }
