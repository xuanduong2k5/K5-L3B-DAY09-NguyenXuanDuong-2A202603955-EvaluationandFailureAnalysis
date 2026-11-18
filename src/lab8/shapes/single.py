"""Shape B — one bounded ReAct agent with host-controlled refund writes."""

from __future__ import annotations

import sqlite3
from typing import Any

from lab8.db import connect
from lab8.durability import InjectedCrash, issue_refund
from lab8.mock_llm import complete
from lab8.runtime import run_react
from lab8.schemas import Budget, RunResult, make_run_id
from lab8.shapes.common import finish
from lab8.tools import get_claim_view, tool_cost
from lab8.trace import Trace


def run_claim(
    claim_id: str,
    *,
    seed: int,
    store_path: str,
    crash_at: str | None = None,
) -> RunResult:
    conn = connect(store_path)
    try:
        return _run(conn, claim_id, seed=seed, crash_at=crash_at)
    finally:
        conn.close()


def _run(
    conn: sqlite3.Connection,
    claim_id: str,
    *,
    seed: int,
    crash_at: str | None,
) -> RunResult:
    budget = Budget()
    run_id = make_run_id("single", claim_id, seed)
    trace = Trace(run_id)

    # The initial read uses the same metered adapter as the ReAct tools. Keep the
    # ticket in the agent's observation, but never copy it into the trace.
    budget.add_tool(tool_cost())
    claim = get_claim_view(conn, claim_id)
    safe_claim = {
        key: claim[key]
        for key in ("found", "claim_id", "principal_id", "order_id")
        if key in claim
    }
    trace.add("claim_view", safe_claim)

    def decide(observation: dict[str, Any]) -> dict[str, Any]:
        """Select bounded read actions from structured observations only."""
        last_tool = observation.get("last_tool")
        last_result = observation.get("last_result") or {}
        complete(
            budget,
            seed=seed,
            purpose="react",
            prompt=f"step={budget.model_calls + 1};last_tool={last_tool or 'start'}",
        )

        if last_tool == "lookup_order":
            trace.add(
                "tool_result",
                {
                    "tool": last_tool,
                    "visible": bool(last_result.get("visible")),
                    "reason": last_result.get("reason"),
                    "order": _safe_order(last_result.get("order")),
                },
            )
        elif last_tool == "compute_compensation":
            trace.add("tool_result", {"tool": last_tool, **_safe_quote(last_result)})

        observed_claim = observation.get("claim") or safe_claim
        if not observed_claim.get("found"):
            trace.add("agent_action", {"action": "final", "status": "failed"})
            return {"final": {"status": "failed", "proposal_amount": 0, "reason": "unknown_claim"}}
        if not observed_claim.get("order_id"):
            trace.add("agent_action", {"action": "final", "status": "needs_info"})
            return {"final": {"status": "needs_info", "proposal_amount": 0, "reason": "missing_order_id"}}

        if last_tool is None:
            trace.add("agent_action", {"action": "lookup_order"})
            return {
                "tool": "lookup_order",
                "args": {
                    "order_id": observed_claim["order_id"],
                    "principal_id": observed_claim["principal_id"],
                },
            }

        if last_tool == "lookup_order":
            if not last_result.get("visible"):
                trace.add("agent_action", {"action": "final", "status": "escalated"})
                return {
                    "final": {
                        "status": "escalated",
                        "proposal_amount": 0,
                        "reason": last_result.get("reason", "order_not_visible"),
                    }
                }
            trace.add("agent_action", {"action": "compute_compensation"})
            return {"tool": "compute_compensation", "args": {"order": last_result["order"]}}

        if last_tool == "compute_compensation":
            quote = _safe_quote(last_result)
            amount = int(quote["amount_vnd"])
            status = "resolved" if amount > 0 else "denied"
            trace.add("agent_action", {"action": "final", "status": status})
            return {
                "final": {
                    "status": status,
                    "proposal_amount": amount,
                    "reason": quote.get("reason"),
                }
            }

        trace.add("agent_action", {"action": "final", "status": "failed", "reason": "invalid_state"})
        return {"final": {"status": "failed", "proposal_amount": 0, "reason": "invalid_state"}}

    react_result = run_react(
        conn,
        budget,
        observation={"claim": claim},
        decide=decide,
        max_steps=8,
        worker=True,
    )
    final = react_result["final"]
    status = final["status"]
    proposal = int(final.get("proposal_amount", 0))
    reason = final.get("reason")

    if status == "resolved" and proposal > 0:
        # The host owns approval checks, order cap, idempotency, and the write.
        # Pass the full quote so per-order history is applied exactly once.
        try:
            outcome = issue_refund(conn, claim_id, proposal, crash_at=crash_at)
        except InjectedCrash:
            trace.add("crash", {"at": crash_at})
            trace.write()
            raise
        trace.add(
            "host_result",
            {"ok": bool(outcome["ok"]), "reason": outcome["reason"], "conflict": bool(outcome["conflict"])},
        )
        if not outcome["ok"]:
            status = "failed" if outcome["conflict"] else "denied"
            reason = outcome["reason"]
        else:
            reason = None

    return finish(
        conn,
        shape="single",
        claim_id=claim_id,
        seed=seed,
        status=status,
        proposal_amount=proposal,
        budget=budget,
        trace=trace,
        error_reason=reason,
    )


def _safe_order(order: Any) -> dict[str, Any] | None:
    if not isinstance(order, dict):
        return None
    allowed = (
        "order_id",
        "status",
        "delay_days",
        "shipping_fee_vnd",
        "paid_vnd",
        "damage_verified",
        "carrier",
    )
    return {key: order[key] for key in allowed if key in order}


def _safe_quote(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"amount_vnd": 0, "reason": "invalid_quote"}
    return {
        key: payload[key]
        for key in ("amount_vnd", "reason", "clause_ids", "parts")
        if key in payload
    }
