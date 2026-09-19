"""interrupt() calls are found where a node makes them, including through the helpers it calls."""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

import pytest
from langgraph.types import interrupt

from graphlock.interrupts import code_digest, interrupt_sites


def ask_amount(state: Any) -> Any:
    return interrupt("amount?")


def ask_both(state: Any) -> Any:
    ask_amount(state)
    return interrupt("approver?")


def node_direct(state: Any) -> dict[str, Any]:
    amount = interrupt("amount?")
    approver = interrupt("approver?")
    return {"log": [amount, approver]}


def node_via_helper(state: Any) -> dict[str, Any]:
    first = ask_amount(state)
    second = interrupt("approver?")
    return {"log": [first, second]}


def node_two_deep(state: Any) -> dict[str, Any]:
    return {"log": [ask_both(state)]}


def recursive(state: Any) -> Any:
    interrupt("again?")
    return recursive(state)


def test_direct_calls_in_order() -> None:
    assert interrupt_sites(node_direct) == ["interrupt('amount?')", "interrupt('approver?')"]


def test_helpers_are_followed_where_they_are_called() -> None:
    assert interrupt_sites(node_via_helper) == ["interrupt('amount?')", "interrupt('approver?')"]
    assert interrupt_sites(node_two_deep) == ["interrupt('amount?')", "interrupt('approver?')"]


def test_closure_helpers_are_followed() -> None:
    def make() -> Any:
        def helper(state: Any) -> Any:
            return interrupt("closure?")

        def node(state: Any) -> Any:
            return helper(state)

        return node

    assert interrupt_sites(make()) == ["interrupt('closure?')"]


def test_recursion_terminates() -> None:
    assert interrupt_sites(recursive) == ["interrupt('again?')"]


def test_unreadable_source_is_none_not_a_guess() -> None:
    fn = eval("lambda state: interrupt('x')")  # noqa: S307 - no source file to read
    assert interrupt_sites(fn) is None
    assert code_digest(fn) is None


def test_digest_ignores_formatting_comments_and_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "graphlock_digest_a.py").write_text("def a(s):\n    return {'x': 1}  # comment\n")
    (tmp_path / "graphlock_digest_b.py").write_text("def b(s):\n    return {'x':1}\n")
    (tmp_path / "graphlock_digest_c.py").write_text("def a(s):\n    return {'x': 2}\n")
    monkeypatch.syspath_prepend(str(tmp_path))
    a = importlib.import_module("graphlock_digest_a").a
    b = importlib.import_module("graphlock_digest_b").b
    c = importlib.import_module("graphlock_digest_c").a
    assert code_digest(a) is not None
    assert code_digest(a) == code_digest(b)
    assert code_digest(a) != code_digest(c)


@pytest.mark.parametrize(
    ("old", "new", "change"),
    [
        (["a", "b"], ["b", "a"], "moved"),  # swapped: answers go to the wrong calls
        (["a"], ["x", "a"], "moved"),  # inserted before
        (["a", "b"], ["b"], "moved"),  # the first removed: b now receives a's answer
        (["a", "b"], ["a"], "removed"),  # the last removed: its answer is dropped
        (["a"], [], "removed"),
        (["a"], ["a", "b"], "appended"),
        (["a"], ["a"], "same"),
        (["ask(x)"], ["ask(y)"], "reworded"),  # one prompt, new wording: the answer still fits
        (["a", "b"], ["a2", "b2"], "reworded"),
    ],
)
def test_interrupt_change(old: list[str], new: list[str], change: str) -> None:
    from graphlock.check import interrupt_change

    assert interrupt_change(old, new) == change
