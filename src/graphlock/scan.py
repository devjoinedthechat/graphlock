"""Scan stored threads and report the ones a new graph would break.

`graphlock check` compares shapes and needs no database. `scan` answers the next question: which of
the threads actually stored will be hit, and how. For each thread it reads what the latest
checkpoint was waiting for under the old layout, restores it under the new graph with LangGraph's own
restore and task-planning code (no node code runs), and reports every difference. It only reads.
"""

from __future__ import annotations

import contextlib
import dataclasses
import random
import sys
import typing
from collections import Counter, defaultdict
from collections.abc import Callable, Iterable, Sequence
from typing import Any

import ormsgpack
from pydantic import TypeAdapter, ValidationError

from graphlock import _lg, _stores
from graphlock.check import interrupt_change
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
    stored_threads: int = 0  # threads that matched the filters, before sampling
    foreign: int = 0  # threads skipped as another graph's: they mention none of its nodes, old or new
    unrelated: int = 0  # without a lockfile: threads that mention none of the graph's current nodes
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
            "stored_threads": self.stored_threads,
            "foreign": self.foreign,
            "unrelated": self.unrelated,
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


@dataclasses.dataclass(frozen=True)
class ThreadFilter:
    """Which stored threads belong to the graph being scanned.

    A store often holds the threads of several graphs. `where` matches checkpoint metadata, which
    includes the `metadata` of the run config (`{"graph": "refunds"}`); `prefix` matches thread ids.
    """

    thread_ids: tuple[str, ...] | None = None
    prefix: str | None = None
    where: dict[str, Any] | None = None

    def configs(self) -> list[dict[str, Any] | None]:
        if self.thread_ids is None:
            return [None]
        return [{"configurable": {"thread_id": t}} for t in self.thread_ids]

    def keeps(self, thread_id: str) -> bool:
        return self.prefix is None or thread_id.startswith(self.prefix)


def _track_latest(latest: dict[tuple[str, str], dict[str, Any]], saved: Any, filt: ThreadFilter) -> None:
    conf = saved.config["configurable"]
    if not filt.keeps(conf["thread_id"]):
        return
    key = (conf["thread_id"], conf.get("checkpoint_ns", ""))
    if key not in latest or conf["checkpoint_id"] > latest[key]["configurable"]["checkpoint_id"]:
        latest[key] = {"configurable": dict(conf)}


def _latest_checkpoints(saver: Any, filt: ThreadFilter) -> dict[tuple[str, str], dict[str, Any]]:
    """(thread_id, checkpoint_ns) -> config of its latest checkpoint."""
    fast = latest_from_store(saver, filt)
    if fast is not None:
        return fast
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for config in filt.configs():
        for saved in saver.list(config, filter=filt.where):
            _track_latest(latest, saved, filt)
    return latest


async def _alatest_checkpoints(saver: Any, filt: ThreadFilter) -> dict[tuple[str, str], dict[str, Any]]:
    latest: dict[tuple[str, str], dict[str, Any]] = {}
    for config in filt.configs():
        async for saved in saver.alist(config, filter=filt.where):
            _track_latest(latest, saved, filt)
    return latest


def latest_from_store(saver: Any, filt: ThreadFilter) -> dict[tuple[str, str], dict[str, Any]] | None:
    """A faster way to find each thread's latest checkpoint, for stores that offer one; else None."""
    return _stores.latest_checkpoints(saver, filt)


def _nodes_referenced(ckpt: Any) -> set[str]:
    """The node names a stored checkpoint mentions: nodes that ran, and nodes it is waiting on."""
    names = {n for n in ckpt["versions_seen"] if not n.startswith("__")}
    for channel in set(ckpt["channel_versions"]) | set(ckpt["channel_values"]):
        if channel.startswith(_lg.BRANCH_PREFIX):
            names.add(channel[len(_lg.BRANCH_PREFIX) :])
        elif (join := _lg.parse_join(channel)) is not None:
            names.update(join[0])
            names.add(join[1])
    for send in ckpt["channel_values"].get(_lg.TASKS) or []:
        if isinstance(send, _lg.Send):
            names.add(send.node)
    return names


def _sampled(
    latest: dict[tuple[str, str], dict[str, Any]], sample: int | None, seed: int
) -> dict[tuple[str, str], dict[str, Any]]:
    """At most `sample` threads, chosen at random but reproducibly, with all their namespaces."""
    threads = sorted({t for t, _ in latest})
    if sample is None or len(threads) <= sample:
        return latest
    chosen = set(random.Random(seed).sample(threads, sample))  # noqa: S311 - a sample, not a secret
    return {key: config for key, config in latest.items() if key[0] in chosen}


class _Session:
    """One scan: the checkpoints it reads, and the report it builds from them."""

    def __init__(
        self, graph: Any, saver: Any, migrations: Sequence[Migration], lock: GraphShape | None
    ) -> None:
        self.graph, self.saver, self.migrations = graph, saver, list(migrations)
        self.locked = dict(iter_graphs(lock)) if lock is not None else {}
        self.known_nodes = set(graph.nodes) | (set(lock["nodes"]) if lock is not None else set())
        self.has_lock = lock is not None
        self.report = ScanReport()
        self.needed: dict[str, Counter[str]] = {
            m.describe(): Counter(paused=0, finished=0) for m in self.migrations
        }
        self.foreign: set[str] = set()
        self.recorder = _ClassRefRecorder(saver.serde)

    def __enter__(self) -> _Session:
        self._serde = self.saver.serde
        self.saver.serde = self.recorder
        return self

    def __exit__(self, *exc: object) -> None:
        self.saver.serde = self._serde

    def add(self, thread_id: str, ns: str, saved: Any, refs: set[tuple[str, str]]) -> None:
        if saved is None or thread_id in self.foreign:
            return
        if ns == "":
            mentioned = _nodes_referenced(saved.checkpoint) - {_lg.START}
            if mentioned and not mentioned & self.known_nodes:
                if self.has_lock:
                    self.foreign.add(thread_id)  # another graph's thread: none of its nodes are ours
                    return
                self.report.unrelated += 1
        self.report.checkpoints += 1
        sub, path = graph_for_ns(self.graph, ns)
        analysis = _Analysis(thread_id, ns, path, sub, self.locked.get(path), self.saver)
        raw = analysis.run(saved, refs)
        if ns == "" and analysis.pending:
            self.report.paused += 1
        if ns and not analysis.pending:
            return  # a finished subgraph run: nothing will resume it
        if not self.migrations:
            self.report.issues += raw
            return
        migrated, applied, _ = apply_migrations(saved, self.migrations, self.graph)
        for name in applied:
            self.needed[name]["paused" if analysis.pending else "finished"] += 1
        if not applied:
            self.report.issues += raw
            return
        used = [m for m in self.migrations if m.describe() in applied]
        revived = {r for r in refs if any(m.handles(Finding("GL301", f"{r[0]}:{r[1]}", "")) for m in used)}
        again = _Analysis(thread_id, ns, path, sub, self.locked.get(path), self.saver)
        after = {i.key() for i in again.run(migrated, refs - revived)}
        for issue in raw:
            if issue.key() in after:
                self.report.issues.append(issue)
            else:
                self.report.issues.append(
                    dataclasses.replace(issue, handled_by=_repairers(issue, used, path))
                )

    def finish(self, latest: dict[tuple[str, str], Any], stored: int) -> ScanReport:
        report = self.report
        report.threads = len({t for t, _ in latest} - self.foreign)
        report.stored_threads = stored
        report.foreign = len(self.foreign)
        report.migrations_needed = {name: dict(counts) for name, counts in self.needed.items()}
        order = {Severity.BREAKING: 0, Severity.WARNING: 1, Severity.INFO: 2}
        report.issues.sort(key=lambda i: (order[i.level], i.code, i.thread_id, i.subject))
        return report


def _prepare(graph: Any, saver: Any) -> Any:
    saver = _unwrap(saver if saver is not None else graph.checkpointer)
    if saver is None:
        raise ValueError("scan needs a checkpointer: pass saver= or compile the graph with one")
    return saver


def scan(
    graph: Any,
    saver: Any = None,
    *,
    migrations: Sequence[Migration] = (),
    lock: GraphShape | None = None,
    thread_ids: Iterable[str] | None = None,
    thread_prefix: str | None = None,
    where: dict[str, Any] | None = None,
    sample: int | None = None,
    seed: int = 0,
    on_progress: Callable[[int, int], None] | None = None,
) -> ScanReport:
    """Report what `graph` would do to every thread stored in `saver` (default: the graph's checkpointer).

    With `lock`, threads that mention none of the graph's nodes, old or new, are counted as another
    graph's and skipped. `thread_ids`, `thread_prefix` and `where` (checkpoint metadata) narrow the
    scan explicitly. `sample` scans at most that many threads, chosen reproducibly from `seed`.
    `on_progress(done, total)` is called as checkpoints are read.
    """
    saver = _prepare(graph, saver)
    filt = ThreadFilter(tuple(thread_ids) if thread_ids is not None else None, thread_prefix, where)
    with _Session(graph, saver, migrations, lock) as session:
        everything = _latest_checkpoints(saver, filt)
        latest = _sampled(everything, sample, seed)
        for done, ((thread_id, ns), config) in enumerate(sorted(latest.items()), start=1):
            session.recorder.take()
            saved = saver.get_tuple(config)
            session.add(thread_id, ns, saved, session.recorder.take())
            if on_progress is not None:
                on_progress(done, len(latest))
    return session.finish(latest, len({t for t, _ in everything}))


async def ascan(
    graph: Any,
    saver: Any = None,
    *,
    migrations: Sequence[Migration] = (),
    lock: GraphShape | None = None,
    thread_ids: Iterable[str] | None = None,
    thread_prefix: str | None = None,
    where: dict[str, Any] | None = None,
    sample: int | None = None,
    seed: int = 0,
    on_progress: Callable[[int, int], None] | None = None,
) -> ScanReport:
    """`scan` for async checkpointers (AsyncPostgresSaver, AsyncSqliteSaver)."""
    saver = _prepare(graph, saver)
    filt = ThreadFilter(tuple(thread_ids) if thread_ids is not None else None, thread_prefix, where)
    with _Session(graph, saver, migrations, lock) as session:
        everything = await _alatest_checkpoints(saver, filt)
        latest = _sampled(everything, sample, seed)
        for done, ((thread_id, ns), config) in enumerate(sorted(latest.items()), start=1):
            session.recorder.take()
            saved = await saver.aget_tuple(config)
            session.add(thread_id, ns, saved, session.recorder.take())
            if on_progress is not None:
                on_progress(done, len(latest))
    return session.finish(latest, len({t for t, _ in everything}))


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
    _pending_nodes: set[str] = dataclasses.field(default_factory=set)
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

        self._pending_nodes = expected | {j[1] for j, _ in barriers.values()}
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

        # The run loop plans the first step from `updated_channels` only. A deferred trigger listed
        # there is planned like any other once `defer` is off; one that isn't is never planned.
        updated = ckpt.get("updated_channels")
        for node in sorted(expected_later & set(self.graph.nodes)):
            channel = _lg.branch_channel(node)
            stored = ckpt["channel_values"].get(channel)
            if updated is None or channel in updated:
                continue
            if (
                _is_after_finish(stored)
                and not stored[1]
                and not _after_finish(self.graph.channels.get(channel))
            ):
                self.issue(
                    "GL102",
                    node,
                    f"'{node}' is deferred and waiting for the run to finish, but it is no longer deferred. "
                    "Nothing will schedule it: it never runs, and no error is raised.",
                )

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
            if channel in self.graph.channels:
                continue
            if target not in self.graph.nodes:
                self.issue(
                    "GL101",
                    target,
                    f"Waiting on a fan-in into '{target}' after {sorted(seen)} finished, but '{target}' no "
                    "longer exists. It will never run.",
                )
                continue
            self.issue(
                "GL103",
                channel,
                f"'{target}' is waiting on {sorted(set(sources) - seen)} after {sorted(seen)} finished, but "
                "its fan-in channel was renamed. It will never run.",
            )

        self._check_orphaned_writes(writes)
        self._check_stale_channels(ckpt, expected | {j[1] for j, _ in barriers.values()})
        self._check_values(values)
        self._check_interrupt_order(ckpt, writes, step)
        return self.issues

    def _check_orphaned_writes(self, writes: Sequence[tuple[str, str, Any]]) -> None:
        """Writes of tasks that finished before the pause, to channels the new graph doesn't have.

        LangGraph applies them when the step completes, and drops any to an unknown channel with only
        a log line: "wrote to unknown channel, ignoring it".
        """
        reported = {(i.code, i.subject) for i in self.issues}
        for _, channel, value in writes:
            if channel.startswith("__") or channel in self.graph.channels:
                continue
            join = _lg.parse_join(channel)
            if join is not None:
                target = join[1]
                if target not in self.graph.nodes:
                    key = ("GL101", target)
                    message = (
                        f"A finished '{value}' wrote to the fan-in into '{target}', which no longer exists. "
                        "LangGraph drops the write."
                    )
                else:
                    key = ("GL103", channel)
                    message = (
                        f"A finished '{value}' wrote to fan-in channel '{channel}', which the new graph "
                        f"names differently. LangGraph drops the write, so '{target}' never runs."
                    )
                if key not in reported:
                    reported.add(key)
                    self.issue(key[0], key[1], message)
            elif not channel.startswith(_lg.BRANCH_PREFIX) and ("GL203", channel) not in reported:
                reported.add(("GL203", channel))
                self.issue(
                    "GL203",
                    channel,
                    f"A finished task wrote to removed field '{channel}'. LangGraph drops the write.",
                    severity=Severity.INFO,
                )

    def _check_stale_channels(self, ckpt: Any, pending: set[str]) -> None:
        """Channels the new graph doesn't have make interrupt_before re-fire forever (see GL203).

        LangGraph marks only the new graph's channels as seen on resume, but its breakpoint check
        compares every stored version. A leftover channel of any kind does it: a removed field, the
        trigger of a renamed node that already ran, a fan-in listed in another order. It only bites
        when the thread reaches an interrupt_before node, so a thread past every breakpoint is fine.
        """
        breakpoints = _breakpoints(self.graph)
        if not breakpoints or not _reaches(self.graph, pending, breakpoints, finished=not self.pending):
            return
        reported = {(i.code, i.subject) for i in self.issues}
        where = ", ".join(breakpoints)
        # A channel the graph no longer has is never written again. If a resume under the old code
        # already marked its current version as seen by the breakpoint, it can never look new.
        seen_by_breakpoint = ckpt["versions_seen"].get(_lg.INTERRUPT, {})
        for channel, version in sorted(ckpt["channel_versions"].items()):
            if channel in self.graph.channels or channel in (_lg.START, _lg.TASKS):
                continue
            if channel in seen_by_breakpoint and not version > seen_by_breakpoint[channel]:
                continue
            join = _lg.parse_join(channel)
            if channel.startswith(_lg.BRANCH_PREFIX):
                code, subject = "GL101", channel[len(_lg.BRANCH_PREFIX) :]
                what = f"a leftover trigger of removed node '{subject}'"
            elif join is not None:
                code, subject = "GL103", channel
                what = f"a leftover fan-in channel '{channel}'"
            else:
                code, subject = "GL203", channel
                what = f"a value for removed field '{channel}'"
            if (code, subject) in reported:
                continue
            reported.add((code, subject))
            self.value_issue(
                code,
                subject,
                f"Holds {what}. At an interrupt_before breakpoint ({where}) this thread will pause again "
                "on every resume and never get past it, whether it is paused there now or reaches it later.",
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
                # A deferred node's trigger is `(value, finished)`: until the run finishes, it's due later.
                pending_finish = _is_after_finish(values[channel]) and not values[channel][1]
                (later if pending_finish else nodes).add(node)
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
                if stored is not _lg.MISSING and self._latent_barrier(name, channel, stored):
                    restored[name] = channel
                    continue
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

    def _latent_barrier(self, name: str, channel: Any, stored: Any) -> bool:
        """A fan-in barrier restored in the wrong shape that holds no sources yet.

        It fires nothing and crashes nothing until one of its sources writes to it again, so it only
        matters if the thread can still reach a source. True when handled here.
        """
        seen = getattr(channel, "seen", None)
        join = _lg.parse_join(name)
        if join is None or seen is None or isinstance(seen, (set, frozenset)) or _barrier_seen(stored):
            return False
        sources, target = join
        if _reaches(self.graph, self._pending_nodes, sources, finished=not self.pending):
            self.value_issue(
                "GL102",
                target,
                f"The fan-in into '{target}' restores in the wrong shape. The next time one of {sources} "
                "finishes, writing to it crashes.",
            )
        return True

    def _check_reducers(self, values: dict[str, Any]) -> None:
        """Would the new reducer accept the stored value? Tried with an empty update of the new type."""
        for name, spec in self.graph.builder.channels.items():
            if name not in values or not isinstance(spec, _lg.BinaryOperatorAggregate):
                continue
            empty = spec.from_checkpoint(_lg.MISSING).value  # the reducer's own starting value, e.g. []
            if empty is _lg.MISSING:
                continue
            try:
                spec.operator(values[name], empty)
            except Exception as exc:
                self.value_issue(
                    "GL204",
                    name,
                    f"State field '{name}' holds {type(values[name]).__name__} {_short(values[name], 60)}, "
                    f"which the new reducer can't merge: {_short(exc)}. The next write to it raises.",
                    severity=Severity.BREAKING,
                )

    def _check_values(self, values: dict[str, Any]) -> None:
        self._check_reducers(values)
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
            if new_sites is None or interrupt_change(old_sites, new_sites) != "moved":
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


def _after_finish(channel: Any) -> bool:
    return isinstance(channel, (_lg.LastValueAfterFinish, _lg.NamedBarrierValueAfterFinish))


def _is_after_finish(value: Any) -> bool:
    return isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[1], bool)


def _successors(graph: Any) -> dict[str, set[str]]:
    """Where each node can go next, from the graph's edges. A branch with unknown targets goes anywhere."""
    builder = graph.builder
    everywhere = set(builder.nodes)
    succ: dict[str, set[str]] = {name: set() for name in [_lg.START, *builder.nodes]}
    for start, end in builder.edges:
        succ.setdefault(start, set()).add(end)
    for starts, end in builder.waiting_edges:
        for start in starts:
            succ.setdefault(start, set()).add(end)
    for start, branches in builder.branches.items():
        for branch in branches.values():
            ends = getattr(branch, "ends", None)
            succ.setdefault(start, set()).update(set(ends.values()) if ends else everywhere)
    for name, spec in builder.nodes.items():
        ends = getattr(spec, "ends", None)  # Command(goto=...) targets declared on the node
        if ends:
            succ[name].update(ends)
    return succ


def _reaches(graph: Any, pending: set[str], targets: list[str], *, finished: bool) -> bool:
    """Whether a thread waiting on `pending` (or, if finished, one given new input) can reach `targets`."""
    if "*" in targets:
        return True
    succ = _successors(graph)
    frontier = [_lg.START] if finished else [n for n in pending if n in succ]
    seen: set[str] = set()
    while frontier:
        node = frontier.pop()
        if node in seen:
            continue
        seen.add(node)
        if node in targets:
            return True
        frontier.extend(succ.get(node, ()))
    return False


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
