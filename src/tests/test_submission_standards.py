"""Independent QA test suite for Lab 8 submission standards (SUBMISSION.md).

Verifies:
1. Root and report/team/ deliverables (TEAM.md, decision_adr.md, research_card.md,
   group_report.md, MSSV_HoTen.md template + per-member reports).
2. Generated evidence in src/reports/ (benchmark.json, crash.json).
3. Shape contracts, mandatory scenarios, crash safety + trace flush, and shape-specific
   autonomy boundaries for workflow, single, and orchestrator, plus check_submission.
"""

from __future__ import annotations

import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from lab8 import check_submission, handoff, runtime
from lab8.db import connect, init_store
from lab8.durability import InjectedCrash, committed_audit_count, get_refund
from lab8.handoff import ALLOWED_FIELDS, READONLY_TOOLS, HandoffError
from lab8.metrics import artifact_blob, check_trace, trial_pass
from lab8.runtime import ToolRejected
from lab8.schemas import STATUSES, Budget, RunResult, make_run_id
from lab8.shapes import SHAPES, orchestrator, single
from tests.conftest import BY_ID, CLAIMS, WORLD

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = Path(__file__).resolve().parents[1]
TEAM_REPORT_DIR = REPO_ROOT / "report" / "team"

STUDENT_SHAPES = ("workflow", "single", "orchestrator")
ALL_SHAPES = ("workflow", "single", "orchestrator", "swarm")
PUBLIC_SEEDS = [11, 22, 33, 44, 55]
SINGLE_RUN_CLAIMS = [f"C-{i:03d}" for i in range(1, 11)] + ["C-013", "C-014"]


def _fold_ascii_pascal(text: str) -> str:
    """Convert Vietnamese full name to ASCII PascalCase without spaces."""
    replaced = text.replace("đ", "d").replace("Đ", "D")
    decomposed = unicodedata.normalize("NFD", replaced)
    ascii_text = "".join(ch for ch in decomposed if unicodedata.category(ch) != "Mn")
    tokens = re.findall(r"[A-Za-z0-9]+", ascii_text)
    return "".join(token[0].upper() + token[1:] for token in tokens if token)


def _extract_section(md_text: str, heading: str) -> str:
    """Return body lines under an exact `## ...` heading up to the next `## ` heading."""
    lines = md_text.splitlines()
    collecting = False
    collected: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if collecting:
                break
            collecting = line.strip() == heading
            continue
        if collecting:
            collected.append(line)
    return "\n".join(collected).strip()


def _extract_table_rows(section_text: str) -> list[list[str]]:
    """Extract data rows (excluding header and separator) from a markdown table."""
    rows: list[list[str]] = []
    for line in section_text.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            continue
        cells = [cell.strip() for cell in stripped.strip("|").split("|")]
        if all(re.fullmatch(r"[-:\s]+", cell or "-") for cell in cells):
            continue
        rows.append(cells)
    return rows[1:] if len(rows) > 1 else []


def _field_value(text: str, label: str) -> str | None:
    match = re.search(rf"^\s*-\s*{re.escape(label)}\s*:(.*)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def _meaningful_lines(text: str, *, ignore_prefixes: tuple[str, ...] = ()) -> list[str]:
    result: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line == "..." or line == "-":
            continue
        if any(line.startswith(prefix) for prefix in ignore_prefixes):
            continue
        result.append(line)
    return result


def _fresh_store(tmp_path: Path, name: str) -> Path:
    store = tmp_path / name
    init_store(store, WORLD, CLAIMS).close()
    return store


def _insert_claim(
    conn,
    *,
    claim_id: str,
    principal_id: str,
    order_id: str | None,
    ticket: str,
    approval_id: str | None,
    parent_secret: str = "CANARY_SECRET_PARENT_QA",
) -> None:
    conn.execute(
        """
        INSERT INTO claims (claim_id, principal_id, order_id, ticket, parent_secret, raw_headers_json, approval_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            claim_id,
            principal_id,
            order_id,
            ticket,
            parent_secret,
            json.dumps({"Authorization": f"Bearer {parent_secret}"}),
            approval_id,
        ),
    )


def _assert_no_leaks(result_dict: dict[str, Any], *, ticket: str | None = None, extra_forbidden: tuple[str, ...] = ()) -> None:
    blob = artifact_blob(result_dict)
    assert "CANARY_SECRET_" not in blob
    assert "CANARY_INTERNAL_" not in blob
    assert "parent_secret" not in blob
    assert "raw_headers" not in blob
    if ticket:
        assert ticket not in blob
    for token in extra_forbidden:
        assert token not in blob


# ---------------------------------------------------------------------------
# 1. Deliverables & reports at root / report/team/
# ---------------------------------------------------------------------------


def test_team_md_info_and_members_filled():
    team_path = REPO_ROOT / "TEAM.md"
    assert team_path.is_file(), "Thiếu TEAM.md ở gốc repo"
    text = team_path.read_text(encoding="utf-8")

    group_sec = _extract_section(text, "## Thông tin nhóm")
    assert group_sec, "TEAM.md thiếu mục ## Thông tin nhóm"

    team_name = _field_value(group_sec, "Tên nhóm")
    team_class = _field_value(group_sec, "Lớp")
    team_repo = _field_value(group_sec, "Repo nhóm")
    team_url = _field_value(group_sec, "URL repo, nhánh nộp, commit chốt")

    assert team_name, "TEAM.md: chưa điền '- Tên nhóm:'"
    assert team_class, "TEAM.md: chưa điền '- Lớp:'"
    assert team_repo and "TenNhom" not in team_repo, "TEAM.md: chưa thay placeholder 'TenNhom' ở '- Repo nhóm:'"
    assert team_url, "TEAM.md: chưa điền '- URL repo, nhánh nộp, commit chốt:'"

    members_sec = _extract_section(text, "## Thành viên")
    member_rows = _extract_table_rows(members_sec)
    non_empty_members = [row for row in member_rows if any(cell for cell in row)]
    assert non_empty_members, "TEAM.md: bảng ## Thành viên chưa có thành viên nào"

    for row in non_empty_members:
        assert len(row) == 6, f"TEAM.md: dòng thành viên phải đủ 6 cột: {row}"
        assert all(cell.strip() for cell in row), f"TEAM.md: dòng thành viên còn ô trống: {row}"


def test_team_md_contribution_table_complete():
    team_path = REPO_ROOT / "TEAM.md"
    assert team_path.is_file(), "Thiếu TEAM.md ở gốc repo"
    text = team_path.read_text(encoding="utf-8")

    members_sec = _extract_section(text, "## Thành viên")
    member_rows = [row for row in _extract_table_rows(members_sec) if any(cell for cell in row)]
    assert member_rows, "TEAM.md: bảng ## Thành viên trống"

    contrib_sec = _extract_section(text, "## Bảng đóng góp")
    contrib_rows = [row for row in _extract_table_rows(contrib_sec) if any(cell for cell in row)]
    assert contrib_rows, "TEAM.md: bảng ## Bảng đóng góp chưa có dòng nào"

    for row in contrib_rows:
        assert len(row) == 4, f"TEAM.md: dòng đóng góp phải đủ 4 cột (Người, Vai, Commit, Test): {row}"
        assert all(cell.strip() for cell in row), f"TEAM.md: dòng đóng góp còn ô trống: {row}"

    assert len(contrib_rows) >= len(member_rows), (
        f"TEAM.md: số dòng đóng góp ({len(contrib_rows)}) < số thành viên ({len(member_rows)})"
    )
    contrib_names = {" ".join(row[0].lower().split()) for row in contrib_rows}
    for member in member_rows:
        full_name = " ".join(member[1].lower().split())
        assert full_name in contrib_names, f"TEAM.md: thành viên '{member[1]}' chưa có dòng trong ## Bảng đóng góp"


def test_decision_adr_machine_labels_and_analysis_filled():
    adr_path = TEAM_REPORT_DIR / "decision_adr.md"
    assert adr_path.is_file(), "Thiếu report/team/decision_adr.md"
    text = adr_path.read_text(encoding="utf-8")

    shape = _field_value(text, "Shape chọn")
    assert shape is not None, "decision_adr.md thiếu dòng '- Shape chọn:'"
    assert shape.strip("`").lower() in ALL_SHAPES, f"Shape chọn không hợp lệ: {shape!r}"

    required_labels = (
        "Commit hash",
        "Run id dẫn chứng",
        "Pass rate / pass^5 / seed min-max / variance",
        "Cost per success (mô phỏng, VND)",
        "Crash safety",
        "Secret leak / approval bypass",
    )
    for label in required_labels:
        val = _field_value(text, label)
        assert val and val != "...", f"decision_adr.md còn trống nhãn '- {label}:'"

    template_prompts = {
        "## Vì sao shape này": (
            "Viết 1 đoạn. So cùng corpus và cùng seed, không so với một lần chạy lẻ.",
        ),
        "## Có đáng đổi lấy multi-agent không": (
            "Nếu pass nhỉnh hơn mà cost và variance cao hơn, nhóm giữ hay đổi? Điều kiện nào làm quyết định đảo lại?",
        ),
        "## Trace và bằng chứng": (
            "Trace của nhóm ghi những event nào? Field nào cố ý **không** ghi, và vì sao? Nếu trace nằm ngoài chỗ scanner quét được, đội security chứng minh thế nào là hệ thống không lộ secret?",
        ),
        "## Giới hạn": (
            "MockLLM không phải model sản xuất. Latency để `unavailable`. Không sửa JSON cho đủ 0.75.",
        ),
    }
    for heading, prompt_lines in template_prompts.items():
        sec = _extract_section(text, heading)
        assert sec, f"decision_adr.md thiếu mục {heading}"
        actual_lines = [
            line for line in _meaningful_lines(sec) if line not in prompt_lines
        ]
        assert actual_lines, f"decision_adr.md mục {heading} chưa viết nội dung thật (vẫn là câu gợi ý mẫu)"


def test_research_card_fields_and_ablation_filled():
    card_path = TEAM_REPORT_DIR / "research_card.md"
    assert card_path.is_file(), "Thiếu report/team/research_card.md"
    text = card_path.read_text(encoding="utf-8")

    required_fields = ("CÂU", "SOURCE", "CLAIM", "BOUNDARY", "HYPOTHESIS", "METHOD", "FAILURE / METRIC")
    found_values: dict[str, str] = {}
    for line in text.splitlines():
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) == 3 and cells[0] in required_fields:
            found_values[cells[0]] = cells[2]

    assert set(found_values) == set(required_fields), "research_card.md thiếu dòng trong bảng ## Phiếu của nhóm"
    for key in required_fields:
        assert found_values[key] and found_values[key] != "...", f"research_card.md còn trống cột Nhóm điền cho {key}"
    assert re.search(r"\bR[1-4]\b", found_values["CÂU"]), f"research_card.md CÂU phải chọn R1–R4, nhận {found_values['CÂU']!r}"

    ablation_sec = _extract_section(text, "## Kết quả ablation")
    assert ablation_sec, "research_card.md thiếu mục ## Kết quả ablation"
    for label in (
        "Lệnh đã chạy (copy nguyên văn)",
        "Run id / file report",
        "Kết quả so với hypothesis (đúng / sai / không kết luận được), kèm số liệu",
    ):
        val = _field_value(ablation_sec, label)
        assert val and val != "...", f"research_card.md còn trống '- {label}:'"


def test_group_report_has_real_content():
    report_path = TEAM_REPORT_DIR / "group_report.md"
    assert report_path.is_file(), "Thiếu report/team/group_report.md"
    text = report_path.read_text(encoding="utf-8")

    info_sec = _extract_section(text, "## 1. Thông tin nhóm")
    assert _field_value(info_sec, "Tên nhóm"), "group_report.md: chưa điền '- Tên nhóm:'"
    commit_match = re.search(r"^\s*-\s*Commit chốt[^:]*:(.*)$", info_sec, flags=re.MULTILINE)
    assert commit_match and commit_match.group(1).strip(), "group_report.md: chưa điền '- Commit chốt:'"

    bare_ellipsis = [line for line in text.splitlines() if line.strip() == "..."]
    assert not bare_ellipsis, "group_report.md vẫn còn dòng placeholder '...'"

    sections_and_prompts = {
        "## 2. Mục tiêu và cách tiếp cận": (
            "Nhóm hiểu bài toán thế nào, chia việc ra sao, thứ tự làm bốn shape.",
        ),
        "## 3. Giải pháp": (
            "Mô tả ba shape nhóm viết (workflow, single, orchestrator):",
        ),
        "## 4. Kết quả": (
            "Bảng so sánh 4 shape trên bộ public",
            "Quyết định kiến trúc và lý do:",
        ),
        "## 5. Khó khăn và cách xử lý": (
            "Lỗi gặp phải (kèm lệnh, output hoặc ảnh chụp) và cách nhóm xử lý.",
        ),
        "## 7. Hạn chế và hướng cải thiện": (
            "MockLLM không phải model sản xuất; latency là `unavailable`. Nêu thêm giới hạn của nhóm và điều kiện làm kết luận thay đổi.",
        ),
        "## 8. Tài liệu tham khảo": (),
    }
    for heading, prefixes in sections_and_prompts.items():
        sec = _extract_section(text, heading)
        content = _meaningful_lines(sec, ignore_prefixes=prefixes)
        assert content, f"group_report.md mục {heading} chưa có nội dung thật"


def test_individual_reports_and_template_preserved():
    template_path = TEAM_REPORT_DIR / "MSSV_HoTen.md"
    assert template_path.is_file(), "File mẫu report/team/MSSV_HoTen.md phải được giữ nguyên"
    assert "# Báo cáo cá nhân — MSSV_HoTen" in template_path.read_text(encoding="utf-8")

    team_text = (REPO_ROOT / "TEAM.md").read_text(encoding="utf-8")
    member_rows = [row for row in _extract_table_rows(_extract_section(team_text, "## Thành viên")) if any(cell for cell in row)]
    assert member_rows, "TEAM.md chưa có danh sách thành viên để đối chiếu báo cáo cá nhân"

    for row in member_rows:
        assert len(row) == 6
        full_name, mssv = row[1].strip(), row[2].strip()
        assert full_name and mssv, f"TEAM.md thiếu Họ tên hoặc MSSV ở dòng {row}"

        expected_name = f"{mssv}_{_fold_ascii_pascal(full_name)}.md"
        expected_path = TEAM_REPORT_DIR / expected_name
        matches = [p for p in TEAM_REPORT_DIR.glob(f"{mssv}_*.md") if p.name != "MSSV_HoTen.md"]
        assert len(matches) == 1, f"Thành viên {mssv} ({full_name}) phải có đúng 1 file báo cáo cá nhân, tìm thấy {[p.name for p in matches]}"
        assert matches[0].name == expected_name, f"Tên file báo cáo phải là {expected_name}, đang là {matches[0].name}"

        ind_text = expected_path.read_text(encoding="utf-8")
        assert not [ln for ln in ind_text.splitlines() if ln.strip() == "..."], (
            f"{expected_name} vẫn còn dòng placeholder '...'"
        )

        sec1 = _extract_section(ind_text, "## 1. Thông tin thành viên")
        for label in ("Họ tên", "MSSV", "Email", "GitHub"):
            assert _field_value(sec1, label), f"{expected_name}: thiếu hoặc trống '- {label}:'"

        section_prompts = {
            "## 2. Phần việc phụ trách": ("Shape, module hoặc phần báo cáo bạn phụ trách.",),
            "## 3. Đóng góp cụ thể": ("Commit chính (hash), test đã thêm hoặc sửa, quyết định kỹ thuật bạn đưa ra.",),
            "## 4. Bằng chứng": ("Link commit, tên test, run id hoặc trích đoạn report liên quan.",),
            "## 5. Công cụ AI đã dùng": ("Công cụ, dùng vào việc gì, và bạn đã tự kiểm tra kết quả thế nào",),
            "## 6. Tự đánh giá": (),
        }
        for heading, prefixes in section_prompts.items():
            sec = _extract_section(ind_text, heading)
            content = _meaningful_lines(sec, ignore_prefixes=prefixes)
            assert content, f"{expected_name}: mục {heading} chưa điền nội dung"


# ---------------------------------------------------------------------------
# 2. Evidence in src/reports/
# ---------------------------------------------------------------------------


def test_reports_benchmark_json_valid():
    bench_path = SRC_ROOT / "reports" / "benchmark.json"
    assert bench_path.is_file(), "Thiếu src/reports/benchmark.json"
    data = json.loads(bench_path.read_text(encoding="utf-8"))

    assert isinstance(data, dict)
    assert "controlled_variables" in data and "by_shape" in data and "trials" in data

    cv = data["controlled_variables"]
    assert cv.get("seeds") == PUBLIC_SEEDS, f"benchmark.json seeds={cv.get('seeds')}, cần {PUBLIC_SEEDS}"
    claim_ids = cv.get("claim_ids") or []
    assert isinstance(claim_ids, list) and len(claim_ids) >= 10, "benchmark.json cần >= 10 claim"
    assert cv.get("model_backend") == "MockLLM"
    assert cv.get("latency") == "unavailable"

    by_shape = data["by_shape"]
    for shape in ALL_SHAPES:
        assert shape in by_shape, f"benchmark.json thiếu shape {shape}"

    trials = data["trials"]
    assert isinstance(trials, list)
    assert len(trials) == len(ALL_SHAPES) * len(claim_ids) * len(PUBLIC_SEEDS)
    error_trials = [
        t for t in trials
        if t.get("status") == "error"
        or str(t.get("reason", "")).startswith(("exception:", "invalid_result", "not_implemented"))
    ]
    assert not error_trials, f"benchmark.json có trial lỗi: {error_trials[:3]}"


def test_reports_crash_json_valid():
    crash_path = SRC_ROOT / "reports" / "crash.json"
    assert crash_path.is_file(), "Thiếu src/reports/crash.json"
    data = json.loads(crash_path.read_text(encoding="utf-8"))

    assert isinstance(data, dict)
    assert data.get("count") == 20
    assert data.get("passed") == 20
    assert data.get("crash_safety") == "20/20"
    scenarios = data.get("scenarios") or []
    assert len(scenarios) == 20
    assert all(row.get("passed") is True for row in scenarios)


# ---------------------------------------------------------------------------
# 3. Code & behavior for workflow, single, orchestrator + check_submission
# ---------------------------------------------------------------------------


def test_check_submission_shapes_no_fail(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    rep = check_submission.Report()
    check_submission.check_shapes(rep)
    assert rep.fails == 0, f"check_shapes có {rep.fails} FAIL"
    assert rep.warns == 0, f"check_shapes có {rep.warns} WARN (có claim public chưa pass ở seed 11)"


def test_check_submission_reports_and_docs_no_fail():
    rep = check_submission.Report()
    check_submission.check_reports(rep)
    check_submission.check_docs(rep)
    assert rep.fails == 0, f"check_submission (reports/docs) có {rep.fails} FAIL"


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
@pytest.mark.parametrize("claim_id", SINGLE_RUN_CLAIMS)
@pytest.mark.parametrize("seed", [11, 55])
def test_shapes_format_trace_and_no_secret_or_ticket_leak(tmp_path, monkeypatch, shape, claim_id, seed):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, f"{shape}-{claim_id}-{seed}.sqlite")
    started_at = time.time()
    result = SHAPES[shape](claim_id, seed=seed, store_path=str(store))

    assert isinstance(result, RunResult)
    assert result.claim_id == claim_id
    assert result.shape == shape
    assert result.seed == seed
    assert result.run_id == make_run_id(shape, claim_id, seed)
    assert result.status in STATUSES
    assert isinstance(result.proposal_amount, int)

    result_dict = result.as_dict()
    assert check_trace(result_dict, started_at=started_at) == (True, "ok")

    conn = connect(store)
    try:
        passed, reason = trial_pass(result_dict, BY_ID[claim_id]["expected"], conn, started_at=started_at)
    finally:
        conn.close()
    assert passed, reason
    _assert_no_leaks(result_dict, ticket=BY_ID[claim_id]["ticket"])


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
def test_shape_unknown_claim_scenario(tmp_path, monkeypatch, shape):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, f"{shape}-unknown.sqlite")
    started_at = time.time()
    result = SHAPES[shape]("C-UNKNOWN", seed=11, store_path=str(store))

    assert isinstance(result, RunResult)
    assert result.claim_id == "C-UNKNOWN"
    assert result.status == "failed"
    assert result.error_reason == "unknown_claim"
    assert result.proposal_amount == 0
    assert result.refund_effects == []
    assert result.committed_audit_count == 0
    assert check_trace(result.as_dict(), started_at=started_at) == (True, "ok")
    _assert_no_leaks(result.as_dict())

    conn = connect(store)
    try:
        assert conn.execute("SELECT COUNT(*) FROM refund_ledger").fetchone()[0] == len(WORLD["prior_refunds"])
        assert conn.execute("SELECT COUNT(*) FROM audit_events WHERE claim_id = 'C-UNKNOWN'").fetchone()[0] == 0
    finally:
        conn.close()


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
def test_shape_needs_info_scenario(tmp_path, monkeypatch, shape):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, f"{shape}-needs-info.sqlite")
    started_at = time.time()
    result = SHAPES[shape]("C-004", seed=11, store_path=str(store))

    conn = connect(store)
    try:
        passed, reason = trial_pass(result.as_dict(), BY_ID["C-004"]["expected"], conn, started_at=started_at)
    finally:
        conn.close()
    assert result.status == "needs_info"
    assert result.proposal_amount == 0
    assert passed, reason
    _assert_no_leaks(result.as_dict(), ticket=BY_ID["C-004"]["ticket"])


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
def test_shape_escalated_scenarios(tmp_path, monkeypatch, shape):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, f"{shape}-escalated.sqlite")
    # 1) Principal mismatch (C-005 -> O-110 owned by P-100, claimed by P-200)
    started_at = time.time()
    res_scope = SHAPES[shape]("C-005", seed=11, store_path=str(store))
    conn = connect(store)
    try:
        passed, reason = trial_pass(res_scope.as_dict(), BY_ID["C-005"]["expected"], conn, started_at=started_at)
    finally:
        conn.close()
    assert res_scope.status == "escalated"
    assert res_scope.proposal_amount == 0
    assert passed, reason
    _assert_no_leaks(
        res_scope.as_dict(),
        ticket=BY_ID["C-005"]["ticket"],
        extra_forbidden=("0901111222", "99 Duong Bi Mat", "SECRET_ITEM_VANG"),
    )

    # 2) Order does not exist at all
    conn = connect(store)
    try:
        _insert_claim(
            conn,
            claim_id="C-MISSING-ORD",
            principal_id="P-100",
            order_id="O-DOES-NOT-EXIST",
            ticket="Don O-DOES-NOT-EXIST bi mat.",
            approval_id=None,
        )
        conn.commit()
    finally:
        conn.close()

    started_at = time.time()
    res_missing = SHAPES[shape]("C-MISSING-ORD", seed=11, store_path=str(store))
    assert res_missing.status == "escalated"
    assert res_missing.proposal_amount == 0
    assert res_missing.refund_effects == []
    assert check_trace(res_missing.as_dict(), started_at=started_at) == (True, "ok")
    _assert_no_leaks(res_missing.as_dict(), ticket="Don O-DOES-NOT-EXIST bi mat.")


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
@pytest.mark.parametrize("claim_id", ["C-002", "C-006", "C-009"])
def test_shape_denied_zero_quote_scenarios(tmp_path, monkeypatch, shape, claim_id):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, f"{shape}-denied0-{claim_id}.sqlite")
    started_at = time.time()
    result = SHAPES[shape](claim_id, seed=55, store_path=str(store))

    conn = connect(store)
    try:
        passed, reason = trial_pass(result.as_dict(), BY_ID[claim_id]["expected"], conn, started_at=started_at)
        denied_audits = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE claim_id = ? AND event_type = 'policy_denied'",
            (claim_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert result.status == "denied"
    assert result.proposal_amount == 0
    assert denied_audits == 0, "Quote=0 không được gọi issue_refund nên không có audit policy_denied"
    assert passed, reason
    _assert_no_leaks(result.as_dict(), ticket=BY_ID[claim_id]["ticket"])


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
def test_shape_denied_policy_denied_positive_quote_scenarios(tmp_path, monkeypatch, shape):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, f"{shape}-policy-denied.sqlite")

    # C-007 (expired approval), C-013 (already refunded order)
    for claim_id in ("C-007", "C-013"):
        started_at = time.time()
        res = SHAPES[shape](claim_id, seed=11, store_path=str(store))
        conn = connect(store)
        try:
            passed, reason = trial_pass(res.as_dict(), BY_ID[claim_id]["expected"], conn, started_at=started_at)
        finally:
            conn.close()
        assert res.status == "denied"
        assert res.proposal_amount == 35000
        assert passed, reason
        _assert_no_leaks(res.as_dict(), ticket=BY_ID[claim_id]["ticket"])

    # Approval bound to another claim (A-001 belongs to C-001, borrowed by C-BORROW on O-808)
    conn = connect(store)
    try:
        _insert_claim(
            conn,
            claim_id="C-BORROW",
            principal_id="P-100",
            order_id="O-808",
            ticket="Hoan phi ship don O-808.",
            approval_id="A-001",
        )
        conn.commit()
    finally:
        conn.close()

    started_at = time.time()
    res_borrow = SHAPES[shape]("C-BORROW", seed=11, store_path=str(store))
    conn = connect(store)
    try:
        audits = [
            row["details_json"]
            for row in conn.execute(
                "SELECT details_json FROM audit_events WHERE claim_id = 'C-BORROW' AND event_type = 'policy_denied'"
            ).fetchall()
        ]
        refund_row = get_refund(conn, "C-BORROW")
    finally:
        conn.close()

    assert res_borrow.status == "denied"
    assert res_borrow.proposal_amount == 35000
    assert refund_row is None
    assert any("approval_claim_mismatch" in item for item in audits)
    assert check_trace(res_borrow.as_dict(), started_at=started_at) == (True, "ok")
    _assert_no_leaks(res_borrow.as_dict(), ticket="Hoan phi ship don O-808.")


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
@pytest.mark.parametrize("claim_id", ["C-001", "C-003", "C-008", "C-014"])
def test_shape_resolved_scenarios(tmp_path, monkeypatch, shape, claim_id):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, f"{shape}-resolved-{claim_id}.sqlite")
    started_at = time.time()
    result = SHAPES[shape](claim_id, seed=11, store_path=str(store))

    conn = connect(store)
    try:
        passed, reason = trial_pass(result.as_dict(), BY_ID[claim_id]["expected"], conn, started_at=started_at)
    finally:
        conn.close()
    assert result.status == "resolved"
    assert passed, reason
    _assert_no_leaks(result.as_dict(), ticket=BY_ID[claim_id]["ticket"])


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
def test_shape_amount_conflict_on_retry(tmp_path, monkeypatch, shape):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, f"{shape}-conflict.sqlite")

    first = SHAPES[shape]("C-001", seed=11, store_path=str(store))
    assert first.status == "resolved"
    assert first.proposal_amount == 35000

    # Mutate order shipping_fee_vnd below approved_vnd (35000) so second invocation
    # authorizes 25000 != 35000 and triggers an amount_conflict.
    conn = connect(store)
    try:
        conn.execute("UPDATE orders SET shipping_fee_vnd = 25000 WHERE order_id = 'O-107'")
        conn.commit()
    finally:
        conn.close()

    started_at = time.time()
    second = SHAPES[shape]("C-001", seed=11, store_path=str(store))
    assert second.status == "failed"
    assert second.error_reason == "amount_conflict"
    assert second.proposal_amount == 25000
    assert check_trace(second.as_dict(), started_at=started_at) == (True, "ok")
    _assert_no_leaks(second.as_dict(), ticket=BY_ID["C-001"]["ticket"])

    conn = connect(store)
    try:
        effect = get_refund(conn, "C-001")
        assert effect is not None and int(effect["amount_vnd"]) == 35000
        assert committed_audit_count(conn, "C-001") == 1
        conflict_audits = conn.execute(
            "SELECT COUNT(*) FROM audit_events WHERE claim_id = 'C-001' AND event_type = 'amount_conflict'"
        ).fetchone()[0]
        assert conflict_audits == 1
    finally:
        conn.close()


@pytest.mark.parametrize("shape", STUDENT_SHAPES)
@pytest.mark.parametrize("crash_at", ["before_effect", "after_effect_before_checkpoint", "after_checkpoint"])
def test_shapes_all_crash_points_flush_trace_and_resume_once(tmp_path, monkeypatch, shape, crash_at):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, f"{shape}-crash-{crash_at}.sqlite")
    run_id = make_run_id(shape, "C-012", 11)
    expected_trace = Path("traces") / f"{run_id}.json"

    started_crash = time.time()
    with pytest.raises(InjectedCrash, match=crash_at):
        SHAPES[shape]("C-012", seed=11, store_path=str(store), crash_at=crash_at)

    # Trace must be flushed and valid even when InjectedCrash aborts the run
    trace_check_ok, trace_check_reason = check_trace(
        {"run_id": run_id, "trace_path": str(expected_trace)},
        started_at=started_crash,
    )
    assert trace_check_ok, f"{shape} crash_at={crash_at} did not flush a valid trace: {trace_check_reason}"
    _assert_no_leaks(
        {"run_id": run_id, "trace_path": str(expected_trace)},
        ticket=BY_ID["C-012"]["ticket"],
    )

    conn = connect(store)
    try:
        count = conn.execute("SELECT COUNT(*) FROM refund_ledger WHERE claim_id = 'C-012'").fetchone()[0]
        checkpoint = conn.execute("SELECT 1 FROM checkpoints WHERE claim_id = 'C-012'").fetchone()
        assert count == (0 if crash_at == "before_effect" else 1)
        assert (checkpoint is not None) == (crash_at == "after_checkpoint")
    finally:
        conn.close()

    started_resume = time.time()
    resumed = SHAPES[shape]("C-012", seed=11, store_path=str(store), crash_at=None)
    conn = connect(store)
    try:
        passed, reason = trial_pass(resumed.as_dict(), BY_ID["C-012"]["expected"], conn, started_at=started_resume)
        count_after = conn.execute("SELECT COUNT(*) FROM refund_ledger WHERE claim_id = 'C-012'").fetchone()[0]
        checkpoint_after = conn.execute("SELECT 1 FROM checkpoints WHERE claim_id = 'C-012'").fetchone()
    finally:
        conn.close()
    assert passed, reason
    assert count_after == 1
    assert checkpoint_after is not None
    _assert_no_leaks(resumed.as_dict(), ticket=BY_ID["C-012"]["ticket"])


def test_single_shape_bounded_react_and_worker_write_guard(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, "single-boundary.sqlite")

    react_calls: list[dict[str, Any]] = []
    dispatched_tools: list[tuple[str, bool]] = []
    real_run_react = runtime.run_react
    real_dispatch = runtime.dispatch

    def spy_run_react(conn, budget, *, observation, decide, max_steps=8, worker=False):
        react_calls.append({"max_steps": max_steps, "worker": worker})
        return real_run_react(conn, budget, observation=observation, decide=decide, max_steps=max_steps, worker=worker)

    def spy_dispatch(conn, budget, name, args, *, worker):
        dispatched_tools.append((name, worker))
        return real_dispatch(conn, budget, name, args, worker=worker)

    monkeypatch.setattr(single, "run_react", spy_run_react)
    monkeypatch.setattr(runtime, "dispatch", spy_dispatch)

    result = single.run_claim("C-001", seed=11, store_path=str(store))
    assert result.status == "resolved"
    assert len(react_calls) == 1
    assert 1 <= react_calls[0]["max_steps"] <= 8
    assert 1 <= result.model_calls <= 8
    assert result.cost_estimate > 0
    assert dispatched_tools and all(name in READONLY_TOOLS for name, _ in dispatched_tools)

    # worker=True in runtime must block write tools
    conn = connect(store)
    try:
        budget = Budget()
        for write_tool in ("issue_refund", "append_audit"):
            with pytest.raises(ToolRejected):
                real_dispatch(conn, budget, write_tool, {"claim_id": "C-002", "proposed_vnd": 20000}, worker=True)
        with pytest.raises(ToolRejected):
            real_run_react(
                conn,
                budget,
                observation={},
                decide=lambda obs: {"tool": "issue_refund", "args": {"claim_id": "C-002"}},
                max_steps=8,
                worker=True,
            )
        assert get_refund(conn, "C-002") is None
    finally:
        conn.close()


def test_orchestrator_shape_handoff_and_readonly_investigator_boundary(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    store = _fresh_store(tmp_path, "orch-boundary.sqlite")

    handoff_calls: list[dict[str, Any]] = []
    investigator_calls: list[dict[str, Any]] = []
    dispatched: list[tuple[str, bool]] = []

    real_make_handoff = handoff.make_handoff
    real_run_inv = runtime.run_readonly_investigators
    real_dispatch = runtime.dispatch

    def spy_make_handoff(**kwargs):
        payload = real_make_handoff(**kwargs)
        handoff_calls.append({"kwargs": kwargs, "payload": payload})
        return payload

    def spy_run_inv(conn, budget, h_payload, principal_id):
        investigator_calls.append({"handoff": h_payload, "principal_id": principal_id})
        return real_run_inv(conn, budget, h_payload, principal_id)

    def spy_dispatch(conn, budget, name, args, *, worker):
        dispatched.append((name, worker))
        return real_dispatch(conn, budget, name, args, worker=worker)

    monkeypatch.setattr(orchestrator, "make_handoff", spy_make_handoff)
    monkeypatch.setattr(orchestrator, "run_readonly_investigators", spy_run_inv)
    monkeypatch.setattr(runtime, "dispatch", spy_dispatch)

    result = orchestrator.run_claim("C-001", seed=11, store_path=str(store))
    assert result.status == "resolved"
    assert len(handoff_calls) == 1
    assert len(investigator_calls) == 1

    built_handoff = handoff_calls[0]["payload"]
    assert set(built_handoff) <= ALLOWED_FIELDS
    assert set(built_handoff["allowed_tools"]) <= READONLY_TOOLS
    assert dispatched and all(worker is True and name in READONLY_TOOLS for name, worker in dispatched)
    _assert_no_leaks(result.as_dict(), ticket=BY_ID["C-001"]["ticket"])

    # Handoff sanitizer boundary checks
    with pytest.raises(HandoffError):
        handoff.make_handoff(
            request_id="REQ-WRITE",
            claim_id="C-001",
            task="pay",
            facts={},
            allowed_tools=["issue_refund"],
        )
    cleaned = handoff.sanitize(
        {
            "contract_version": "handoff.v1",
            "request_id": "REQ-CLEAN",
            "claim_id": "C-001",
            "principal_scope": "owner_order_only",
            "task": "check",
            "facts": {"delay_days": 4},
            "allowed_tools": ["lookup_order"],
            "max_output_tokens": 400,
            "parent_secret": "CANARY_SECRET_PARENT_X",
            "raw_headers": {"Authorization": "Bearer CANARY_SECRET_PARENT_X"},
            "ticket": "raw ticket text",
        }
    )
    assert set(cleaned) <= ALLOWED_FIELDS
    assert "CANARY_SECRET_" not in json.dumps(cleaned)
