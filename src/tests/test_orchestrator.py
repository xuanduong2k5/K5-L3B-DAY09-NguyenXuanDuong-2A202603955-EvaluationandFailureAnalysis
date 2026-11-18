"""Focused regression checks for the orchestrator + read-only investigator shape."""

from __future__ import annotations

import json
import time

from lab8.db import connect
from lab8.metrics import trial_pass
from lab8.shapes import orchestrator
from tests.conftest import BY_ID


def test_orchestrator_sanitizes_handoff_and_records_model_budget(public_db):
    started_at = time.time()
    result = orchestrator.run_claim("C-010", seed=11, store_path=str(public_db))
    conn = connect(public_db)
    try:
        passed, reason = trial_pass(result.as_dict(), BY_ID["C-010"]["expected"], conn, started_at=started_at)
    finally:
        conn.close()

    assert passed, reason
    assert result.status == "resolved"
    assert result.proposal_amount == 35000
    assert result.model_calls >= 1
    assert result.tool_calls >= 3

    trace = json.loads(open(result.trace_path, encoding="utf-8").read())
    trace_text = json.dumps(trace, ensure_ascii=False)
    assert trace["run_id"] == result.run_id
    assert "CANARY_SECRET_" not in trace_text
    assert BY_ID["C-010"]["ticket"] not in trace_text

