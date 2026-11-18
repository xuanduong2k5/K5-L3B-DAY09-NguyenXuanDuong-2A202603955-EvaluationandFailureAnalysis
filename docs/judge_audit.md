# Judge audit

Chạy: `python -m lab9.judge --csv <file> --audit 5`

| Tiêu chí | n | κ | Precision | Recall | Đạt FLOORS (κ≥0.6, P≥0.8, R≥0.7)? | Quyết định |
|---|---|---|---|---|---|---|
| tone_ok | 100 | 0.757 | 0.857 | 0.800 | Có | Dùng có giám sát; chỉ làm tín hiệu ngôn ngữ |
| helpful | 100 | 0.375 | 1.000 | 0.300 | Không | Loại khỏi gate; không đủ recall |

## ≥5 case audit thủ công
| # | Case | Judge nói | Người nói | Nhận định / hành động với rubric |
|---|---|---|---|---|
| 1 | H-008 | PASS | FAIL | False negative: xưng hô “em/anh” và “nha” lệch giọng hỗ trợ; thêm ví dụ văn phong quá thân mật vào rubric. |
| 2 | H-017 | PASS | FAIL | False negative: “bạn ơi … nha” không phù hợp xưng hô CSKH; yêu cầu lời chào/xưng hô nhất quán, lịch sự. |
| 3 | H-029 | PASS | FAIL | False negative: câu “vui lòng chờ” thiếu giải thích và bước tiếp theo; helpful phải có hành động cụ thể theo claim. |
| 4 | H-042 | FAIL | PASS | False positive: câu trả lời lịch sự, nêu số tiền và thời gian nhận tiền; thêm mẫu này làm ví dụ tone đạt. |
| 5 | H-051 | FAIL | PASS | False positive: câu dùng từ tiếng Anh “refund” nhưng vẫn lịch sự; không đánh đồng vay mượn từ ngữ với giọng điệu kém. |

## Kết luận
- `tone_ok` đạt floors: κ=0.757, P=0.857, R=0.800; observed fail rate 0.28 được hiệu chỉnh Rogan–Gladen thành 0.30.
- `helpful` bị loại: κ=0.375 và R=0.300 thấp hơn floors dù P=1.000; observed 0.09 hiệu chỉnh thành 0.30. Dùng code/nhãn người cho tiêu chí helpful.
- Rủi ro còn lại: judge chỉ được kiểm trên replay công khai, chưa có tập held-out do annotator độc lập gán nhãn.
