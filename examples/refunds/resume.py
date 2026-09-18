"""After the deploy: a manager approves refund-1, and it runs under refunds_v2."""

from __future__ import annotations

import sys

import refunds_v2
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from migrations import MIGRATIONS

import graphlock

path = sys.argv[1] if len(sys.argv) > 1 else "refunds.sqlite"
with SqliteSaver.from_conn_string(path) as saver:
    graph = graphlock.with_migrations(refunds_v2.builder.compile(checkpointer=saver), MIGRATIONS)
    result = graph.invoke(Command(resume="yes"), {"configurable": {"thread_id": "refund-1"}})
    print("\n".join(result["log"]))
