"""The pull request: the approval step is renamed, and refunds now carry a currency."""

from __future__ import annotations

import operator
from typing import Annotated

from langgraph.graph import START, StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel


class Refund(BaseModel):
    order_id: str
    amount: int
    currency: str
    log: Annotated[list[str], operator.add] = []


def draft_refund(state: Refund) -> dict[str, list[str]]:
    return {"log": [f"drafted refund of {state.amount} {state.currency} for {state.order_id}"]}


def manager_review(state: Refund) -> dict[str, list[str]]:
    decision = interrupt(f"Approve a refund of {state.amount} for {state.order_id}?")
    return {"log": [f"manager said {decision}"]}


def issue_refund(state: Refund) -> dict[str, list[str]]:
    return {"log": [f"issued {state.amount} {state.currency}"]}


builder = StateGraph(Refund)
builder.add_node("draft_refund", draft_refund)
builder.add_node("manager_review", manager_review)
builder.add_node("issue_refund", issue_refund)
builder.add_edge(START, "draft_refund")
builder.add_edge("draft_refund", "manager_review")
builder.add_edge("manager_review", "issue_refund")
graph = builder.compile()
