"""Find each thread's latest checkpoint with one query, for the stores that allow it.

The generic way is the checkpointer's own `list()`, which loads and deserializes every checkpoint
of every thread: its whole history, to keep one checkpoint per thread. For SQLite and Postgres, one
indexed query returns just the latest checkpoint id per (thread, namespace), and `scan` then loads
only those. `tests/test_stores.py` holds both paths to the same answer.

Like `_lg.py`, this module knows things the checkpointer packages don't promise: their table
layout and their cursor helpers. When it doesn't recognise the store, or a metadata filter is
asked for, it returns None and the generic path runs.
"""

from __future__ import annotations

from typing import Any

Latest = dict[tuple[str, str], dict[str, Any]]


def latest_checkpoints(saver: Any, filt: Any) -> Latest | None:
    """(thread_id, checkpoint_ns) -> config of its latest checkpoint, or None to use `list()`."""
    if filt.where:
        return None  # metadata filters go through the checkpointer's own list()
    kind = f"{type(saver).__module__}.{type(saver).__qualname__}"
    if kind == "langgraph.checkpoint.sqlite.SqliteSaver":
        return _sqlite(saver, filt)
    if kind == "langgraph.checkpoint.postgres.PostgresSaver":
        return _postgres(saver, filt)
    return None


def _where(filt: Any, placeholder: str, *, any_array: bool) -> tuple[str, list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if filt.thread_ids is not None:
        if any_array:
            clauses.append(f"thread_id = ANY({placeholder})")
            params.append(list(filt.thread_ids))
        else:
            clauses.append(f"thread_id IN ({', '.join(placeholder for _ in filt.thread_ids)})")
            params.extend(filt.thread_ids)
    if filt.prefix:
        escaped = filt.prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        clauses.append(f"thread_id LIKE {placeholder} ESCAPE '\\'")
        params.append(escaped + "%")
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _config(thread_id: str, ns: str, checkpoint_id: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}}


def _sqlite(saver: Any, filt: Any) -> Latest | None:
    where, params = _where(filt, "?", any_array=False)
    query = (
        "SELECT thread_id, checkpoint_ns, MAX(checkpoint_id) FROM checkpoints"  # noqa: S608 - fixed text
        f"{where} GROUP BY thread_id, checkpoint_ns"
    )
    try:
        with saver.cursor(transaction=False) as cur:
            rows = cur.execute(query, params).fetchall()
    except Exception:
        return None
    return {(t, ns): _config(t, ns, cid) for t, ns, cid in rows}


def _postgres(saver: Any, filt: Any) -> Latest | None:
    where, params = _where(filt, "%s", any_array=True)
    query = (
        "SELECT DISTINCT ON (thread_id, checkpoint_ns) thread_id, checkpoint_ns, checkpoint_id "  # noqa: S608
        f"FROM checkpoints{where} "
        'ORDER BY thread_id, checkpoint_ns, checkpoint_id COLLATE "C" DESC'
    )
    try:
        with saver._cursor() as cur:
            cur.execute(query, params)
            rows = cur.fetchall()
    except Exception:
        return None
    return {
        (r["thread_id"], r["checkpoint_ns"]): _config(r["thread_id"], r["checkpoint_ns"], r["checkpoint_id"])
        for r in rows
    }
