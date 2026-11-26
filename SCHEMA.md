# SCHEMA DỮ LIỆU — Giám sát hầm kín (v2)

Tài liệu này là **hợp đồng dữ liệu** giữa 3 thành phần. Sửa một bên thì phải
sửa theo tài liệu này, nếu không dashboard sẽ im lặng hiển thị `—`.

```
ESP32 (firmware)                 GitHub Actions (AI)              Trình duyệt
─────────────────                ───────────────────              ───────────
ghi  /latest        ─────┐
ghi  /history       ─────┼──→  đọc /latest, /history
                          │     ghi /predictions/latest  ──────→  đọc cả 3
                          │     (tuỳ chọn) đọc /config/thresholds
```

## 0. Quy ước chung

| Quy ước | Giá trị |
|---|---|
| Tên chỉ số | `temp`, `hum`, `dust`, `mq2` (**dùng ở mọi nơi**) |
| Đơn vị | °C, %RH, µg/m³, mV |
| `ts` trong `/latest`, `/history` | **mili-giây** (Firebase `{".sv":"timestamp"}`) |
| Khoá của `/history/<key>` | **giây** epoch (UTC), cũng là mốc thời gian của mẫu |
| `generatedAt` trong payload AI | **mili-giây** (`Date.now()` của JS dùng trực tiếp) |
| `timestamp`, `baseTs` trong payload AI | **giây** epoch (khi cần tính toán) |
| Độ chính xác | temp 1 chữ số thập phân, hum 1, dust 0, mq2 0 |

Giá trị "không đo được" luôn là `null` (không dùng `0`, không dùng chuỗi `""`).
Bên đọc phải chịu được `null` ở mọi field.

## 1. `/latest` — firmware ghi mỗi 10 giây

| Field | Kiểu | Ghi chú |
|---|---|---|
| `ts` | number (ms) | server timestamp |
| `temp` | number \| null | °C, `null` khi mất RS485 |
| `hum` | number \| null | %RH |
| `dust` | number \| null | µg/m³ |
| `mq2` | number \| null | mV |
| `smoke` | bool | MQ-2 vượt ngưỡng khói |
| `dustAlarm` | bool | bụi vượt ngưỡng |
| `alarm` | bool | `smoke \|\| dustAlarm` |
| `warmup` | bool | MQ-2 còn trong 60 s làm nóng |
| `noiseDust`, `noiseMq2` | bool | tín hiệu dao động bất thường |
| `modbusOk` | bool | đọc được cảm biến nhiệt độ/độ ẩm |
| `fw` | string | phiên bản firmware, ví dụ `"1.0.2"` |
| `rssi` | number | dBm |
| `uptime` | number | giây |

## 2. `/history/<epochSec>` — firmware ghi mỗi 60 giây (trung bình 1 phút)

| Field | Kiểu | Ghi chú |
|---|---|---|
| `ts` | number (ms) | server timestamp |
| `temp` | number \| null | trung bình cộng trong phút |
| `hum` | number \| null | |
| `mq2` | number \| null | |
| `dust` | number \| null | |
| `al` | 0 \| 1 | trong phút có cảnh báo |
| `wu` | 0 \| 1 | trong phút có warm-up |

> **Tương thích ngược:** firmware ≤ 1.0.1 ghi `t` / `h` thay cho `temp` / `hum`.
> `ai/forecast.py` và `index.html` vẫn đọc được cả hai khoá
> (`FIELD_ALIAS` trong Python, `ALIAS` trong JS). Dữ liệu cũ không cần xoá.

## 3. `/predictions/latest` — AI ghi mỗi 30 phút

Đây là payload **duy nhất** dashboard đọc. Bản cũ ghi vào `/ai_analysis` với
cấu trúc khác — đã bỏ.

```jsonc
{
  "status": "ok",              // "ok" | "no-history"
  "schema": 2,                 // tăng khi thay đổi cấu trúc
  "generatedAt": 1732600000000,// ms — dashboard hiển thị "Cập nhật lúc ..."
  "timestamp": 1732600000,     // s
  "baseTs": 1732599800,        // s — mốc của điểm history cuối; chart nối dự báo từ đây
  "binMinutes": 60,            // mỗi phần tử forecast cách nhau bao nhiêu phút
  "horizons": [60,120,180,240,300,360], // phút kể từ baseTs

  "risk": {
    "level": 0,                // 0 = bình thường, 1 = cảnh báo, 2 = nguy hiểm
    "text": "BÌNH THƯỜNG",     // nhãn hiển thị
    "reasons": []              // mỗi phần tử là 1 câu tiếng Việt
  },
  "summary": "…",              // 1-3 câu tóm tắt

  "current": { "temp": 36.5, "hum": 74.0, "dust": 260, "mq2": 1150 },
  // ^ Ảnh chụp tại thời điểm AI chạy. Dashboard KHÔNG dùng số này cho cột
  //   "Hiện tại" — nó đọc /latest trực tiếp (5 giây/lần) để luôn khớp cảm biến.
  //   Vẫn phải giữ field này để bản ghi cũ/script khác còn đọc được.

  // Dự báo theo từng mốc trong "horizons"; null nếu thiếu dữ liệu.
  // Dự báo được NEO vào current rồi tắt dần sai lệch, nên forecast[m][0]
  // luôn nằm trong ±rate của current — không nhảy cóc so với dashboard.
  "forecast": {
    "temp": [27.2, 27.4, 27.6, 27.7, 27.8, 27.9],
    "hum":  [72.1, 71.7, 71.5, 71.3, 71.1, 71.0],
    "dust": [174, 181, 186, 190, 193, 196],
    "mq2":  [800, 796, 794, 791, 790, 789]
  },
  "forecastMethod": { "temp": "arima", "hum": "arima" }, // "arima" | "linear" | "insufficient"

  "trend": { "temp": 0.43, "hum": -0.6, "dust": 18.44, "mq2": -8.72 }, // đơn vị/giờ
  "thresholds": {
    "temp": { "warn": 40.0, "danger": 50.0, "source": "firmware" },
    "hum":  { "warn": 80.0, "danger": 90.0, "source": "firmware" },
    "dust": { "warn": 500.0, "danger": 800.0, "source": "firmware" },
    "mq2":  { "warn": 1800.0, "danger": 2500.0, "source": "firmware" }
  },
  "anomaly": {
    "isAnomaly": false,
    "method": "isolation_forest", // "isolation_forest" | "zscore" | "none"
    "score": 0.05,
    "samples": 1440,
    "reason": ""
  },
  "quality": {
    "samples": 1440,             // số mẫu history dùng để học
    "coverageHours": 23.98,
    "ageSec": 0.6,               // /latest cũ bao lâu
    "stale": false,              // ageSec > 180
    "forecastReady": { "temp": true, "hum": true, "dust": true, "mq2": true }
  },
  "device": {
    "fw": "1.0.2", "rssi": -58, "uptime": 4321,
    "modbusOk": true, "alarm": false, "warmup": false
  },
  "external": {
    "ok": true,
    "city": "Thái Nguyên",       // trạm so sánh (SITE_NAME)
    "lat": 21.5942, "lon": 105.8481,
    "tz": "Asia/Ho_Chi_Minh",
    "cityTemp": 31.2, "cityHum": 68,
    "cityPm25": 42.0, "cityPm25Forecast6h": 55.0
  }
}
```

### Đọc payload này ở đâu trong `index.html`

| Phần giao diện | Khoá dùng |
|---|---|
| Nhãn rủi ro `#ai-risk` | `risk.level`, `risk.text` |
| Danh sách lý do | `risk.reasons` |
| Câu tóm tắt | `summary` |
| "Cập nhật lúc …" | `generatedAt` |
| Cột *Hiện tại* | `/latest` (ưu tiên, cập nhật 5 s) → dự phòng `current`; tô màu theo `thresholds` |
| Cột `+1h … +6h` | `horizons` + `forecast[metric]` |
| Cột *Xu hướng* | `trend[metric]` |
| Đường nét đứt trên biểu đồ | `forecast[metric]`, `baseTs`, `binMinutes` |
| Ghi chú cuối bảng | `quality`, `anomaly.method`, `forecastMethod` |
| Thẻ "So sánh với thành phố" | `external` |

## 4. `/config/thresholds` — tuỳ chọn, do người dùng đặt

Ghi đè ngưỡng mà **không cần sửa code**. Thiếu path này thì dùng giá trị mặc
định trong `ai/forecast.py`.

```json
{
  "dust": { "warn": 300, "danger": 600 },
  "mq2":  { "warn": 1500 }
}
```

Giá trị `source` trong payload sẽ là `"config"` cho chỉ số được ghi đè.
Lưu ý: **firmware vẫn dùng ngưỡng biên dịch cứng** (`DUST_WARN_UG`,
`MQ2_WARN_MV`) — muốn đèn trên thiết bị đổi theo thì phải sửa và nạp lại
firmware. Vì vậy mặc định ngưỡng trong `forecast.py` được đặt **bằng đúng**
ngưỡng firmware.

## 5. Ngưỡng mặc định (phải khớp 3 nơi)

| Chỉ số | warn | danger | Nơi định nghĩa |
|---|---|---|---|
| `temp` | 40 °C | 50 °C | `forecast.py` `METRICS` |
| `hum` | 80 %RH | 90 %RH | `forecast.py` `METRICS` |
| `dust` | 500 µg/m³ | 800 µg/m³ | `sketch_nov20a.ino` `DUST_WARN_UG` + `forecast.py` |
| `mq2` | 1800 mV | 2500 mV | `sketch_nov20a.ino` `MQ2_WARN_MV` + `forecast.py` |

## 6. Quy tắc khi sửa schema

1. **Thêm** field mới → được phép, không cần tăng `schema`; bên đọc bỏ qua field lạ.
2. **Đổi tên / xoá** field → tăng `schema`, cập nhật cả 3 nơi + tài liệu này.
3. Không bao giờ ghi `undefined`/`NaN` lên Firebase — dùng `null`.
4. Không đổi đơn vị của một field đã tồn tại (tạo field mới thay thế).
