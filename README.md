# Giám sát hầm kín

Hệ thống đo và cảnh báo môi trường hầm kín: **ESP32 → Firebase → Dashboard + AI dự báo**.
Nhiệt độ, độ ẩm (Modbus RS485), bụi (GP2Y1010AU0F), khói (MQ-2) được lấy mẫu liên tục,
đẩy lên Firebase, hiển thị trên web và được một job AI phân tích/dự báo mỗi 30 phút.

```
┌──────────── ESP32 (sketch_nov20a.ino) ─────────────┐
│ KTH2-R (RS485/Modbus) → nhiệt độ, độ ẩm            │
│ GP2Y1010AU0F          → bụi                        │
│ MQ-2                  → khói                       │
│ LED cảnh báo + OTA qua HTTPS                       │
└───────────────┬────────────────────────────────────┘
                │ PUT /latest (10 s), /history (60 s) — Firebase Auth
                ▼
        Firebase Realtime Database (khoitrongham)
          ├── /latest                    ← trạng thái tức thời
          ├── /history/<epoch>           ← trung bình mỗi phút
          ├── /config/thresholds (tuỳ chọn)
          └── /predictions/latest        ← kết quả AI
                ▲                                │
                │ đọc 24 h                       ▼
   ┌────────────┴─────────────┐        ┌──────────────────────────┐
   │ GitHub Actions (30 phút) │        │ index.html (GitHub Pages)│
   │ ai/forecast.py           │        │ Chart.js + bảng dự báo   │
   └──────────────────────────┘        └──────────────────────────┘
```

## Cấu trúc repo

| Đường dẫn | Vai trò |
|---|---|
| `index.html` | Dashboard (GitHub Pages). Đọc `/latest`, `/history`, `/predictions/latest` |
| `esp32/sketch_nov20a/sketch_nov20a.ino` | Firmware ESP32 |
| `ai/forecast.py` | AI: thống kê, ARIMA, IsolationForest, sinh payload cho dashboard |
| `ai/test_forecast.py` | Kiểm thử end-to-end với Firebase giả lập (không cần mạng) |
| `firmware/firmware.bin` + `version.json` | Bản firmware phát hành cho OTA |
| `.github/workflows/forecast.yml` | Chạy AI mỗi 30 phút |
| `SCHEMA.md` | **Hợp đồng dữ liệu giữa 3 thành phần** — đọc trước khi sửa code |

## Đấu dây (khớp với firmware)

| Chức năng | GPIO | Ghi chú |
|---|---|---|
| MQ-2 (AO) | 35 | ADC1, chỉ input |
| Bụi Vo | 34 | ADC1, chỉ input |
| Bụi LED | 25 | LOW = bật LED hồng ngoại |
| RS485 RO | 16 | RX2 |
| RS485 DI | 17 | TX2 |
| RS485 DE/RE | 13 | điều khiển chiều truyền |
| LED cảnh báo | 26 | qua trở 220 Ω xuống GND |

KTH2-R: 9600 8N1, Modbus ID 1, thanh ghi `0x0300` (nhiệt độ), `0x0301` (độ ẩm), scale 0.1.

## 1. Chuẩn bị Firebase

1. Tạo project, bật **Realtime Database** (ví dụ region `asia-southeast1`).
2. Bật **Authentication → Email/Password**, tạo một tài khoản riêng cho thiết bị.
3. Lấy **Web API Key** (Project settings → General).
4. Rules tối thiểu (cho phép đọc công khai, ghi phải đăng nhập):

```json
{
  "rules": {
    "latest":      { ".read": true, ".write": "auth != null" },
    "history":     { ".read": true, ".write": "auth != null" },
    "config":      { ".read": true, ".write": "auth != null" },
    "predictions": { ".read": true, ".write": true }
  }
}
```

> `/predictions` phải cho ghi công khai vì `forecast.py` chạy trên GitHub Actions
> không có token người dùng. Nếu muốn siết, dùng `FIREBASE_DB_AUTH` (xem dưới).

## 2. Nạp firmware và cấu hình thiết bị

Không cần sửa file `.h` nào: cấu hình nằm trong bộ nhớ NVS của ESP32.
Nạp firmware bằng Arduino IDE, mở **Serial Monitor 115200, chọn "Newline"**, gõ:

```
SET ssid=TenWiFi
SET wpass=MatKhauWiFi
SET apikey=WebAPIKey
SET dburl=https://khoitrongham-default-rtdb.asia-southeast1.firebasedatabase.app
SET email=email_tai_khoan_esp32
SET fpass=mat_khau_tai_khoan_esp32
```

Sau khi đủ 6 khoá thiết bị tự khởi động lại và bắt đầu gửi dữ liệu.
Lệnh khác: `SHOW` (xem cấu hình), `HELP`, `OTA` (kiểm tra cập nhật ngay), `RESTART`.

`DUST_AUTO_CALIB` tự hiệu chuẩn `V0` cho cảm biến bụi mỗi lần khởi động
(đặt cảm biến trong không khí sạch lúc bật nguồn).

## 3. Dashboard (GitHub Pages)

Settings → Pages → Source: `main` / thư mục `/ (root)`.
Truy cập `https://<user>.github.io/giam-sat-ham-kin/`.

## 4. Chạy AI

### Trên GitHub Actions
`forecast.yml` chạy mỗi 30 phút, hoặc bấm **Run workflow** để chạy ngay.
Cần `numpy` (bắt buộc); `statsmodels`/`scikit-learn` nếu thiếu thì `forecast.py`
tự dùng phương pháp dự phòng (ngoại suy tuyến tính + z-score).

### Chạy thử ở máy
```bash
pip install numpy            # bắt buộc
pip install statsmodels scikit-learn   # tuỳ chọn

python ai/forecast.py --dry-run          # in JSON, KHÔNG ghi Firebase
python ai/forecast.py --out ketqua.json  # chạy thật + lưu bản sao
python ai/test_forecast.py               # test end-to-end, không cần mạng
```

### Biến môi trường

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `FIREBASE_DB_URL` | project `khoitrongham` | URL database |
| `FIREBASE_DB_AUTH` | rỗng | token nối vào `?auth=` nếu rules không cho ghi công khai |
| `AI_OUTPUT_PATH` | `predictions/latest` | đường dẫn ghi kết quả |
| `AI_LOOKBACK_HOURS` | `24` | số giờ dữ liệu dùng để học |
| `ENABLE_WEATHER` | `1` | `0` để không gọi Open-Meteo |
| `SITE_NAME` | `Thái Nguyên` | tên trạm so sánh, ghi vào `external.city` |
| `SITE_LAT`, `SITE_LON` | 21.5942, 105.8481 | toạ độ Thái Nguyên |
| `SITE_TZ` | `Asia/Ho_Chi_Minh` | múi giờ dùng khi hỏi Open-Meteo |
| `AI_BIAS_DECAY_MIN` | `120` | hằng số tắt dần khi neo dự báo vào giá trị hiện tại |
| `AI_STALE_SEC` | `180` | quá bao lâu thì coi `/latest` là cũ |

## 5. AI làm gì

| Bước | Cách làm |
|---|---|
| Ngưỡng | Ngưỡng tuyệt đối lấy từ `METRICS` (khớp firmware), ghi đè được qua `/config/thresholds` |
| Ngưỡng thống kê | So `current` với phân bố 24 h; lệch ≥ 3σ thì cảnh báo mức 1 |
| Bất thường đa chiều | IsolationForest (thiếu sklearn → z-score trên 4 chỉ số) |
| Dự báo | ARIMA(1,1,1) trên chuỗi 24 h; lỗi/thiếu thư viện → ngoại suy tuyến tính có giảm dần |
| **Neo vào hiện tại** | Dự báo cộng thêm chênh lệch giữa cảm biến bây giờ và mẫu cuối của chuỗi, rồi tắt dần (`AI_BIAS_DECAY_MIN`) → điểm +1h **không nhảy cóc** so với số đang hiển thị |
| Chặn phi lý | Dự báo bị kẹp theo tốc độ thay đổi tối đa (`rate`) và khoảng vật lý của cảm biến |
| Cảnh báo thiết bị | `alarm`, `smoke`, `dustAlarm`, `modbusOk`, `warmup`, `noiseDust` từ `/latest` |
| Ngữ cảnh | Nhiệt độ/độ ẩm/PM2.5 của Thái Nguyên (Open-Meteo) để so sánh |

Kết quả: `risk.level` 0/1/2 + danh sách lý do bằng tiếng Việt + bảng dự báo 1→6 giờ.

### Dashboard và thời tiết

Thẻ "So sánh với Thái Nguyên" do **trình duyệt tự gọi Open-Meteo** (10 phút/lần),
nên thời tiết vẫn lên dù job AI chưa chạy. Số liệu AI lấy được chỉ dùng làm dự
phòng khi trình duyệt bị chặn. Cột **"Hiện tại"** trong bảng dự báo cũng đọc
trực tiếp `/latest` (cập nhật 5 giây/lần) chứ không lấy ảnh chụp trong payload AI,
nên luôn khớp với 4 thẻ cảm biến phía trên.

## 6. Phát hành firmware mới (OTA)

Thiết bị **tự kiểm tra `firmware/version.json` mỗi 2 phút** (lần đầu sau khi khởi
động là 30 giây); nếu `version` khác `FW_VERSION` đang chạy thì tải
`firmware/firmware.bin` và tự cập nhật, rồi khởi động lại.

Cơ chế an toàn đi kèm:

| Tình huống | Hành vi |
|---|---|
| Kiểm tra OK, không có bản mới | Lặp lại sau **2 phút** |
| Lỗi mạng / GitHub không trả lời / tải hỏng | Giãn dần **4 → 8 → 16 → 30 phút** (tối đa), không đập liên tục |
| Đã cập nhật lên bản đó nhưng `FW_VERSION` chưa đổi | Ghi vào NVS `ota/tried`, bỏ qua để không lặp vô hạn |
| Muốn kiểm tra ngay | Gõ `OTA` ở Serial Monitor |

> **Lưu ý về cache:** GitHub Pages/CDN có thể cache `version.json` vài phút, nên
> firmware gắn thêm `?t=<millis>` để phá cache. Dù vậy, sau khi push bản mới,
> thiết bị có thể cần 2–10 phút mới nhận ra.
>
> **Lưu ý về tài nguyên:** mỗi lần kiểm tra là một request HTTPS (~1–3 giây).
> Request manifest dùng timeout riêng **5 giây** để không chặn vòng đo quá lâu;
> nếu mạng kém, một vài mẫu đo có thể bị bỏ qua trong lúc kiểm tra.

Thứ tự bắt buộc:

```powershell
# 1. Sửa code, ĐỔI FW_VERSION trong sketch_nov20a.ino (ví dụ "1.0.3")
# 2. Arduino IDE → Sketch → Export Compiled Binary
# 3. Chép file .bin vào firmware/
Copy-Item .\esp32\sketch_nov20a\build\esp32.esp32.esp32\sketch_nov20a.ino.bin .\firmware\firmware.bin -Force
# 4. Tính MD5 và điền vào version.json
Get-FileHash .\firmware\firmware.bin -Algorithm MD5 | Format-List
# 5. Commit + push cả firmware.bin và version.json (bin TRƯỚC, version sau)
```

`md5` để trống cũng được (firmware bỏ qua kiểm tra), nhưng nên điền để phát
hiện file tải hỏng.

## 7. Đã sửa gì ở bản v2 (đồng bộ 3 phía)

| # | Lỗi cũ | Hệ quả | Cách sửa |
|---|---|---|---|
| 1 | `forecast.py` ghi `/ai_analysis`, `index.html` đọc `/predictions/latest` | Bảng AI không bao giờ có dữ liệu | Thống nhất `/predictions/latest` |
| 2 | History ghi `t`/`h`, `forecast.py` tìm `temp`/`hum` | Luôn trả `None` → không cảnh báo, không dự báo | Firmware ghi `temp`/`hum`; Python/JS vẫn đọc được `t`/`h` cũ |
| 3 | `forecast.py` trả `{value, trend, confidence}`, JS đọc mảng `f[1]`, `f[5]` | Bảng dự báo trống, `undefined` | Payload v2: `forecast[metric] = [6 số]` + `horizons` |
| 4 | JS hard-code cột `+30 phút`, `+6 giờ` sai chỉ số mảng | Nhãn cột lệch giá trị | Cột sinh động từ `horizons` |
| 5 | `forecast.py` không trả `latest` nhưng JS cần `data.latest` | Cột "Hiện tại" trống | Thêm `current` |
| 6 | `.gitignore.txt` (sai tên) | `build/` 35 MB và `secrets.h` bị commit | Đổi thành `.gitignore` |
| 7 | Ngưỡng AI (bụi 150, khói 1000) ≠ ngưỡng firmware (500, 1800) | Thiết bị và AI cảnh báo lệch nhau | Đặt ngưỡng AI bằng ngưỡng firmware |
| 8 | `confidence = 100 - aic/10` | Số vô nghĩa | Bỏ, dùng `trend` + `quality` thật |
| 9 | PM2.5 lấy giờ theo UTC của runner | Luôn `None` do lệch múi giờ | Dùng `timezone=UTC` cho cả hai phía |
| 10 | In tiếng Việt trên Windows (cp1252) | `UnicodeEncodeError` | Ép stdout UTF-8 |
| 11 | ARIMA trên chuỗi ngắn/phẳng | Crash hoặc số vô lý | Bắt exception + fallback + kẹp tốc độ |
| 12 | OTA kiểm tra mỗi 6 giờ, không có backoff, bị CDN cache | Phát hành bản mới phải chờ lâu; lỗi mạng thì thử lại vô ích | Kiểm tra **mỗi 2 phút**, backoff 4→30 phút khi lỗi, thêm `?t=` phá cache |

Hợp đồng dữ liệu đầy đủ: xem [SCHEMA.md](SCHEMA.md).

## 8. Checklist chuyển từ bản cũ

```bash
# 1. Bỏ file .gitignore sai tên
git mv .gitignore.txt .gitignore      # (hoặc tạo mới rồi xoá file cũ)

# 2. Gỡ artifact build khỏi git (vẫn giữ trên đĩa)
git rm -r --cached esp32/sketch_nov20a/build

# 3. secrets.h không còn dùng (cấu hình đã chuyển vào NVS)
git rm --cached esp32/sketch_nov20a/secrets.h

# 4. Nếu đã từng commit mật khẩu thật: ĐỔI mật khẩu WiFi/Firebase API key
#    rồi xoá file khỏi lịch sử (git filter-repo) — chỉ xoá file là chưa đủ.

# 5. Nạp firmware 1.0.2 (history đổi sang temp/hum) rồi chạy lại AI
```

Dữ liệu `/history` cũ dùng `t`/`h` không cần xoá — vẫn đọc được.

## 9. Sự cố thường gặp

| Triệu chứng | Nguyên nhân thường gặp |
|---|---|
| Dashboard "Chưa có dữ liệu trong /latest" | Sai `dburl`, rules chặn, hoặc thiết bị chưa đủ 6 khoá cấu hình |
| Số liệu đứng im, "Thiết bị ngoại tuyến" | Mất WiFi; xem Serial log `[FB]`, `[WiFi]`; DNS router trả `0.0.0.0` (firmware đã tự đổi sang 8.8.8.8) |
| Nhiệt độ/độ ẩm `—` | `modbusOk=false` → kiểm tra A/B của RS485, địa chỉ Modbus, trở 120 Ω |
| Bụi luôn 0 hoặc rất lớn | Hiệu chuẩn `V0` sai: bật nguồn khi có bụi; xem log `Hieu chuan bui` |
| Khói luôn báo động | MQ-2 chưa qua 60 s làm nóng, hoặc `MQ2_WARN_MV` quá thấp |
| Bảng AI trống | Chưa chạy GitHub Actions, hoặc job lỗi (xem log Actions) |
| Dự báo `—` | Chưa đủ 60 mẫu history (≥ 1 giờ dữ liệu) |
| OTA lặp lại | Quên đổi `FW_VERSION`, hoặc `version.json` trỏ tới file `.bin` cũ |
| Push bản mới nhưng thiết bị không cập nhật | Log `[OTA] Dang chay x, ban moi nhat x` = thiết bị vẫn thấy bản cũ → CDN còn cache (chờ vài phút) hoặc chưa push `version.json` |
| Log `[OTA] Loi, thu lai sau N giay` | Lỗi mạng khi đọc manifest; N tăng dần 240 → 1800 giây, tự về 120 khi mạng OK lại |

## 10. Kiểm thử

```bash
python ai/test_forecast.py        # 7 nhóm test, không cần mạng
python ai/test_forecast.py -v     # in cả payload JSON
```

Test dựng một HTTP server giả lập Firebase, sinh 24 h dữ liệu tổng hợp (lẫn cả
khoá cũ `t`/`h`), chạy `forecast.py` như thật rồi kiểm tra: schema, đủ 4 chỉ số,
khoảng giá trị hợp lý, ngưỡng khớp firmware, đường đi ghi Firebase, xử lý
`alarm`/`stale`, trường hợp chưa có dữ liệu, ghi đè ngưỡng, và `--dry-run`.
