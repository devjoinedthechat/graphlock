"""Compare the locked shape of a graph with its new shape and report what stored threads will hit."""

from __future__ import annotations

import dataclasses
from collections.abc import Sequence

from graphlock import _lg
from graphlock.findings import Finding, Severity
from graphlock.migrations import Migration, resolve_class
from graphlock.shape import GraphShape, NodeShape

# Channel classes that store the same shape (a bare value), so swapping one for another restores fine.
_BARE_VALUE_KINDS = {"LastValue", "BinaryOperatorAggregate", "AnyValue", "UntrackedValue"}


def check(old: GraphShape, new: GraphShape, migrations: Sequence[Migration] = ()) -> list[Finding]:
    """Findings for a deploy that replaces `old` with `new`, with the ones `migrations` repair marked."""
    findings = _check_graph(old, new, "")
    out = []
    for f in findings:
        handler = next((m for m in migrations if m.handles(f)), None)
        out.append(f if handler is None else _with(f, handled_by=handler.describe()))
    return sorted(out, key=lambda f: (_order(f), f.graph, f.code, f.subject))


def _order(f: Finding) -> int:
    return {Severity.BREAKING: 0, Severity.WARNING: 1, Severity.INFO: 2}[f.level]


def _with(f: Finding, **changes: object) -> Finding:
    return dataclasses.replace(f, **changes)  # type: ignore[arg-type]


def _check_graph(old: GraphShape, new: GraphShape, path: str) -> list[Finding]:
    findings: list[Finding] = []
    findings += _nodes(old, new, path)
    findings += _joins(old, new, path)
    findings += _state(old, new, path)
    findings += _types(old, new, path)
    for name, node in old["nodes"].items():
        new_node = new["nodes"].get(name)
        old_sub, new_sub = node.get("subgraph"), (new_node or {}).get("subgraph")
        if old_sub is not None and new_sub is not None:
            findings += _check_graph(old_sub, new_sub, f"{path}/{name}" if path else name)
    return findings


def _rename_target(node: NodeShape, added: dict[str, NodeShape]) -> str | None:
    """The added node that `name` was most likely renamed to."""
    same_code = [n for n, a in added.items() if node.get("code") and a.get("code") == node.get("code")]
    if len(same_code) == 1:
        return same_code[0]
    same_kind = [
        n
        for n, a in added.items()
        if a.get("kind") == node.get("kind") and a.get("defer") == node.get("defer")
    ]
    return same_kind[0] if len(same_kind) == 1 and len(added) == 1 else None


def _nodes(old: GraphShape, new: GraphShape, path: str) -> list[Finding]:
    findings: list[Finding] = []
    added = {n: s for n, s in new["nodes"].items() if n not in old["nodes"]}
    for name, node in old["nodes"].items():
        new_node = new["nodes"].get(name)
        if new_node is None:
            target = _rename_target(node, added)
            what = "subgraph" if node.get("kind") == "subgraph" else "node"
            message = (
                f"{what.capitalize()} '{name}' is gone. Threads paused at it, or with pending work for it, "
                "will resume as if finished and it will never run."
            )
            if node.get("kind") == "subgraph":
                message += " Threads paused inside it also lose their subgraph state."
            hint = (
                f"It looks renamed to '{target}': add rename_node({name!r}, {target!r})."
                if target
                else "Drain the threads `graphlock scan` lists before deploying."
            )
            findings.append(Finding("GL101", name, message, graph=path, hint=hint))
            continue

        if node.get("defer", False) != new_node.get("defer", False):
            findings.append(_defer_finding(name, new_node, old, path))

        old_sub = node.get("kind") == "subgraph"
        new_sub = new_node.get("kind") == "subgraph"
        if old_sub != new_sub:
            findings.append(
                Finding(
                    "GL104",
                    name,
                    f"'{name}' {'was' if old_sub else 'is now'} a subgraph. Threads paused inside it "
                    "lose the subgraph's state.",
                    graph=path,
                )
            )

        findings += _interrupts(name, node, new_node, path)
    return findings


def _defer_finding(name: str, new_node: NodeShape, old: GraphShape, path: str) -> Finding:
    turned_on = new_node.get("defer", False)
    fan_in = any((join := _lg.parse_join(ch)) is not None and join[1] == name for ch in old["channels"])
    if fan_in:
        message = (
            f"defer was turned {'on' if turned_on else 'off'} for fan-in node '{name}'. Threads where "
            "some of its sources already finished crash on resume."
        )
        return Finding("GL102", name, message, graph=path)
    if turned_on:
        message = (
            f"defer was turned on for '{name}'. Threads with a pending trigger for it crash on resume "
            "with TypeError (langgraph#8629)."
        )
        return Finding("GL102", name, message, graph=path)
    message = (
        f"defer was turned off for '{name}'. Pending triggers restore fine; a thread waiting for the "
        "end of its run to start it will start it at the next step instead."
    )
    return Finding("GL102", name, message, graph=path, severity=Severity.INFO)


def _interrupts(name: str, node: NodeShape, new_node: NodeShape, path: str) -> list[Finding]:
    old_sites = node.get("interrupts")
    new_sites = new_node.get("interrupts")
    if not old_sites or new_sites is None:
        return []
    if new_sites[: len(old_sites)] != old_sites:
        return [
            Finding(
                "GL401",
                name,
                f"The interrupt() calls in '{name}' changed from {_list(old_sites)} to {_list(new_sites)}. "
                "Threads paused inside it will pass stored answers to the wrong calls.",
                graph=path,
                hint="Keep existing interrupt() calls in order and add new ones after them.",
            )
        ]
    if node.get("code") and new_node.get("code") and node.get("code") != new_node.get("code"):
        return [
            Finding(
                "GL402",
                name,
                f"'{name}' calls interrupt() and its code changed. Threads paused inside it will run "
                "the new code from the top when they resume.",
                graph=path,
            )
        ]
    return []


def _list(sites: Sequence[str]) -> str:
    return "[" + ", ".join(sites) + "]" if sites else "[]"


def _joins(old: GraphShape, new: GraphShape, path: str) -> list[Finding]:
    findings = []
    for channel in old["channels"]:
        join = _lg.parse_join(channel)
        if join is None or channel in new["channels"]:
            continue
        sources, target = join
        if target not in new["nodes"] or any(s not in new["nodes"] for s in sources):
            continue  # a removed node is reported as GL101
        same_set = [
            ch
            for ch in new["channels"]
            if (j := _lg.parse_join(ch)) is not None and j[1] == target and set(j[0]) == set(sources)
        ]
        if same_set:
            message = (
                f"The fan-in into '{target}' now lists its sources as {_lg.parse_join(same_set[0])[0]} "  # type: ignore[index]
                f"instead of {sources}. That renames its channel, so threads where some sources already "
                f"finished will never run '{target}'."
            )
            hint = f"Pass the sources in the old order: add_edge({sources!r}, {target!r})."
        else:
            message = (
                f"The sources of the fan-in into '{target}' changed from {sources}. Threads where some "
                f"of them already finished will never run '{target}'."
            )
            hint = "Drain the threads `graphlock scan` lists, or add rename_channel(old, new)."
        findings.append(Finding("GL103", channel, message, graph=path, hint=hint))
    return findings


def _state(old: GraphShape, new: GraphShape, path: str) -> list[Finding]:
    findings: list[Finding] = []
    old_fields, new_fields = old["state"]["fields"], new["state"]["fields"]
    style = new["state"]["style"]
    validates = style == "pydantic"
    constructs = style in {"pydantic", "dataclass"}

    for name, field in new_fields.items():
        before = old_fields.get(name)
        if field["required"] and constructs and (before is None or not before["required"]):
            findings.append(
                Finding(
                    "GL201",
                    name,
                    f"State field '{name}' is required and has no default. Stored threads without a value "
                    "for it fail validation at the next node.",
                    graph=path,
                    severity=Severity.BREAKING if before is None else Severity.WARNING,
                    hint=f"Give it a default, or add set_default({name!r}, ...).",
                )
            )

    breakpoints = new["interrupt_before"] + new["interrupt_after"]
    for name, field in old_fields.items():
        after = new_fields.get(name)
        if after is None:
            if breakpoints:
                message = (
                    f"State field '{name}' was removed. Threads with a stored value for it that pause at a "
                    f"breakpoint ({', '.join(breakpoints)}) will pause again on every resume and never get "
                    "past it."
                )
            else:
                message = f"State field '{name}' was removed. Stored values stay in the checkpoint unread."
            findings.append(
                Finding(
                    "GL203",
                    name,
                    message,
                    graph=path,
                    severity=None if breakpoints else Severity.INFO,
                    hint=f"Add drop_field({name!r}).",
                )
            )
            continue
        if field["type"] != after["type"]:
            findings.append(
                Finding(
                    "GL202",
                    name,
                    f"State field '{name}' changed type from {field['type']} to {after['type']}. "
                    + (
                        "Pydantic state validates stored values, so ones that no longer fit fail at the "
                        "next node."
                        if validates
                        else "Stored values are passed to nodes unchanged, in the old type."
                    ),
                    graph=path,
                    severity=Severity.BREAKING if validates else Severity.WARNING,
                    hint=f"Add convert_field({name!r}, fn) to convert stored values that don't fit.",
                )
            )

    for name, channel in old["channels"].items():
        after_ch = new["channels"].get(name)
        if after_ch is None or name not in old_fields:
            continue
        kinds = {channel["kind"], after_ch["kind"]}
        if channel["kind"] != after_ch["kind"] and not kinds <= _BARE_VALUE_KINDS:
            findings.append(
                Finding(
                    "GL205",
                    name,
                    f"State field '{name}' changed channel from {channel['kind']} to {after_ch['kind']}, "
                    "which stores a different shape.",
                    graph=path,
                )
            )
        elif channel.get("reducer") != after_ch.get("reducer"):
            before_r = channel.get("reducer") or "none (last value wins)"
            after_r = after_ch.get("reducer") or "none (last value wins)"
            findings.append(
                Finding(
                    "GL204",
                    name,
                    f"State field '{name}' changed reducer from {before_r} to {after_r}. Stored values "
                    "are kept and the next write is merged with the new reducer.",
                    graph=path,
                )
            )
    return findings


def _types(old: GraphShape, new: GraphShape, path: str) -> list[Finding]:
    findings = []
    for type_path, kind in old["types"].items():
        if type_path in new["types"] or resolve_class(type_path) is not None:
            continue
        restored = "a plain dict" if kind == "pydantic" else "None or a plain dict"
        findings.append(
            Finding(
                "GL301",
                type_path,
                f"{type_path} ({kind}) can't be imported any more. Stored objects of it will come back "
                f"as {restored}, with no error.",
                graph=path,
                hint="Keep an alias at the old import path, or add revive(old_path, NewClass).",
            )
        )
    return findings
