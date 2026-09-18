"""Run five refund requests under the deployed graph. Three are left waiting for a manager."""

from __future__ import annotations

import sys

import refunds_v1
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

path = sys.argv[1] if len(sys.argv) > 1 else "refunds.sqlite"
with SqliteSaver.from_conn_string(path) as saver:
    graph = refunds_v1.builder.compile(checkpointer=saver)
    for n, amount in enumerate([40, 125, 60, 900, 15], start=1):
        config = {"configurable": {"thread_id": f"refund-{n}"}}
        graph.invoke({"order_id": f"A-10{n}", "amount": amount}, config)
        if n in (2, 5):
            graph.invoke(Command(resume="yes"), config)  # the manager already answered these
print(f"seeded 5 refunds in {path}: refund-1, refund-3 and refund-4 wait for a manager")
