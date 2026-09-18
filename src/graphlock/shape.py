"""The shape of a compiled LangGraph graph: everything about it that a stored thread depends on.

A shape is plain JSON so it can be committed as a lockfile and diffed in review. It records
- nodes: whether each is deferred, which channels trigger it, its `interrupt()` calls and a code digest;
- channels: the kind of every state, branch and join channel, with its type and reducer;
- state: the state schema's fields, and whether each is required;
- types: the classes reachable from the state annotations, which checkpoints store by import path;
- the static breakpoints (`interrupt_before` / `interrupt_after`);
- subgraphs, recursively.
"""

from __future__ import annotations

import dataclasses
import enum
import types as pytypes
import typing
from collections.abc import Callable, Iterator
from typing import Any, TypedDict

import typing_extensions

from graphlock import _lg
from graphlock.interrupts import code_digest, interrupt_sites, node_function

SHAPE_VERSION = 1

# Channels every graph has; their details follow from the rest of the shape.
_SKIP_CHANNELS = {_lg.START, _lg.TASKS}


class NodeShape(TypedDict, total=False):
    kind: str  # "function" | "subgraph" | "runnable"
    defer: bool
    triggers: list[str]
    interrupts: list[str] | None
    code: str | None
    subgraph: GraphShape


class ChannelShape(TypedDict, total=False):
    kind: str
    type: str
    reducer: str
    names: list[str]


class FieldShape(TypedDict):
    type: str
    required: bool


class StateShape(TypedDict):
    schema: str
    style: str  # "typeddict" | "pydantic" | "dataclass" | "other"
    fields: dict[str, FieldShape]


class GraphShape(TypedDict):
    nodes: dict[str, NodeShape]
    channels: dict[str, ChannelShape]
    state: StateShape
    types: dict[str, str]
    interrupt_before: list[str]
    interrupt_after: list[str]


class UnsupportedGraph(TypeError):
    """The object is not a compiled `StateGraph`."""


def extract_shape(graph: Any) -> GraphShape:
    """The shape of a compiled `StateGraph` (a `StateGraph` builder is compiled first)."""
    graph = _compiled(graph)
    builder = graph.builder
    subgraphs = dict(graph.get_subgraphs())

    nodes: dict[str, NodeShape] = {}
    for name, spec in sorted(builder.nodes.items()):
        runnable = spec.runnable
        node: NodeShape = {
            "defer": bool(getattr(spec, "defer", False)),
            "triggers": sorted(graph.nodes[name].triggers),
        }
        if name in subgraphs:
            node["kind"] = "subgraph"
            node["subgraph"] = extract_shape(subgraphs[name])
            fn = node_function(runnable)
            if fn is not None:  # a function that invokes a subgraph
                node["interrupts"] = interrupt_sites(fn)
                node["code"] = code_digest(fn)
        else:
            fn = node_function(runnable)
            node["kind"] = "function" if fn is not None else "runnable"
            node["interrupts"] = interrupt_sites(fn)
            node["code"] = code_digest(fn)
        nodes[name] = node

    channels: dict[str, ChannelShape] = {}
    for name, channel in sorted(graph.channels.items()):
        if name in _SKIP_CHANNELS or not isinstance(channel, _lg.BaseChannel):
            continue
        channels[name] = _channel_shape(channel, is_state=name in builder.channels)

    state_schema = builder.state_schema
    return {
        "nodes": nodes,
        "channels": channels,
        "state": _state_shape(state_schema),
        "types": dict(sorted(_reachable_types(state_schema).items())),
        "interrupt_before": _breakpoints(graph.interrupt_before_nodes),
        "interrupt_after": _breakpoints(graph.interrupt_after_nodes),
    }


def _compiled(graph: Any) -> Any:
    if hasattr(graph, "builder") and hasattr(graph, "channels"):
        return graph
    if hasattr(graph, "compile") and hasattr(graph, "nodes") and hasattr(graph, "state_schema"):
        return graph.compile()
    raise UnsupportedGraph(
        f"graphlock needs a compiled StateGraph; got {type(graph).__module__}.{type(graph).__qualname__}. "
        "Functional-API (@entrypoint) graphs are not supported yet."
    )


def _breakpoints(value: Any) -> list[str]:
    if value == "*":
        return ["*"]
    return sorted(value or [])


def _channel_shape(channel: Any, *, is_state: bool) -> ChannelShape:
    shape: ChannelShape = {"kind": type(channel).__name__}
    if is_state:
        shape["type"] = type_name(channel.typ)
    if isinstance(channel, _lg.BinaryOperatorAggregate):
        shape["reducer"] = callable_name(channel.operator)
    names = getattr(channel, "names", None)
    if isinstance(names, (set, frozenset)):
        shape["names"] = sorted(names)
    return shape


def callable_name(fn: Callable[..., Any]) -> str:
    module = getattr(fn, "__module__", None) or ""
    qualname = getattr(fn, "__qualname__", None) or getattr(fn, "__name__", None) or repr(fn)
    if module in ("_operator", "operator"):
        module = "operator"
    return f"{module}.{qualname}" if module else qualname


def class_path(cls: type) -> str:
    """How checkpoints refer to a class: `module:QualName`."""
    return f"{cls.__module__}:{cls.__qualname__}"


# Required[...], NotRequired[...] and ReadOnly[...] say nothing about what a checkpoint stores.
_WRAPPERS = tuple(
    getattr(module, name)
    for module in (typing, typing_extensions)
    for name in ("Required", "NotRequired", "ReadOnly")
    if hasattr(module, name)
)


def type_name(tp: Any) -> str:
    """A stable, readable spelling of a type annotation (reducers and other metadata dropped)."""
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is typing.Annotated:
        return type_name(args[0])
    if tp is type(None) or tp is None:
        return "None"
    if tp is Any:
        return "Any"
    if origin is typing.Union or origin is pytypes.UnionType:
        return " | ".join(sorted(type_name(a) for a in args))
    if origin is typing.Literal:
        return f"Literal[{', '.join(repr(a) for a in args)}]"
    if origin in _WRAPPERS:
        return type_name(args[0])
    if origin is not None:
        base = _plain_name(origin)
        return f"{base}[{', '.join(type_name(a) for a in args)}]" if args else base
    if isinstance(tp, typing.ForwardRef):
        return tp.__forward_arg__
    if isinstance(tp, typing.TypeVar):
        return tp.__name__
    if isinstance(tp, str):
        return tp
    return _plain_name(tp)


def _plain_name(tp: Any) -> str:
    if isinstance(tp, type):
        if tp.__module__ in ("builtins", "typing", "collections.abc"):
            return tp.__qualname__
        return f"{tp.__module__}.{tp.__qualname__}"
    name = getattr(tp, "_name", None) or getattr(tp, "__name__", None)
    return str(name) if name else repr(tp)


def _hints(schema: Any) -> dict[str, Any]:
    try:
        return typing.get_type_hints(schema, include_extras=True)
    except Exception:
        return dict(getattr(schema, "__annotations__", {}))


def _style(schema: Any) -> str:
    if _is_pydantic(schema):
        return "pydantic"
    if dataclasses.is_dataclass(schema):
        return "dataclass"
    if isinstance(schema, type) and hasattr(schema, "__required_keys__"):
        return "typeddict"
    return "other"


def _is_pydantic(obj: Any) -> bool:
    return isinstance(obj, type) and hasattr(obj, "model_fields") and hasattr(obj, "model_validate")


def _state_shape(schema: Any) -> StateShape:
    style = _style(schema)
    hints = _hints(schema)
    fields: dict[str, FieldShape] = {}
    if style == "pydantic":
        model_fields = schema.model_fields
        for name in sorted(model_fields):
            fields[name] = {
                "type": type_name(hints.get(name, Any)),
                "required": model_fields[name].is_required(),
            }
    elif style == "dataclass":
        for f in dataclasses.fields(schema):
            required = f.default is dataclasses.MISSING and f.default_factory is dataclasses.MISSING
            fields[f.name] = {"type": type_name(hints.get(f.name, Any)), "required": required}
        fields = dict(sorted(fields.items()))
    else:
        required_keys: frozenset[str] = getattr(schema, "__required_keys__", frozenset())
        for name in sorted(hints):
            fields[name] = {"type": type_name(hints[name]), "required": name in required_keys}
    return {
        "schema": class_path(schema) if isinstance(schema, type) else repr(schema),
        "style": style,
        "fields": fields,
    }


_BUILTIN_MODULES = {"builtins", "typing", "typing_extensions", "collections", "collections.abc", "types"}


def _reachable_types(schema: Any) -> dict[str, str]:
    """Classes a checkpoint of this state can hold, keyed by import path, with their kind."""
    found: dict[str, str] = {}
    seen: set[int] = set()

    def visit(tp: Any) -> None:
        if id(tp) in seen:
            return
        seen.add(id(tp))
        for arg in typing.get_args(tp):
            visit(arg)
        origin = typing.get_origin(tp)
        if origin is not None:
            visit(origin)
            return
        if not isinstance(tp, type) or tp.__module__ in _BUILTIN_MODULES:
            return
        if tp.__module__.startswith("langgraph."):
            return
        found[class_path(tp)] = _kind(tp)
        if _is_pydantic(tp) or dataclasses.is_dataclass(tp):
            for hint in _hints(tp).values():
                visit(hint)

    for hint in _hints(schema).values():
        visit(hint)
    return found


def _kind(cls: type) -> str:
    if _is_pydantic(cls):
        return "pydantic"
    if dataclasses.is_dataclass(cls):
        return "dataclass"
    if issubclass(cls, enum.Enum):
        return "enum"
    return "class"


def iter_graphs(shape: GraphShape, prefix: str = "") -> Iterator[tuple[str, GraphShape]]:
    """The shape and every subgraph shape in it, each with its path (`""`, `"research"`, ...)."""
    yield prefix, shape
    for name, node in shape["nodes"].items():
        sub = node.get("subgraph")
        if sub is not None:
            yield from iter_graphs(sub, f"{prefix}/{name}" if prefix else name)
