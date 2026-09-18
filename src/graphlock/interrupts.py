"""Static facts about a node's code: where it calls `interrupt()`, and a digest of what it does.

LangGraph resumes a node by running it again from the top and handing the stored answers to its
`interrupt()` calls in order. Two things about a node's code therefore matter to a paused thread:
the ordered list of `interrupt()` calls, and whether the code changed at all.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import textwrap
from collections.abc import Callable
from typing import Any

_MAX_SITE = 120


def node_function(runnable: Any) -> Callable[..., Any] | None:
    """The user function behind a LangGraph node, if there is one."""
    for attr in ("func", "afunc"):
        fn = getattr(runnable, attr, None)
        if callable(fn) and hasattr(fn, "__code__"):
            return fn  # type: ignore[no-any-return]
    if inspect.isfunction(runnable):
        return runnable
    return None


def _parse(fn: Callable[..., Any]) -> ast.AST | None:
    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except (OSError, TypeError):
        return None
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # getsource() returns whole lines; a lambda in the middle of a multi-line call does not parse.
        return None
    code = fn.__code__
    if fn.__name__ != "<lambda>":
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == fn.__name__:
                return node
        return None
    first_line = code.co_firstlineno - _first_line(fn) + 1
    lambdas = [n for n in ast.walk(tree) if isinstance(n, ast.Lambda) and n.lineno == first_line]
    if len(lambdas) == 1:
        return lambdas[0]
    if len(lambdas) > 1 and hasattr(code, "co_positions"):
        # Several lambdas on one line: pick the one whose body starts where this code object does.
        cols = [p[2] for p in code.co_positions() if p[0] == code.co_firstlineno and p[2] is not None]
        if cols:
            start = min(cols)
            indent = _indent(fn)
            for lam in lambdas:
                if lam.body.col_offset + indent <= start <= (lam.body.end_col_offset or 0) + indent:
                    return lam
    return None


def _first_line(fn: Callable[..., Any]) -> int:
    try:
        return inspect.getsourcelines(fn)[1]
    except (OSError, TypeError):
        return fn.__code__.co_firstlineno


def _indent(fn: Callable[..., Any]) -> int:
    try:
        line = inspect.getsourcelines(fn)[0][0]
    except (OSError, TypeError):
        return 0
    return len(line) - len(line.lstrip())


def _is_interrupt_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    return (isinstance(func, ast.Name) and func.id == "interrupt") or (
        isinstance(func, ast.Attribute) and func.attr == "interrupt"
    )


# How many calls deep to follow a node into the helpers it calls. Real nodes often wrap one helper
# (an approval step shared by several nodes); deeper chains are rare, and each level costs a parse.
MAX_HELPER_DEPTH = 2


def interrupt_sites(fn: Callable[..., Any] | None) -> list[str] | None:
    """The `interrupt(...)` calls `fn` makes, in the order it makes them. None when unreadable.

    Calls to plain functions the node can see (module globals and closure variables) are followed
    up to `MAX_HELPER_DEPTH` deep, and their `interrupt()` calls are listed where the helper is
    called: a node that delegates its approval to a helper still has that approval's position.
    """
    if fn is None:
        return None
    return _sites(fn, depth=0, seen=set())


def _sites(fn: Callable[..., Any], *, depth: int, seen: set[int]) -> list[str] | None:
    tree = _parse(fn)
    if tree is None:
        return None
    seen = seen | {id(fn.__code__)}
    calls = sorted(
        (n for n in ast.walk(tree) if isinstance(n, ast.Call)),
        key=lambda n: (n.lineno, n.col_offset),
    )
    sites: list[str] = []
    for call in calls:
        if _is_interrupt_call(call):
            sites.append(_site(call))
            continue
        if depth >= MAX_HELPER_DEPTH or not isinstance(call.func, ast.Name):
            continue
        helper = _visible(fn, call.func.id)
        if helper is None or id(helper.__code__) in seen:
            continue
        inner = _sites(helper, depth=depth + 1, seen=seen)
        if inner:
            sites.extend(inner)
    return sites


def _visible(fn: Callable[..., Any], name: str) -> Callable[..., Any] | None:
    """The plain Python function `name` refers to inside `fn`, if any: a closure variable or a global."""
    code = fn.__code__
    if fn.__closure__ and name in code.co_freevars:
        try:
            value = fn.__closure__[code.co_freevars.index(name)].cell_contents
        except ValueError:  # an empty cell
            return None
    else:
        value = getattr(fn, "__globals__", {}).get(name)
    return value if inspect.isfunction(value) else None


def _site(call: ast.AST) -> str:
    text = ast.unparse(call)
    return text if len(text) <= _MAX_SITE else text[: _MAX_SITE - 1] + "…"


def code_digest(fn: Callable[..., Any] | None) -> str | None:
    """A digest of what `fn` does, blind to formatting, comments and its own name."""
    if fn is None:
        return None
    tree = _parse(fn)
    if tree is None:
        return None
    if isinstance(tree, (ast.FunctionDef, ast.AsyncFunctionDef)):
        # The body and arguments, not the name or decorators: renaming a function changes nothing.
        payload = ast.dump(ast.Module(body=tree.body, type_ignores=[])) + ast.dump(tree.args)
    else:
        payload = ast.dump(tree)
    return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()[:16]
