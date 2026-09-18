"""Scan stored threads and report the ones a new graph would break.

`graphlock check` compares shapes and needs no database. `scan` answers the next question: which of
the threads actually stored will be hit, and how. For each thread it reads what the latest
checkpoint was waiting for under the old layout, restores it under the new graph with LangGraph's own
restore and task-planning code (no node code runs), and reports every difference. It only reads.
"""

from __future__ import annotations

import contextlib
import dataclasses
import sys
import typing
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from typing import Any

import ormsgpack
from pydantic import TypeAdapter, ValidationError

from graphlock import _lg
from graphlock.findings import RULES, Finding, Severity
from graphlock.migrations import Migration, apply_migrations, graph_for_ns
from graphlock.saver import MigratingSaver
from graphlock.shape import GraphShape, extract_shape, iter_graphs, type_name

# msgpack extension codes the LangGraph serializer uses for objects stored by import path
_CLASS_EXT_CODES = range(6)
_DELTA_EXT_CODE = 7


@dataclasses.dataclass(frozen=True)
class ThreadIssue:
    code: str
    thread_id: str
    subject: str
    message: str
    ns: str = ""
    severity: Severity | None = None
    handled_by: tuple[str, ...] = ()

    @property
    def level(self) -> Severity:
        return self.severity or RULES[self.code].severity

    @property
    def blocking(self) -> bool:
        return self.level is Severity.BREAKING and not self.handled_by

    def key(self) -> tuple[str, str, str, str]:
        return (self.code, self.thread_id, self.ns, self.subject)

    def to_json(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "rule": RULES[self.code].name,
            "severity": str(self.level),
            "thread_id": self.thread_id,
            "checkpoint_ns": self.ns,
            "subject": self.subject,
            "message": self.message,
            "handled_by": list(self.handled_by),
            "blocking": self.blocking,
        }


@dataclasses.dataclass
class ScanReport:
    threads: int = 0
    paused: int = 0  # threads whose latest checkpoint has work left to do
    checkpoints: int = 0
    issues: list[ThreadIssue] = dataclasses.field(default_factory=list)
    # migration -> stored threads it still changes, split by whether the thread is paused mid-run
    migrations_needed: dict[str, dict[str, int]] = dataclasses.field(default_factory=dict)

    @property
    def blocking(self) -> list[ThreadIssue]:
        return [i for i in self.issues if i.blocking]

    @property
    def affected_threads(self) -> set[str]:
        return {i.thread_id for i in self.issues if i.level is Severity.BREAKING}

    def to_json(self) -> dict[str, Any]:
        return {
            "threads": self.threads,
            "paused": self.paused,
            "checkpoints": self.checkpoints,
            "affected_threads": len(self.affected_threads),
            "blocking": len(self.blocking),
            "issues": [i.to_json() for i in self.issues],
            "migrations_needed": self.migrations_needed,
        }


class _ClassRefRecorder:
    """Wraps a serializer and records every class a loaded blob refers to by import path."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.refs: set[tuple[str, str]] = set()

    def loads_typed(self, data: tuple[str, bytes]) -> Any:
        type_, payload = data
        if type_ == "msgpack" and isinstance(payload, (bytes, bytearray)):
            with contextlib.suppress(Exception):  # recording is best effort; the real load below decides
                ormsgpack.unpackb(payload, ext_hook=self._hook, option=ormsgpack.OPT_NON_STR_KEYS)
        return self.inner.loads_typed(data)

    def _hook(self, code: int, data: bytes) -> Any:
        if code in _CLASS_EXT_CODES or code == _DELTA_EXT_CODE:
            inner = ormsgpack.unpackb(data, ext_hook=self._hook, option=ormsgpack.OPT_NON_STR_KEYS)
            if code in _CLASS_EXT_CODES and isinstance(inner, (list, tuple)) and len(inner) >= 2:
                self.refs.add((str(inner[0]), str(inner[1])))
        return None

    def take(self) -> set[tuple[str, str]]:
        refs, self.refs = self.refs, set()
        return refs

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


def _restorable(module: str, name: str) -> bool:
    """Whether LangGraph could have rebuilt `module.name` when it loaded the checkpoint.

    Never imports anything: stored data names these modules, and scanning a store must be no more
    dangerous than resuming its threads. LangGraph's serializer imports a module when it rebuilds an
    object, so after the load a restorable class is already in `sys.modules`. One that isn't either
    failed to import or was blocked by the serializer's allowlist, and either way came back raw.
    """
    obj: Any = sys.modules.get(module)
    if obj is None:
        return False
    for part in name.split("."):
        obj = getattr(obj, part, None)
        if obj is None:
            return False
    return True


def _unwrap(saver: Any) -> Any:
    return saver.inner if isinstance(saver, MigratingSaver) else saver


def _latest_checkpoints(
    saver: Any, thread_ids: Iterable[str] | None
) -> dict[tuple[str, str], dict[str, Any]]:
    """(thread_id, checkpoint_ns) -> config of its latest checkpoint."""
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    configs: Iterable[Any] = (
        [None] if thread_ids is None else [{"configurable": {"thread_id": t}} for t in thread_ids]
    )
    for config in configs:
        for saved in saver.list(config):
            conf = saved.config["configurable"]
            key = (conf["thread_id"], conf.get("checkpoint_ns", ""))
            if key not in latest or conf["checkpoint_id"] > latest[key]["configurable"]["checkpoint_id"]:
                latest[key] = {"configurable": dict(conf)}
    return latest


def scan(
    graph: Any,
    saver: Any = None,
    *,
    migrations: Sequence[Migration] = (),
    lock: GraphShape | None = None,
    thread_ids: Iterable[str] | None = None,
) -> ScanReport:
    """Report what `graph` would do to every thread stored in `saver` (default: the graph's checkpointer)."""
    saver = _unwrap(saver if saver is not None else graph.checkpointer)
    if saver is None:
        raise ValueError("scan needs a checkpointer: pass saver= or compile the graph with one")
    locked = dict(iter_graphs(lock)) if lock is not None else {}

    report = ScanReport()
    recorder = _ClassRefRecorder(saver.serde)
    original_serde = saver.serde
    saver.serde = recorder
    try:
        latest = _latest_checkpoints(saver, thread_ids)
        report.threads = len({t for t, _ in latest})
        needed: dict[str, Counter[str]] = {m.describe(): Counter(paused=0, finished=0) for m in migrations}
        for (thread_id, ns), config in sorted(latest.items()):
            recorder.take()
            saved = saver.get_tuple(config)
            refs = recorder.take()
            if saved is None:
                continue
            report.checkpoints += 1
            sub, path = graph_for_ns(graph, ns)
            analysis = _Analysis(thread_id, ns, path, sub, locked.get(path), saver)
            raw = analysis.run(saved, refs)
            if ns == "" and analysis.pending:
                report.paused += 1
            if ns and not analysis.pending:
                continue  # a finished subgraph run: nothing will resume it
            if not migrations:
                report.issues += raw
                continue
            migrated, applied, _ = apply_migrations(saved, migrations, graph)
            for name in applied:
                needed[name]["paused" if analysis.pending else "finished"] += 1
            if not applied:
                report.issues += raw
                continue
            used = [m for m in migrations if m.describe() in applied]
            revived = {
                r for r in refs if any(m.handles(Finding("GL301", f"{r[0]}:{r[1]}", "")) for m in used)
            }
            again = _Analysis(thread_id, ns, path, sub, locked.get(path), saver)
            after = {i.key() for i in again.run(migrated, refs - revived)}
            for issue in raw:
                if issue.key() in after:
                    report.issues.append(issue)
                else:
                    report.issues.append(dataclasses.replace(issue, handled_by=_repairers(issue, used, path)))
        report.migrations_needed = {name: dict(counts) for name, counts in needed.items()}
    finally:
        saver.serde = original_serde
    report.issues.sort(
        key=lambda i: (
            {Severity.BREAKING: 0, Severity.WARNING: 1, Severity.INFO: 2}[i.level],
            i.code,
            i.thread_id,
            i.subject,
        )
    )
    return report


@dataclasses.dataclass
class _Analysis:
    """One stored checkpoint, read against the (sub)graph it belongs to."""

    thread_id: str
    ns: str
    path: str
    graph: Any  # None when the subgraph the checkpoint belongs to no longer exists
    locked: GraphShape | None
    saver: Any
    pending: bool = False
    issues: list[ThreadIssue] = dataclasses.field(default_factory=list)

    def issue(self, code: str, subject: str, message: str, severity: Severity | None = None) -> None:
        self.issues.append(ThreadIssue(code, self.thread_id, subject, message, ns=self.ns, severity=severity))

    def value_issue(self, code: str, subject: str, message: str, severity: Severity | None = None) -> None:
        """A problem with stored values: it breaks a paused thread, and a finished one only if continued."""
        if not self.pending:
            message += " The thread has finished, so this only matters if it is continued."
            if (severity or RULES[code].severity) is Severity.BREAKING:
                severity = Severity.WARNING
        self.issue(code, subject, message, severity)

    def run(self, saved: _lg.CheckpointTuple, refs: set[tuple[str, str]]) -> list[ThreadIssue]:
        self.issues = []
        ckpt: Any = saved.checkpoint
        writes = list(saved.pending_writes or [])
        step = int((saved.metadata or {}).get("step", -1)) + 1
        values: dict[str, Any] = ckpt["channel_values"]

        expected_now, expected_later = self._expected_nodes(ckpt, writes)
        expected = expected_now | expected_later
        barriers = {
            ch: (j, _barrier_seen(v))
            for ch, v in values.items()
            if (j := _lg.parse_join(ch)) is not None and _barrier_seen(v)
        }
        paused_inside = {tid for tid, ch, _ in writes if ch in (_lg.INTERRUPT, _lg.RESUME)}
        self.pending = bool(expected or barriers or paused_inside)

        if self.graph is None:
            parent = self.path.rsplit("/", 1)[-1]
            self.issue(
                "GL101",
                parent,
                f"Paused inside subgraph '{parent}', which no longer exists. The thread will resume "
                "as if finished.",
            )
            return self.issues

        for module, name in sorted(refs):
            if not _restorable(module, name):
                self.value_issue(
                    "GL301",
                    f"{module}:{name}",
                    f"Stores a {module}.{name}, which can't be imported any more or is blocked by the "
                    "serializer's allowlist; it restores as a plain dict or None.",
                )

        channels = self._restore(ckpt, saved.config)
        next_nodes: set[str] | None = None
        if channels is not None:
            try:
                next_nodes = set(
                    _lg.next_task_names(
                        ckpt, writes, self.graph, channels=channels, config=saved.config, step=step
                    ).values()
                )
            except Exception as exc:
                self.issue("GL205", "restore", f"Planning the next step fails: {_short(exc)}")

        for node in sorted(expected):
            if node not in self.graph.nodes:
                self.issue(
                    "GL101",
                    node,
                    f"Waiting to run '{node}', which no longer exists. The thread will resume as if "
                    "finished and nothing after it will run.",
                )
            elif next_nodes is not None and node in expected_now and node not in next_nodes:
                self.issue(
                    "GL102",
                    node,
                    f"Waiting to run '{node}', but its stored trigger doesn't fire under the new graph. "
                    "The thread will resume as if finished.",
                )

        for channel, ((sources, target), seen) in sorted(barriers.items()):
            if channel in self.graph.channels or target not in self.graph.nodes:
                continue
            self.issue(
                "GL103",
                channel,
                f"'{target}' is waiting on {sorted(set(sources) - seen)} after {sorted(seen)} finished, but "
                "its fan-in channel was renamed. It will never run.",
            )

        self._check_stale_fields(ckpt)
        self._check_values(values)
        self._check_interrupt_order(ckpt, writes, step)
        return self.issues

    def _check_stale_fields(self, ckpt: Any) -> None:
        """Versions of state channels the new graph doesn't have break interrupt_before breakpoints."""
        breakpoints = _breakpoints(self.graph)
        if not breakpoints:
            return
        for channel in sorted(ckpt["channel_versions"]):
            if channel in self.graph.channels or channel in (_lg.START, _lg.TASKS):
                continue
            if channel.startswith((_lg.BRANCH_PREFIX, _lg.JOIN_PREFIX)):
                continue  # renamed or removed nodes and fan-ins are reported on their own
            self.value_issue(
                "GL203",
                channel,
                f"Holds a value for removed field '{channel}'. At an interrupt_before breakpoint "
                f"({', '.join(breakpoints)}) this thread will pause again on every resume and never get "
                "past it, whether it is paused there now or reaches it later.",
            )

    def _expected_nodes(self, ckpt: Any, writes: Sequence[tuple[str, str, Any]]) -> tuple[set[str], set[str]]:
        """Nodes the stored checkpoint says should run, read from its channels alone.

        Returns (next step, a later step): pending writes of finished tasks only trigger their
        targets once the current step completes.
        """
        values, versions, seen = ckpt["channel_values"], ckpt["channel_versions"], ckpt["versions_seen"]
        nodes: set[str] = set()
        later: set[str] = set()
        for channel in values:
            if not channel.startswith(_lg.BRANCH_PREFIX):
                continue
            node = channel[len(_lg.BRANCH_PREFIX) :]
            version = versions.get(channel)
            if version is None:
                continue
            last_seen = seen.get(node, {}).get(channel)
            if last_seen is None or version > last_seen:
                nodes.add(node)
        for send in values.get(_lg.TASKS) or []:
            if isinstance(send, _lg.Send):
                nodes.add(send.node)
        for _, channel, value in writes:
            if channel.startswith(_lg.BRANCH_PREFIX):
                later.add(channel[len(_lg.BRANCH_PREFIX) :])
            elif channel == _lg.TASKS and isinstance(value, _lg.Send):
                later.add(value.node)
        return nodes, later - nodes

    def _restore(self, ckpt: Any, config: Any) -> dict[str, Any] | None:
        """Restore every channel under the new graph, one at a time so a failure names its channel."""
        restored: dict[str, Any] = {}
        failed = False
        for name, spec in self.graph.channels.items():
            if not isinstance(spec, _lg.BaseChannel):
                continue
            stored = ckpt["channel_values"].get(name, _lg.MISSING)
            try:
                channel = spec.from_checkpoint(stored)
                if stored is not _lg.MISSING:
                    channel.is_available()
                    _probe_shape(channel)
                restored[name] = channel
            except Exception as exc:
                failed = True
                join = _lg.parse_join(name)
                node = (
                    name[len(_lg.BRANCH_PREFIX) :]
                    if name.startswith(_lg.BRANCH_PREFIX)
                    else join[1]
                    if join
                    else name
                )
                code = "GL102" if name.startswith(_lg.BRANCH_PREFIX) or join else "GL205"
                self.value_issue(
                    code, node, f"Restoring channel '{name}' fails: {_short(exc)}. Resuming crashes."
                )
        if failed:
            return None
        try:
            channels, _ = _lg.channels_from_checkpoint(
                self.graph.channels, ckpt, saver=self.saver, config=config
            )
        except Exception as exc:
            self.value_issue(
                "GL205", "restore", f"Restoring the checkpoint fails: {_short(exc)}. Resuming crashes."
            )
            return None
        return dict(channels)

    def _check_values(self, values: dict[str, Any]) -> None:
        schema = self.graph.builder.state_schema
        fields = {k: v for k, v in values.items() if k in self.graph.builder.channels}
        if hasattr(schema, "model_validate"):
            try:
                schema.model_validate(fields)
            except Exception as exc:
                for error in getattr(exc, "errors", lambda: [])() or [
                    {"loc": ("state",), "type": "error", "msg": str(exc)}
                ]:
                    field = str(error["loc"][0]) if error.get("loc") else "state"
                    code = "GL201" if error.get("type") == "missing" else "GL202"
                    self.value_issue(
                        code,
                        field,
                        f"State field '{field}': {error.get('msg', 'invalid')}. "
                        "Building the state for the next node fails.",
                    )
            return
        hints = _hints(schema)
        for field, value in sorted(fields.items()):
            annotation = hints.get(field)
            if annotation is None:
                continue
            bare = _strip(annotation)
            if isinstance(value, dict) and _expects_object(bare):
                self.value_issue(
                    "GL301",
                    field,
                    f"State field '{field}' holds a plain dict where {_short_type(bare)} is expected: the "
                    "object's class was renamed or moved and LangGraph restored its fields only.",
                )
            elif not _fits_lax(value, bare):
                self.value_issue(
                    "GL202",
                    field,
                    f"State field '{field}' holds {type(value).__name__} {_short(value)}, which doesn't fit "
                    f"{_short_type(bare)}. Nodes will receive it unchanged.",
                    severity=Severity.WARNING,
                )

    def _check_interrupt_order(self, ckpt: Any, writes: Sequence[tuple[str, str, Any]], step: int) -> None:
        if self.locked is None:
            return
        waiting = {tid for tid, ch, _ in writes if ch == _lg.INTERRUPT}
        if not waiting:
            return
        new_shape = extract_shape(self.graph)
        for name, node in self.locked["nodes"].items():
            old_sites = node.get("interrupts") or []
            new_node = new_shape["nodes"].get(name)
            if not old_sites or new_node is None:
                continue
            new_sites = new_node.get("interrupts")
            if new_sites is None or new_sites[: len(old_sites)] == old_sites:
                continue
            task_id = _lg.pull_task_id(ckpt, self.ns, step, name, self.graph.nodes[name].triggers)
            if task_id in waiting:
                answered = sum(
                    len(v) if isinstance(v, list) else 1
                    for tid, ch, v in writes
                    if tid == task_id and ch == _lg.RESUME
                )
                self.issue(
                    "GL401",
                    name,
                    f"Paused inside '{name}' after {answered} answered interrupt(s); its interrupt() calls "
                    "changed order, so stored answers will go to the wrong calls.",
                )


def _breakpoints(graph: Any) -> list[str]:
    """The graph's interrupt_before breakpoints. interrupt_after never re-checks the stale channel."""
    value = getattr(graph, "interrupt_before_nodes", None) or []
    return ["*"] if value == "*" else list(value)


def _repairers(issue: ThreadIssue, used: Sequence[Migration], path: str) -> tuple[str, ...]:
    """The migrations that repair `issue`: the ones that claim it, else every one that applied."""
    finding = Finding(issue.code, issue.subject, issue.message, graph=path)
    claimed = tuple(m.describe() for m in used if m.handles(finding))
    return claimed or tuple(m.describe() for m in used)


def _barrier_seen(value: Any) -> set[str]:
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[1], bool):
        value = value[0]
    if isinstance(value, (set, frozenset, list, tuple)):
        return {v for v in value if isinstance(v, str)}
    return set()


def _probe_shape(channel: Any) -> None:
    """Touch the restored channel the way LangGraph will, so a wrong shape fails here and not later."""
    seen = getattr(channel, "seen", None)
    if seen is not None and not isinstance(seen, (set, frozenset)):
        raise TypeError(f"restored {type(channel).__name__} holds {type(seen).__name__} instead of a set")
    finished = getattr(channel, "finished", None)
    if finished is not None and not isinstance(finished, bool):
        raise TypeError(f"restored {type(channel).__name__} has finished={finished!r}")


def _hints(schema: Any) -> dict[str, Any]:
    try:
        return typing.get_type_hints(schema, include_extras=True)
    except Exception:
        return dict(getattr(schema, "__annotations__", {}))


def _strip(annotation: Any) -> Any:
    while typing.get_origin(annotation) is typing.Annotated:
        annotation = typing.get_args(annotation)[0]
    return annotation


def _expects_object(annotation: Any) -> bool:
    if isinstance(annotation, type):
        return hasattr(annotation, "model_validate") or dataclasses.is_dataclass(annotation)
    args = typing.get_args(annotation)
    return bool(args) and all(_expects_object(a) for a in args if a is not type(None))


_ADAPTERS: dict[Any, Any] = {}


def _fits_lax(value: Any, annotation: Any) -> bool:
    if annotation is Any:
        return True
    try:
        adapter = _ADAPTERS.get(annotation)
        if adapter is None:
            adapter = _ADAPTERS[annotation] = TypeAdapter(annotation)
        adapter.validate_python(value)
    except ValidationError:
        return False
    except Exception:
        return True  # no validator for this annotation: don't guess
    return True


def _short(value: Any, limit: int = 100) -> str:
    text = str(value).splitlines()[0] if str(value) else repr(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _short_type(annotation: Any) -> str:
    return type_name(annotation)


def group_issues(issues: Sequence[ThreadIssue]) -> dict[tuple[str, str, Severity], list[ThreadIssue]]:
    """Issues grouped by (code, subject, severity), for a report that lists each problem once."""
    grouped: dict[tuple[str, str, Severity], list[ThreadIssue]] = defaultdict(list)
    for issue in issues:
        grouped[(issue.code, issue.subject, issue.level)].append(issue)
    return dict(grouped)
