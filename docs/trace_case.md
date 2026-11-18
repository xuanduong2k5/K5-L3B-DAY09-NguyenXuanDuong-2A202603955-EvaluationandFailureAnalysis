# Trace case: từ trace tới quyết định sai đầu tiên

Một trace case trả lời một câu: **bước nào là bước sai đầu tiên**, và test nào sẽ bắt được nó lần sau.

```bash
cd src
python -m lab9.trace_case --shape seeded_regression --claim C-002 --seed 11
python -m lab9.trace_case --shape fixed --claim C-002 --seed 11   # đối chứng
```

Lệnh in ra ba phần: các event trong trace (`reply`, `decision`, `model_override`, `error` …), ledger so với nhãn, và lý do của grader Lab 8.

## Case đã phân tích

| Trường | Bằng chứng |
|---|---|
| Trial | `seeded_regression C-014@11`; report `E-seeded_regression-20261118-ca6c` |
| Triệu chứng | `ProposalAmountCorrect` và `RefundAmountCorrect` fail. Reply nói 120.000đ, ledger chỉ ghi 90.000đ; nhãn mong đợi 120.000đ. |
| Bước sai đầu tiên | Event 5 `proposal`: `amount_vnd=120000`. |
| Vì sao là bước *đầu tiên* | Event 2 lookup đúng order; event 3 quote đúng 150.000đ; event 4 đọc prior refund 30.000đ. Agent đã tự trừ prior refund tại event 5, rồi host tiếp tục áp trần lịch sử khi ghi ledger. |
| Failure mode | FM-05 `pre_deducted` (FM-08 cũng được phát hiện vì reply không khớp ledger). |
| Đối chứng | `fixed C-014@11` giữ proposal theo quote; grader kiểm proposal và ledger đều pass. |
| Regression test | `test_seeded_regression_trace_is_detected_and_fixed_passes`: chạy lại đúng claim@seed, assert cả hai grader fail trên variant lỗi và pass trên `fixed`. |
| Chưa chắc chắn | MockLLM/SQLite replay không đo được tần suất lỗi trên model hoặc dữ liệu production. |

Lệnh tái tạo:
```bash
python -m lab9.trace_case --shape seeded_regression --claim C-014 --seed 11
python -m lab9.trace_case --shape fixed --claim C-014 --seed 11
```

## Quy tắc

- Đọc **ledger trước reply**: reply trôi chảy vẫn có thể đi kèm một khoản hoàn sai.
- "Bước sai đầu tiên" phải dẫn số event cụ thể, không phải "agent bị nhầm".
- Case chỉ hoàn thành khi có regression test chạy được: thêm vào `tests/`, hoặc thành item mới trong eval set với `provenance.from_trace`.
