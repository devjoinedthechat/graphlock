"""Migrations that repair stored threads so they resume correctly under the new graph.

Each migration looks at a stored checkpoint and decides from the data alone whether it applies: a
renamed node's trigger is still under the old name, a required field is missing, a value doesn't
fit its new type. That makes every migration idempotent, so graphlock can apply them lazily on every
read (see `graphlock.saver`) with no version numbers to keep in sync. When `graphlock scan` finds no
stored thread that a migration still applies to, it is safe to delete.
"""

from __future__ import annotations

import copy
import dataclasses
import importlib
import typing
from collections.abc import Callable, Iterable, Sequence
from typing import Any

from pydantic import TypeAdapter, ValidationError

from graphlock import _lg
from graphlock.findings import Finding

__all__ = [
    "Migration",
    "MigrationContext",
    "apply_migrations",
    "convert_field",
    "defer_changed",
    "drop_field",
    "graph_for_ns",
    "rename_channel",
    "rename_field",
    "rename_node",
    "resolve_class",
    "revive",
    "set_default",
]


@dataclasses.dataclass
class MigrationContext:
    """One stored checkpoint, open for repair. Migrations edit it in place."""

    checkpoint: dict[str, Any]
    pending_writes: list[tuple[str, str, Any]]
    metadata: dict[str, Any]
    ns: str  # the checkpoint namespace: "" for the root graph, "research:<task id>" inside a subgraph
    graph_path: str  # "" for the root graph, "research" for a subgraph node, "a/b" when nested
    graph: Any  # the compiled graph (or subgraph) this checkpoint belongs to

    @property
    def step(self) -> int:
        """The step LangGraph will run next from this checkpoint (task ids depend on it)."""
        return int(self.metadata.get("step", -1)) + 1

    @property
    def values(self) -> dict[str, Any]:
        return self.checkpoint["channel_values"]  # type: ignore[no-any-return]

    @property
    def versions(self) -> dict[str, Any]:
        return self.checkpoint["channel_versions"]  # type: ignore[no-any-return]

    @property
    def seen(self) -> dict[str, dict[str, Any]]:
        return self.checkpoint["versions_seen"]  # type: ignore[no-any-return]

    def field_annotation(self, field: str) -> Any:
        schema = self.graph.builder.state_schema
        try:
            hints = typing.get_type_hints(schema, include_extras=True)
        except Exception:
            hints = getattr(schema, "__annotations__", {})
        return hints.get(field, Any)


class Migration:
    """Base class. Subclasses set `graph` ("" for the root graph) and implement `apply`."""

    graph: str = ""

    def describe(self) -> str:
        raise NotImplementedError

    def apply(self, ctx: MigrationContext) -> bool:
        """Repair `ctx` in place; return whether anything changed."""
        raise NotImplementedError

    def handles(self, finding: Finding) -> bool:  # noqa: ARG002 - subclasses use it
        """Whether this migration repairs the stored threads a `graphlock check` finding is about."""
        return False

    def __repr__(self) -> str:
        return self.describe()


def _rename_seen(ctx: MigrationContext, renames: dict[str, str]) -> None:
    for node_seen in ctx.seen.values():
        for old, new in renames.items():
            if old in node_seen:
                value = node_seen.pop(old)
                node_seen[new] = max(value, node_seen[new]) if new in node_seen else value


def _rename_updated(ctx: MigrationContext, renames: dict[str, str]) -> None:
    """The run loop plans the next step from `updated_channels` alone; stale names there stop a thread."""
    updated = ctx.checkpoint.get("updated_channels")
    if updated:
        ctx.checkpoint["updated_channels"] = sorted({renames.get(c, c) for c in updated})


def _move_channel(ctx: MigrationContext, old: str, new: str) -> None:
    if old in ctx.values:
        value = ctx.values.pop(old)
        ctx.values.setdefault(new, value)
    if old in ctx.versions:
        version = ctx.versions.pop(old)
        ctx.versions[new] = max(version, ctx.versions[new]) if new in ctx.versions else version


class rename_node(Migration):
    """A node was renamed. Moves its triggers, fan-in slots, pending Sends and pending writes."""

    def __init__(self, old: str, new: str, *, graph: str = "") -> None:
        self.old, self.new, self.graph = old, new, graph

    def describe(self) -> str:
        return f"rename_node({self.old!r}, {self.new!r})" + (f" in {self.graph!r}" if self.graph else "")

    def handles(self, finding: Finding) -> bool:
        return finding.graph == self.graph and (
            (finding.code == "GL101" and finding.subject == self.old)
            or (finding.code == "GL103" and self.old in finding.message)
        )

    def _channel_renames(self, ctx: MigrationContext) -> dict[str, str]:
        new_channels = set(ctx.graph.channels)
        renames: dict[str, str] = {}
        for channel in set(ctx.versions) | set(ctx.values):
            if channel == _lg.branch_channel(self.old):
                renames[channel] = _lg.branch_channel(self.new)
                continue
            join = _lg.parse_join(channel)
            if join is None:
                continue
            sources, target = join
            if self.old not in sources and target != self.old:
                continue
            sources = [self.new if s == self.old else s for s in sources]
            target = self.new if target == self.old else target
            candidate = _lg.join_channel(sources, target)
            if candidate not in new_channels:
                # The fan-in may also have been re-listed in another order; match it by its source set.
                for name in new_channels:
                    other = _lg.parse_join(name)
                    if other and other[1] == target and set(other[0]) == set(sources):
                        candidate = name
                        break
            renames[channel] = candidate
        return renames

    def apply(self, ctx: MigrationContext) -> bool:
        if self.new not in ctx.graph.nodes:
            return False
        renames = self._channel_renames(ctx)
        sends = ctx.values.get(_lg.TASKS) or []
        has_sends = any(isinstance(s, _lg.Send) and s.node == self.old for s in sends)
        task_ids = self._task_ids(ctx, renames, sends)
        touched_writes = any(
            tid in task_ids or _swap_node(ch, self.old, self.new) != ch for tid, ch, _ in ctx.pending_writes
        )
        if not renames and self.old not in ctx.seen and not has_sends and not touched_writes:
            return False

        # Barrier values name the sources they have seen.
        for old_channel, new_channel in renames.items():
            if old_channel in ctx.values and old_channel.startswith(_lg.JOIN_PREFIX):
                ctx.values[old_channel] = _rename_in_barrier(ctx.values[old_channel], self.old, self.new)
            _move_channel(ctx, old_channel, new_channel)
        if self.old in ctx.seen:
            seen = ctx.seen.pop(self.old)
            ctx.seen.setdefault(self.new, {}).update(seen)
        _rename_seen(ctx, renames)
        _rename_updated(ctx, renames)
        if has_sends:
            ctx.values[_lg.TASKS] = [_rename_send(s, self.old, self.new) for s in sends]

        writes: list[tuple[str, str, Any]] = []
        for tid, channel, value in ctx.pending_writes:
            new_tid = task_ids.get(tid, tid)
            new_channel = renames.get(channel) or _swap_node(channel, self.old, self.new)
            new_value = value
            if channel == _lg.INTERRUPT and tid in task_ids:
                new_value = [_reid_interrupt(i, ctx.ns, self.new, new_tid) for i in value]
            elif channel == _lg.TASKS:
                new_value = _rename_send(value, self.old, self.new)
            elif new_channel.startswith(_lg.JOIN_PREFIX) and value == self.old:
                new_value = self.new  # a source announces itself to a fan-in barrier by name
            writes.append((new_tid, new_channel, new_value))
        ctx.pending_writes[:] = writes
        return True

    def _task_ids(
        self, ctx: MigrationContext, renames: dict[str, str], sends: Sequence[Any]
    ) -> dict[str, str]:
        """Old task id -> new task id for every task LangGraph derives from the node's name."""
        ids: dict[str, str] = {}
        ckpt = ctx.checkpoint
        new_triggers = list(ctx.graph.nodes[self.new].triggers)
        back = {v: k for k, v in renames.items()}
        old_triggers = [back.get(t) or _swap_node(t, self.new, self.old) for t in new_triggers]
        old_id = _lg.pull_task_id(ckpt, ctx.ns, ctx.step, self.old, old_triggers)  # type: ignore[arg-type]
        ids[old_id] = _lg.pull_task_id(ckpt, ctx.ns, ctx.step, self.new, new_triggers)  # type: ignore[arg-type]
        for index, send in enumerate(sends):
            if isinstance(send, _lg.Send) and send.node == self.old:
                old_push = _lg.push_task_id(ckpt, ctx.ns, ctx.step, self.old, index)  # type: ignore[arg-type]
                ids[old_push] = _lg.push_task_id(ckpt, ctx.ns, ctx.step, self.new, index)  # type: ignore[arg-type]
        return ids


def _swap_node(channel: str, old: str, new: str) -> str:
    """The name `channel` would have if node `old` were called `new`."""
    if channel == _lg.branch_channel(old):
        return _lg.branch_channel(new)
    join = _lg.parse_join(channel)
    if join is None:
        return channel
    sources, target = join
    return _lg.join_channel([new if s == old else s for s in sources], new if target == old else target)


def _rename_send(value: Any, old: str, new: str) -> Any:
    if isinstance(value, _lg.Send) and value.node == old:
        return _lg.Send(new, value.arg)
    return value


def _rename_in_barrier(value: Any, old: str, new: str) -> Any:
    def swap(names: Iterable[str]) -> set[str]:
        return {new if n == old else n for n in names}

    if _is_after_finish_pair(value):
        return (swap(value[0]), value[1])
    if isinstance(value, (set, frozenset, list, tuple)):
        return swap(value)
    return value


def _reid_interrupt(value: Any, ns: str, node: str, task_id: str) -> Any:
    if isinstance(value, _lg.Interrupt):
        return _lg.Interrupt(value=value.value, id=_lg.interrupt_id(ns, node, task_id))
    return value


def _is_after_finish_pair(value: Any) -> bool:
    """`(value, finished)` as stored by the *AfterFinish channels (a list after some serializers)."""
    return isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[1], bool)


class rename_channel(Migration):
    """A channel was renamed: a state field, or a fan-in whose sources were re-listed."""

    def __init__(self, old: str, new: str, *, graph: str = "") -> None:
        self.old, self.new, self.graph = old, new, graph

    def describe(self) -> str:
        return f"rename_channel({self.old!r}, {self.new!r})" + (f" in {self.graph!r}" if self.graph else "")

    def handles(self, finding: Finding) -> bool:
        return (
            finding.graph == self.graph and finding.subject == self.old and finding.code in {"GL103", "GL203"}
        )

    def apply(self, ctx: MigrationContext) -> bool:
        if (
            self.old not in ctx.values
            and self.old not in ctx.versions
            and not any(ch == self.old for _, ch, _ in ctx.pending_writes)
        ):
            return False
        _move_channel(ctx, self.old, self.new)
        _rename_seen(ctx, {self.old: self.new})
        _rename_updated(ctx, {self.old: self.new})
        ctx.pending_writes[:] = [
            (tid, self.new if ch == self.old else ch, v) for tid, ch, v in ctx.pending_writes
        ]
        return True


class rename_field(rename_channel):
    """A state field was renamed."""

    def describe(self) -> str:
        return f"rename_field({self.old!r}, {self.new!r})" + (f" in {self.graph!r}" if self.graph else "")


class drop_field(Migration):
    """A state field was removed. Drops its stored value and version so LangGraph stops tracking it."""

    def __init__(self, field: str, *, graph: str = "") -> None:
        self.field, self.graph = field, graph

    def describe(self) -> str:
        return f"drop_field({self.field!r})" + (f" in {self.graph!r}" if self.graph else "")

    def handles(self, finding: Finding) -> bool:
        return finding.graph == self.graph and finding.code == "GL203" and finding.subject == self.field

    def apply(self, ctx: MigrationContext) -> bool:
        if self.field in ctx.graph.channels:
            return False  # the field is back (or never left): its values are live again
        if self.field not in ctx.values and self.field not in ctx.versions:
            return False
        ctx.values.pop(self.field, None)
        ctx.versions.pop(self.field, None)
        for node_seen in ctx.seen.values():
            node_seen.pop(self.field, None)
        updated = ctx.checkpoint.get("updated_channels")
        if updated and self.field in updated:
            ctx.checkpoint["updated_channels"] = [c for c in updated if c != self.field]
        return True


class set_default(Migration):
    """Give stored threads a value for a field they don't have (e.g. a new required field)."""

    def __init__(self, field: str, value: Any, *, graph: str = "") -> None:
        self.field, self.value, self.graph = field, value, graph

    def describe(self) -> str:
        return f"set_default({self.field!r}, {self.value!r})" + (f" in {self.graph!r}" if self.graph else "")

    def handles(self, finding: Finding) -> bool:
        return finding.graph == self.graph and finding.code == "GL201" and finding.subject == self.field

    def apply(self, ctx: MigrationContext) -> bool:
        if self.field in ctx.values or not ctx.versions:
            return False
        ctx.values[self.field] = copy.deepcopy(self.value)
        if self.field not in ctx.versions:
            # A real version, so the checkpointer can store it and later writes can bump it.
            ctx.versions[self.field] = max(ctx.versions.values())
        return True


def _fits(value: Any, annotation: Any) -> bool:
    if annotation is Any:
        return True
    try:
        TypeAdapter(annotation).validate_python(value, strict=True)
    except ValidationError:
        return False
    except Exception:
        return True  # an annotation pydantic can't build a validator for: don't guess
    return True


class convert_field(Migration):
    """Convert stored values of a field that no longer fit its type.

    By default it converts only the values that fail strict validation against the field's current
    annotation, so values written by the new code are left alone. Pass `when=` to decide yourself.
    """

    def __init__(
        self,
        field: str,
        fn: Callable[[Any], Any],
        *,
        when: Callable[[Any], bool] | None = None,
        graph: str = "",
    ) -> None:
        self.field, self.fn, self.when, self.graph = field, fn, when, graph

    def describe(self) -> str:
        name = getattr(self.fn, "__name__", "fn")
        return f"convert_field({self.field!r}, {name})" + (f" in {self.graph!r}" if self.graph else "")

    def handles(self, finding: Finding) -> bool:
        return (
            finding.graph == self.graph
            and finding.code in {"GL202", "GL205"}
            and finding.subject == self.field
        )

    def apply(self, ctx: MigrationContext) -> bool:
        if self.field not in ctx.values:
            return False
        value = ctx.values[self.field]
        needed = self.when(value) if self.when else not _fits(value, _strip(ctx.field_annotation(self.field)))
        if not needed:
            return False
        ctx.values[self.field] = self.fn(value)
        return True


def _strip(annotation: Any) -> Any:
    """Drop `Annotated[...]` metadata (reducers) so pydantic validates the bare type."""
    if typing.get_origin(annotation) is typing.Annotated:
        return typing.get_args(annotation)[0]
    return annotation


def resolve_class(path: str) -> type | None:
    """The class at an import path (`"pkg.mod:Outer.Inner"` or `"pkg.mod.Name"`), or None if it's gone."""
    if ":" in path:
        module, qualname = path.split(":", 1)
    else:
        module, _, qualname = path.rpartition(".")
    try:
        obj: Any = importlib.import_module(module)
    except Exception:
        return None
    for part in qualname.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return None
    return obj if isinstance(obj, type) else None


class revive(Migration):
    """Objects of a renamed or moved class came back as plain dicts; rebuild them as `new`.

    `old` is the class's former import path (`"app.models:Order"` or `"app.models.Order"`). With
    `fields`, only those fields are touched; otherwise every field whose annotation mentions `new`.
    Objects that LangGraph restored as None cannot be rebuilt: their data was not kept.
    """

    def __init__(self, old: str, new: type, *, fields: Sequence[str] | None = None, graph: str = "") -> None:
        self.old, self.new, self.fields, self.graph = old, new, list(fields) if fields else None, graph

    def describe(self) -> str:
        return f"revive({self.old!r}, {self.new.__qualname__})" + (
            f" in {self.graph!r}" if self.graph else ""
        )

    def handles(self, finding: Finding) -> bool:
        return finding.code == "GL301" and _same_path(finding.subject, self.old)

    def apply(self, ctx: MigrationContext) -> bool:
        changed = False
        for field in self._fields(ctx):
            if field in ctx.values:
                value = ctx.values[field]
                revived = self._revive(value)
                if revived is not value:
                    ctx.values[field] = revived
                    changed = True
        return changed

    def _fields(self, ctx: MigrationContext) -> list[str]:
        if self.fields is not None:
            return self.fields
        schema = ctx.graph.builder.state_schema
        try:
            hints = typing.get_type_hints(schema, include_extras=True)
        except Exception:
            hints = getattr(schema, "__annotations__", {})
        return [name for name, hint in hints.items() if _mentions(hint, self.new)]

    def _revive(self, value: Any) -> Any:
        if isinstance(value, dict):
            return _construct(self.new, value)
        if isinstance(value, list) and any(isinstance(v, dict) for v in value):
            return [_construct(self.new, v) if isinstance(v, dict) else v for v in value]
        return value


def _construct(cls: type, data: dict[str, Any]) -> Any:
    validate = getattr(cls, "model_validate", None)
    if callable(validate):
        return validate(data)
    return cls(**data)


def _mentions(annotation: Any, cls: type) -> bool:
    if annotation is cls:
        return True
    return any(_mentions(arg, cls) for arg in typing.get_args(annotation))


def _same_path(a: str, b: str) -> bool:
    return a.replace(":", ".") == b.replace(":", ".")


class defer_changed(Migration):
    """`defer=` was toggled on a node. Converts its stored trigger values to the new channel class."""

    def __init__(self, node: str, *, graph: str = "") -> None:
        self.node, self.graph = node, graph

    def describe(self) -> str:
        return f"defer_changed({self.node!r})" + (f" in {self.graph!r}" if self.graph else "")

    def handles(self, finding: Finding) -> bool:
        return finding.graph == self.graph and finding.code == "GL102" and finding.subject == self.node

    def apply(self, ctx: MigrationContext) -> bool:
        changed = False
        for name, channel in ctx.graph.channels.items():
            if name not in ctx.values:
                continue
            join = _lg.parse_join(name)
            if name != _lg.branch_channel(self.node) and not (join and join[1] == self.node):
                continue
            value = ctx.values[name]
            fixed = _fit_channel_shape(channel, value)
            if fixed is not value:
                ctx.values[name] = fixed
                changed = True
        return changed


def _fit_channel_shape(channel: Any, value: Any) -> Any:
    if isinstance(channel, _lg.LastValueAfterFinish):
        # A bare value is a trigger that was pending when the node was not deferred: ready to run.
        return value if _is_after_finish_pair(value) else (value, True)
    if isinstance(channel, _lg.EphemeralValue):
        return value[0] if _is_after_finish_pair(value) else value
    if isinstance(channel, _lg.NamedBarrierValueAfterFinish):
        return value if _is_after_finish_pair(value) else (set(value), False)
    if isinstance(channel, _lg.NamedBarrierValue):
        return set(value[0]) if _is_after_finish_pair(value) else value
    return value


def graph_for_ns(root: Any, ns: str) -> tuple[Any | None, str]:
    """The (sub)graph a checkpoint namespace belongs to, and its path ("" for the root)."""
    if not ns:
        return root, ""
    graph = root
    names: list[str] = []
    for segment in ns.split(_lg.NS_SEP):
        name = segment.split(_lg.NS_END, 1)[0]
        names.append(name)
        subgraphs = dict(graph.get_subgraphs()) if graph is not None else {}
        graph = subgraphs.get(name)
        if graph is None:
            return None, "/".join(names)
    return graph, "/".join(names)


class Migrated(typing.NamedTuple):
    saved: Any  # the repaired CheckpointTuple (the original when nothing applied)
    applied: list[str]  # the migrations that changed something
    changed: set[str]  # channels whose stored value or version the migrations changed


def apply_migrations(saved: _lg.CheckpointTuple, migrations: Sequence[Migration], root: Any) -> Migrated:
    """`saved` repaired by every migration that applies to it."""
    if not migrations:
        return Migrated(saved, [], set())
    ns = saved.config.get("configurable", {}).get("checkpoint_ns", "")
    graph, path = graph_for_ns(root, ns)
    if graph is None:
        return Migrated(saved, [], set())
    ctx = MigrationContext(
        checkpoint=_copy_checkpoint(saved.checkpoint),
        pending_writes=list(saved.pending_writes or []),
        metadata=dict(saved.metadata or {}),
        ns=ns,
        graph_path=path,
        graph=graph,
    )
    applied = [m.describe() for m in migrations if m.graph == path and m.apply(ctx)]
    if not applied:
        return Migrated(saved, [], set())
    before_values = saved.checkpoint["channel_values"]
    before_versions = saved.checkpoint["channel_versions"]
    changed = {
        k
        for k, v in ctx.values.items()
        if k not in before_values
        or before_values[k] is not v
        or ctx.versions.get(k) != before_versions.get(k)
    }
    migrated = _lg.CheckpointTuple(
        config=saved.config,
        checkpoint=ctx.checkpoint,  # type: ignore[arg-type]
        metadata=saved.metadata,
        parent_config=saved.parent_config,
        pending_writes=ctx.pending_writes,
    )
    return Migrated(migrated, applied, changed)


def _copy_checkpoint(checkpoint: Any) -> dict[str, Any]:
    out = dict(checkpoint)
    out["channel_values"] = dict(checkpoint["channel_values"])
    out["channel_versions"] = dict(checkpoint["channel_versions"])
    out["versions_seen"] = {k: dict(v) for k, v in checkpoint["versions_seen"].items()}
    return out
