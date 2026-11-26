# Sơ đồ cho báo cáo – Giám sát hầm kín

Bộ hình vẽ từ chính mã nguồn trong repo (`sketch_nov20a.ino`, `ai/forecast.py`,
`index.html`), dùng cho phần "Thiết kế hệ thống" của báo cáo.

| Tệp | Nội dung | Gợi ý chú thích |
|---|---|---|
| `so-do-khoi.png` | Sơ đồ khối toàn hệ thống: cảm biến → ESP32 → Firebase → AI + Dashboard | *Hình 3.1. Sơ đồ khối hệ thống giám sát hầm kín* |
| `so-do-nguyen-ly.png` | Sơ đồ nguyên lý đấu nối: chân GPIO, nguồn, RS485, LED | *Hình 3.2. Sơ đồ nguyên lý đấu nối các khối chức năng* |
| `so-do-thuat-toan-firmware.png` | Lưu đồ chương trình firmware ESP32 | *Hình 3.3. Lưu đồ thuật toán chương trình firmware* |
| `so-do-thuat-toan-ota.png` | Lưu đồ riêng của thủ tục tự cập nhật firmware | *Hình 3.4. Lưu đồ thuật toán tự cập nhật firmware (OTA)* |
| `so-do-thuat-toan-ai.png` | Lưu đồ chương trình AI dự báo | *Hình 3.5. Lưu đồ thuật toán chương trình AI dự báo* |

Tất cả là PNG nền trắng, rộng 2240–3200 px (đủ nét khi in A4), tỉ lệ 1:1
không bị méo khi chèn vào Word.

## Số liệu nên trích trong phần thuyết minh

**Sơ đồ khối**
- 3 cảm biến: KTH2-R (nhiệt độ, độ ẩm, RS485/Modbus RTU), GP2Y1010AU0F (bụi,
  nguyên lý tán xạ ánh sáng), MQ-2 (khói/khí gas).
- ESP32 gửi `/latest` mỗi 10 s, `/history` mỗi 60 s (trung bình 1 phút) qua HTTPS
  có xác thực Firebase.
- GitHub Actions chạy `forecast.py` mỗi 30 phút, ghi `/predictions/latest`.
- Dashboard (GitHub Pages) đọc cả 3 nhánh và tự gọi Open-Meteo cho Thái Nguyên.
- OTA: thiết bị tự kiểm tra `version.json` mỗi 2 phút.

**Sơ đồ nguyên lý**

| Chức năng | GPIO | Ghi chú |
|---|---|---|
| MQ-2 (AO) | 35 | ADC1, chỉ vào |
| Bụi (Vo) | 34 | ADC1, chỉ vào |
| Bụi (V-LED) | 25 | xung 10 ms, R 150 Ω |
| RS485 RO / DI | 16 / 17 | RX2 / TX2 |
| RS485 DE/RE | 13 | HIGH khi truyền |
| LED cảnh báo | 26 | R 220 Ω xuống GND |

**Lưu đồ firmware**: chu kỳ đo 2 s; MQ-2 lấy 16 mẫu, bụi 20 mẫu (xung 10 ms,
lấy mẫu tại 280 µs); Modbus đọc thanh ghi `0x0300–0x0301`, thử 2 lần; MQ-2 làm
nóng 60 s; WiFi lỗi thì thử lại 20 s.

**Lưu đồ OTA**: kiểm tra 2 phút/lần; lỗi thì giãn 4 → 8 → 16 → 30 phút; NVS
`ota/tried` chống cập nhật lặp; manifest thêm `?t=<millis>` để phá cache CDN.

**Lưu đồ AI**: 24 giờ dữ liệu; ARIMA(1,1,1) (thiếu thư viện thì ngoại suy tuyến
tính); neo dự báo vào giá trị hiện tại rồi tắt dần; IsolationForest (fallback
z-score); ngưỡng mặc định trùng ngưỡng firmware (bụi 500/800 µg/m³, khói
1800/2500 mV).

## Lưu ý về "sơ đồ nguyên lý"

Hình `so-do-nguyen-ly.png` là **sơ đồ nguyên lý đấu nối dạng khối chức năng**:
mỗi module là một khối có tên chân thật, dây nối và ký hiệu nguồn/đất, kèm ghi
chú linh kiện (R 150 Ω, C 220 µF, trở hãm 120 Ω, R 220 Ω cho LED).

Nếu giảng viên yêu cầu bản vẽ **ký hiệu linh kiện rời** (điện trở zigzag, transistor,
IC MAX485 với từng chân), cần vẽ lại trong Altium / Proteus / KiCad / Fritzing —
bộ hình này không thay thế được bản vẽ CAD đó, nhưng dùng tốt cho phần mô tả
nguyên lý và đấu dây.

## Vẽ lại / sửa chữ

```bash
python ve_so_do.py            # vẽ cả 5 hình
python ve_so_do.py khoi       # chỉ vẽ sơ đồ khối
python ve_so_do.py nguyenly   # sơ đồ nguyên lý
python ve_so_do.py firmware   # lưu đồ firmware
python ve_so_do.py ota        # lưu đồ OTA
python ve_so_do.py ai         # lưu đồ AI
```

Yêu cầu: Python 3.8+ và Pillow. Toàn bộ chữ/kích thước nằm trong `ve_so_do.py`,
sửa trực tiếp rồi chạy lại (hình được vẽ ở hệ số phóng 2× nên nét luôn mượt).
