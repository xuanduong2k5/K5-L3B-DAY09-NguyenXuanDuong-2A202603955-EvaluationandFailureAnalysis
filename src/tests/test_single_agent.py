"""Focused regression checks for the single ReAct shape."""

import json
import time

from lab8.db import connect, init_store
from lab8.metrics import trial_pass
from lab8.shapes import single
from tests.conftest import BY_ID, CLAIMS, WORLD


def test_single_uses_order_facts_and_only_read_tools(tmp_path, monkeypatch):
    from lab8 import runtime

    store = tmp_path / "single-order-facts.sqlite"
    init_store(store, WORLD, CLAIMS).close()
    called_tools = []
    original_dispatch = runtime.dispatch

    def recording_dispatch(conn, budget, name, args, *, worker):
        called_tools.append(name)
        return original_dispatch(conn, budget, name, args, worker=worker)

    monkeypatch.setattr(runtime, "dispatch", recording_dispatch)
    started_at = time.time()
    result = single.run_claim("C-002", seed=55, store_path=str(store))

    conn = connect(store)
    try:
        passed, reason = trial_pass(result.as_dict(), BY_ID["C-002"]["expected"], conn, started_at=started_at)
        refunds = conn.execute("SELECT COUNT(*) AS n FROM refund_ledger WHERE claim_id = 'C-002'").fetchone()["n"]
    finally:
        conn.close()

    assert passed, reason
    assert called_tools == ["lookup_order", "compute_compensation"]
    assert result.status == "denied"
    assert result.proposal_amount == 0
    assert refunds == 0

    trace = json.loads(open(result.trace_path, encoding="utf-8").read())
    trace_text = json.dumps(trace, ensure_ascii=False)
    assert trace["run_id"] == result.run_id
    assert isinstance(trace["events"], list)
    assert "CANARY_SECRET_" not in trace_text
    assert "parent_secret" not in trace_text
    assert BY_ID["C-002"]["ticket"] not in trace_text
