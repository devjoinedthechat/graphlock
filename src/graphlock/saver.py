"""A checkpointer wrapper that repairs stored threads as LangGraph reads them.

    graph = builder.compile(checkpointer=PostgresSaver(conn))
    graph = graphlock.with_migrations(graph, MIGRATIONS)

Reads go through `apply_migrations`. Stored checkpoints are never rewritten in place: a thread is
repaired when it's read, and the repair reaches storage with the next checkpoint LangGraph writes for
that thread. Checkpointers that store each channel separately (in-memory, Postgres) only store the
channels a step wrote, so the wrapper adds the repaired channels to that write; otherwise a repaired
value that the step didn't touch would be dropped.

The wrapper remembers which channels it repaired for each thread until that thread's next write.
LangGraph always reads a thread right before writing it, so the memory needed is bounded by the
threads in flight, not the threads stored: it keeps the `max_tracked` most recently read and forgets
the rest. A thread that is only read, by a dashboard polling `get_state()` say, costs nothing once
it falls out.

Each repair is counted in `stats` (migration -> reads it repaired) and logged at DEBUG on the
`graphlock` logger, so you can see migrations fire in production and tell when they stop.
"""

from __future__ import annotations

import logging
import threading
from collections import Counter, OrderedDict
from collections.abc import AsyncIterator, Iterator, Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig

from graphlock import _lg
from graphlock.migrations import Migration, apply_migrations

logger = logging.getLogger("graphlock")

DEFAULT_MAX_TRACKED = 10_000


class MigratingSaver(_lg.BaseCheckpointSaver):  # type: ignore[type-arg]
    """Wraps a checkpointer; checkpoints read through it are repaired by `migrations`."""

    def __init__(
        self,
        inner: _lg.BaseCheckpointSaver,  # type: ignore[type-arg]
        migrations: Sequence[Migration],
        graph: Any,
        *,
        max_tracked: int = DEFAULT_MAX_TRACKED,
    ) -> None:
        # Deliberately no super().__init__(): serde and everything else belong to `inner`.
        self.inner = inner
        self.migrations = list(migrations)
        self.graph = graph
        self.max_tracked = max_tracked
        self.stats: Counter[str] = Counter()
        # (thread, ns) -> channels to write through, least recently read first
        self._repaired: OrderedDict[tuple[str, str], set[str]] = OrderedDict()
        self._lock = threading.Lock()

    @property  # type: ignore[override]
    def serde(self) -> Any:
        return self.inner.serde

    @serde.setter
    def serde(self, value: Any) -> None:
        self.inner.serde = value

    @property
    def config_specs(self) -> list[Any]:
        return self.inner.config_specs

    def _migrate(self, saved: _lg.CheckpointTuple | None) -> _lg.CheckpointTuple | None:
        if saved is None:
            return None
        migrated, applied, changed = apply_migrations(saved, self.migrations, self.graph)
        if applied:
            key = _key(saved.config)
            with self._lock:
                self.stats.update(applied)
                channels = self._repaired.pop(key, set())
                self._repaired[key] = channels | changed
                while len(self._repaired) > self.max_tracked:
                    self._repaired.popitem(last=False)
            logger.debug("repaired thread %s (ns %r) on read: %s", key[0], key[1], ", ".join(applied))
        return migrated  # type: ignore[no-any-return]

    def _write_through(self, config: RunnableConfig, checkpoint: Any, new_versions: Any) -> Any:
        with self._lock:
            repaired = self._repaired.pop(_key(config), None)
        if not repaired:
            return new_versions
        versions = dict(new_versions)
        for channel in repaired:
            if channel in checkpoint["channel_values"] and channel in checkpoint["channel_versions"]:
                versions.setdefault(channel, checkpoint["channel_versions"][channel])
        return versions

    # reads: repaired
    def get_tuple(self, config: RunnableConfig) -> _lg.CheckpointTuple | None:
        return self._migrate(self.inner.get_tuple(config))

    def list(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> Iterator[_lg.CheckpointTuple]:
        for saved in self.inner.list(config, filter=filter, before=before, limit=limit):
            yield self._migrate(saved)  # type: ignore[misc]

    async def aget_tuple(self, config: RunnableConfig) -> _lg.CheckpointTuple | None:
        return self._migrate(await self.inner.aget_tuple(config))

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[_lg.CheckpointTuple]:
        async for saved in self.inner.alist(config, filter=filter, before=before, limit=limit):
            yield self._migrate(saved)  # type: ignore[misc]

    # everything else: straight through
    def put(
        self, config: RunnableConfig, checkpoint: Any, metadata: Any, new_versions: Any
    ) -> RunnableConfig:
        return self.inner.put(
            config, checkpoint, metadata, self._write_through(config, checkpoint, new_versions)
        )

    async def aput(
        self, config: RunnableConfig, checkpoint: Any, metadata: Any, new_versions: Any
    ) -> RunnableConfig:
        versions = self._write_through(config, checkpoint, new_versions)
        return await self.inner.aput(config, checkpoint, metadata, versions)

    def put_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        self.inner.put_writes(config, writes, task_id, task_path)

    async def aput_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        await self.inner.aput_writes(config, writes, task_id, task_path)

    def get_next_version(self, current: Any, channel: None) -> Any:
        return self.inner.get_next_version(current, channel)

    def with_allowlist(self, extra_allowlist: Any) -> MigratingSaver:
        clone = MigratingSaver(
            self.inner.with_allowlist(extra_allowlist),
            self.migrations,
            self.graph,
            max_tracked=self.max_tracked,
        )
        clone._repaired, clone._lock, clone.stats = self._repaired, self._lock, self.stats
        return clone

    def __getattr__(self, name: str) -> Any:
        # delete_thread, copy_thread, prune, get_delta_channel_history, setup(), ... and their async twins
        return getattr(self.inner, name)


# BaseCheckpointSaver defines these (raising NotImplementedError), so __getattr__ never sees them.
for _name in (
    "delete_thread",
    "adelete_thread",
    "delete_for_runs",
    "adelete_for_runs",
    "copy_thread",
    "acopy_thread",
    "prune",
    "aprune",
    "get_delta_channel_history",
    "aget_delta_channel_history",
):
    if hasattr(_lg.BaseCheckpointSaver, _name):

        def _delegate(self: MigratingSaver, *args: Any, _n: str = _name, **kwargs: Any) -> Any:
            return getattr(self.inner, _n)(*args, **kwargs)

        _delegate.__name__ = _name
        setattr(MigratingSaver, _name, _delegate)


def _key(config: Any) -> tuple[str, str]:
    conf = config.get("configurable", {})
    return (str(conf.get("thread_id")), str(conf.get("checkpoint_ns", "")))


def with_migrations(
    graph: Any, migrations: Sequence[Migration], *, max_tracked: int = DEFAULT_MAX_TRACKED
) -> Any:
    """Return `graph` with its checkpointer wrapped so stored threads are repaired on read.

    The graph must have been compiled with a checkpointer. Subgraphs that share the parent's
    checkpointer (the default) are covered too. `max_tracked` bounds how many threads' repairs are
    remembered between a read and the thread's next write; see the module docstring.
    """
    saver = getattr(graph, "checkpointer", None)
    if not isinstance(saver, _lg.BaseCheckpointSaver):
        raise TypeError("with_migrations needs a graph compiled with a checkpointer instance")
    if isinstance(saver, MigratingSaver):
        saver = saver.inner
    graph.checkpointer = MigratingSaver(saver, migrations, graph, max_tracked=max_tracked)
    return graph
