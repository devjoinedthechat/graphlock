"""The refund agent as deployed: draft a refund, wait for a manager, issue it."""

from __future__ import annotations

import operator
from typing import Annotated

from langgraph.graph import START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel


class Refund(BaseModel):
    order_id: str
    amount: int
    log: Annotated[list[str], operator.add] = []


def draft_refund(state: Refund) -> dict[str, list[str]]:
    return {"log": [f"drafted refund of {state.amount} for {state.order_id}"]}


def wait_for_manager_approval(state: Refund) -> dict[str, list[str]]:
    decision = interrupt(f"Approve a refund of {state.amount} for {state.order_id}?")
    return {"log": [f"manager said {decision}"]}


def issue_refund(state: Refund) -> dict[str, list[str]]:
    return {"log": [f"issued {state.amount}"]}


builder = StateGraph(Refund)
builder.add_node("draft_refund", draft_refund)
builder.add_node("wait_for_manager_approval", wait_for_manager_approval)
builder.add_node("issue_refund", issue_refund)
builder.add_edge(START, "draft_refund")
builder.add_edge("draft_refund", "wait_for_manager_approval")
builder.add_edge("wait_for_manager_approval", "issue_refund")
graph = builder.compile()
