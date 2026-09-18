"""Every LangGraph internal graphlock relies on, in one place.

LangGraph does not promise a stable API for checkpoint layout, channel classes or task ids. graphlock
needs all three, so it imports them here and nowhere else. tests/test_langgraph_contract.py checks
the ids and names graphlock recomputes against LangGraph's own, and tests/corpus.py pins the behaviour
the rules rely on, so a LangGraph release that changes one fails the tests before it reaches a deploy.
"""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from typing import Any

from langgraph._internal._constants import (
    INTERRUPT,
    NS_END,
    NS_SEP,
    NULL_TASK_ID,
    PULL,
    PUSH,
    RESUME,
    TASKS,
)
from langgraph._internal._typing import MISSING
from langgraph.channels.base import BaseChannel
from langgraph.channels.binop import BinaryOperatorAggregate
from langgraph.channels.ephemeral_value import EphemeralValue
from langgraph.channels.last_value import LastValue, LastValueAfterFinish
from langgraph.channels.named_barrier_value import NamedBarrierValue, NamedBarrierValueAfterFinish
from langgraph.channels.topic import Topic
from langgraph.checkpoint.base import BaseCheckpointSaver, Checkpoint, CheckpointTuple
from langgraph.constants import END, START
from langgraph.pregel._algo import _uuid5_str, _xxhash_str, prepare_next_tasks
from langgraph.pregel._checkpoint import channels_from_checkpoint as _channels_from_checkpoint
from langgraph.types import Interrupt, Send
from xxhash import xxh3_128_hexdigest

__all__ = [
    "END",
    "INTERRUPT",
    "MISSING",
    "NS_END",
    "NS_SEP",
    "NULL_TASK_ID",
    "PULL",
    "PUSH",
    "RESUME",
    "START",
    "TASKS",
    "BaseChannel",
    "BaseCheckpointSaver",
    "BinaryOperatorAggregate",
    "Checkpoint",
    "CheckpointTuple",
    "EphemeralValue",
    "Interrupt",
    "LastValue",
    "LastValueAfterFinish",
    "NamedBarrierValue",
    "NamedBarrierValueAfterFinish",
    "Send",
    "Topic",
    "channels_from_checkpoint",
    "interrupt_id",
    "next_task_names",
    "pull_task_id",
    "push_task_id",
]

BRANCH_PREFIX = "branch:to:"
JOIN_PREFIX = "join:"


def branch_channel(node: str) -> str:
    return f"{BRANCH_PREFIX}{node}"


def parse_join(channel: str) -> tuple[list[str], str] | None:
    """`join:a+b:c` -> (["a", "b"], "c"); None for any other channel."""
    if not channel.startswith(JOIN_PREFIX):
        return None
    body = channel[len(JOIN_PREFIX) :]
    sources, sep, target = body.rpartition(":")
    if not sep:
        return None
    return sources.split("+"), target


def join_channel(sources: Sequence[str], target: str) -> str:
    return f"{JOIN_PREFIX}{'+'.join(sources)}:{target}"


# LangGraph 1.2 added saver=/config= so delta channels can replay their history from the saver.
_RESTORE_TAKES_SAVER = "saver" in inspect.signature(_channels_from_checkpoint).parameters


def channels_from_checkpoint(
    specs: Mapping[str, Any], checkpoint: Checkpoint, *, saver: Any = None, config: Any = None
) -> tuple[Mapping[str, Any], Any]:
    """Restore channels from a checkpoint exactly as the run loop does."""
    if _RESTORE_TAKES_SAVER:
        return _channels_from_checkpoint(specs, checkpoint, saver=saver, config=config)  # type: ignore[no-any-return]
    return _channels_from_checkpoint(specs, checkpoint)  # type: ignore[no-any-return,call-arg]


def _task_id_func(checkpoint: Checkpoint) -> Any:
    return _xxhash_str if checkpoint["v"] > 1 else _uuid5_str


def _checkpoint_id_bytes(checkpoint: Checkpoint) -> bytes:
    return bytes.fromhex(checkpoint["id"].replace("-", ""))


def task_ns(parent_ns: str, node: str) -> str:
    return f"{parent_ns}{NS_SEP}{node}" if parent_ns else node


def pull_task_id(
    checkpoint: Checkpoint, parent_ns: str, step: int, node: str, triggers: Sequence[str]
) -> str:
    """The id LangGraph gives the task that runs `node` because one of `triggers` fired."""
    func = _task_id_func(checkpoint)
    ns = task_ns(parent_ns, node)
    result: str = func(_checkpoint_id_bytes(checkpoint), ns, str(step), node, PULL, *sorted(triggers))
    return result


def push_task_id(checkpoint: Checkpoint, parent_ns: str, step: int, node: str, index: int) -> str:
    """The id LangGraph gives the task created by the `index`-th pending `Send`."""
    func = _task_id_func(checkpoint)
    ns = task_ns(parent_ns, node)
    result: str = func(_checkpoint_id_bytes(checkpoint), ns, str(step), node, PUSH, str(index))
    return result


def interrupt_id(parent_ns: str, node: str, task_id: str) -> str:
    """The id of an interrupt raised inside a task (what `Command(resume={id: ...})` is keyed by)."""
    return xxh3_128_hexdigest(f"{task_ns(parent_ns, node)}{NS_END}{task_id}".encode())


def next_task_names(
    checkpoint: Checkpoint,
    pending_writes: Sequence[tuple[str, str, Any]],
    graph: Any,
    *,
    channels: Mapping[str, Any],
    config: Mapping[str, Any],
    step: int,
) -> dict[str, str]:
    """Task id -> node name for what `graph` would run next from this checkpoint. Runs no node code.

    Plans the step the way the run loop does, not the way `get_state()` does: the loop only considers
    nodes triggered by the checkpoint's `updated_channels`, so a stale name there stops a thread that
    `get_state()` reports as ready to run.
    """
    updated = checkpoint.get("updated_channels")
    plan: Any = prepare_next_tasks  # its overloads don't admit a plain dict config
    tasks = plan(
        checkpoint,
        list(pending_writes),
        graph.nodes,
        channels,
        {},
        dict(config),
        step,
        step + 1,
        for_execution=False,
        trigger_to_nodes=getattr(graph, "trigger_to_nodes", None),
        updated_channels=set(updated) if updated else None,
    )
    return {task_id: task.name for task_id, task in tasks.items()}
