#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""AI phân tích & dự báo cho hệ thống GIÁM SÁT HẦM KÍN.

Đọc  /latest  và  /history  từ Firebase Realtime Database,
tính toán ngưỡng + xu hướng + dự báo + bất thường, rồi ghi kết quả
vào /predictions/latest  (đúng schema mà index.html đọc).

Chạy:
    python forecast.py                # đọc + ghi Firebase
    python forecast.py --dry-run      # chỉ đọc, in JSON ra stdout
    python forecast.py --out kq.json  # ghi thêm ra file (tiện debug)

Biến môi trường:
    FIREBASE_DB_URL     URL Realtime Database
    FIREBASE_DB_AUTH    token (nối vào ?auth=), để trống nếu rules public
    AI_OUTPUT_PATH      mặc định "predictions/latest"
    AI_LOOKBACK_HOURS   mặc định 24
    ENABLE_WEATHER      "0" để tắt gọi Open-Meteo
    SITE_LAT / SITE_LON / SITE_NAME / SITE_TZ  trạm so sánh thời tiết (mặc định Thái Nguyên)
    AI_BIAS_DECAY_MIN   hằng số tắt dần khi neo dự báo vào giá trị hiện tại (phút)
    AI_REVERT_MIN       hằng số hồi quy về mức nền 24 giờ (phút), mặc định 360

Mô hình dự báo:
    6 mốc cách nhau 2 giờ, xa nhất 12 giờ. Dùng hồi quy về trung vị 24 giờ
    (damped mean reversion) thay cho ngoại suy tuyến tính/ARIMA, vì ở tầm 12 giờ
    hai mô hình kia cho ra đường phẳng (mọi mốc trùng nhau) hoặc số vô lý.

Phụ thuộc:
    bắt buộc : numpy
    tuỳ chọn : scikit-learn (IsolationForest) -> thiếu thì dùng z-score.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import numpy as np

try:  # phát hiện bất thường đa chiều (tuỳ chọn — thiếu thì dùng z-score)
    from sklearn.ensemble import IsolationForest
    from sklearn.preprocessing import StandardScaler
except Exception:  # pragma: no cover - môi trường không có sklearn
    IsolationForest = None
    StandardScaler = None


# ============================ CẤU HÌNH ============================

DB_URL = os.environ.get(
    "FIREBASE_DB_URL",
    "https://khoitrongham-default-rtdb.asia-southeast1.firebasedatabase.app",
).rstrip("/")
DB_AUTH = os.environ.get("FIREBASE_DB_AUTH", "").strip()
OUTPUT_PATH = os.environ.get("AI_OUTPUT_PATH", "predictions/latest").strip("/")
LOOKBACK_HOURS = float(os.environ.get("AI_LOOKBACK_HOURS", "24"))
WEATHER_ENABLED = os.environ.get("ENABLE_WEATHER", "1") not in ("0", "false", "False", "")
# Trạm so sánh: Thái Nguyên (đổi bằng biến môi trường nếu cần)
SITE_NAME = os.environ.get("SITE_NAME", "Thái Nguyên")
SITE_TZ = os.environ.get("SITE_TZ", "Asia/Ho_Chi_Minh")
SITE_LAT = os.environ.get("SITE_LAT", "21.5942")
SITE_LON = os.environ.get("SITE_LON", "105.8481")
TIMEOUT = float(os.environ.get("AI_HTTP_TIMEOUT", "10"))
STALE_SEC = float(os.environ.get("AI_STALE_SEC", "180"))
# Dự báo được "neo" vào giá trị cảm biến hiện tại rồi tắt dần sai lệch,
# để điểm +1h không nhảy cóc so với số đang hiển thị (đơn vị: phút).
BIAS_DECAY_MIN = float(os.environ.get("AI_BIAS_DECAY_MIN", "120"))

SCHEMA_VERSION = 2

# Các mốc dự báo (phút kể từ điểm dữ liệu cuối cùng): cách nhau 2 giờ, xa nhất 12 giờ
HORIZONS = (120, 240, 360, 480, 600, 720)
BIN_MINUTES = HORIZONS[0]          # bước giữa hai cột (phút)
HORIZON_HOURS = HORIZONS[-1] // 60  # mốc xa nhất, dùng cho câu tóm tắt

# Ngưỡng mặc định. PHẢI KHỚP với ngưỡng trong firmware:
#   DUST_WARN_UG = 500  -> dust warn 500 / danger 800
#   MQ2_WARN_MV  = 1800 -> mq2  warn 1800 / danger 2500
# Có thể ghi đè mà không cần sửa code: /config/thresholds trên Firebase
#   { "temp": {"warn": 40, "danger": 50}, ... }
METRICS = {
    "temp": {
        "label": "Nhiệt độ",
        "unit": "°C",
        "dec": 1,
        "warn": 40.0,
        "danger": 50.0,
        "lo": -10.0,
        "hi": 120.0,
        "rate": 3.0,  # °C/giờ - chặn trên của tốc độ thay đổi khi dự báo
        "minSamples": 60,
    },
    "hum": {
        "label": "Độ ẩm",
        "unit": "%RH",
        "dec": 1,
        "warn": 80.0,
        "danger": 90.0,
        "lo": 0.0,
        "hi": 100.0,
        "rate": 10.0,  # %RH/giờ
        "minSamples": 60,
    },
    "dust": {
        "label": "Bụi",
        "unit": "µg/m³",
        "dec": 0,
        "warn": 500.0,
        "danger": 800.0,
        "lo": 0.0,
        "hi": 2000.0,
        "rate": 150.0,  # µg/m³/giờ
        "minSamples": 60,
    },
    "mq2": {
        "label": "Khói (MQ-2)",
        "unit": "mV",
        "dec": 0,
        "warn": 1800.0,
        "danger": 2500.0,
        "lo": 0.0,
        "hi": 5000.0,
        "rate": 400.0,  # mV/giờ
        "minSamples": 60,
    },
}
ORDER = ("temp", "hum", "dust", "mq2")

# Tên field cũ (firmware <= 1.0.1 ghi history bằng "t"/"h") -> vẫn đọc được
FIELD_ALIAS = {
    "temp": ("temp", "t"),
    "hum": ("hum", "h"),
    "dust": ("dust",),
    "mq2": ("mq2",),
}

LEVEL_TEXT = {0: "BÌNH THƯỜNG", 1: "CẢNH BÁO", 2: "NGUY HIỂM"}


def _force_utf8_console():
    """Windows mặc định dùng cp1252 -> in tiếng Việt sẽ crash; ép UTF-8."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_force_utf8_console()



# ============================ TIỆN ÍCH ============================


def _num(value):
    """Ép về float hữu hạn, sai/None -> None (Firebase hay trả null)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _pick(record, metric):
    for key in FIELD_ALIAS[metric]:
        if key in record:
            value = _num(record[key])
            if value is not None:
                return value
    return None


def _r(value, dec):
    return None if value is None else round(float(value), dec)


def _url(path, query=None):
    url = "{}/{}.json".format(DB_URL, path.strip("/"))
    parts = []
    if query:
        parts.append(query)
    if DB_AUTH:
        parts.append(urllib.parse.urlencode({"auth": DB_AUTH}))
    if parts:
        url += "?" + "&".join(parts)
    return url


def _request(method, url, payload=None):
    body = None
    headers = {"Accept": "application/json", "User-Agent": "giam-sat-ham-kin-ai/2"}
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json; charset=utf-8"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        raw = resp.read().decode("utf-8")
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def fetch_data(path, query=None):
    return _request("GET", _url(path, query))


def push_data(path, data):
    print("[AI] Đang ghi /{} ...".format(path))
    _request("PUT", _url(path), data)
    print("[AI] Đã ghi Firebase /{}".format(path))


# ============================ ĐỌC DỮ LIỆU ============================


def load_latest():
    data = fetch_data("latest")
    return data if isinstance(data, dict) else {}


def load_history(lookback_hours=LOOKBACK_HOURS):
    """Trả về list [{ts, temp, hum, dust, mq2}] đã sắp xếp theo thời gian."""
    since = int(time.time() - lookback_hours * 3600)
    query = urllib.parse.urlencode({"orderBy": '"$key"', "startAt": '"{}"'.format(since)})
    raw = fetch_data("history", query)
    records = []
    if isinstance(raw, dict):
        for key, item in raw.items():
            if not isinstance(item, dict):
                continue
            try:
                ts = int(float(key))
            except (TypeError, ValueError):
                continue
            rec = {"ts": ts}
            for metric in ORDER:
                rec[metric] = _pick(item, metric)
            records.append(rec)
    records.sort(key=lambda r: r["ts"])
    return records


def load_thresholds():
    """Ngưỡng mặc định + cho phép ghi đè từ /config/thresholds."""
    custom = {}
    try:
        raw = fetch_data("config/thresholds")
        if isinstance(raw, dict):
            custom = raw
    except Exception as exc:  # không có config thì bỏ qua
        print("[AI] Không đọc được /config/thresholds: {}".format(exc))

    thresholds = {}
    for metric in ORDER:
        spec = METRICS[metric]
        warn = spec["warn"]
        danger = spec["danger"]
        source = "firmware"
        entry = custom.get(metric) if isinstance(custom.get(metric), dict) else {}
        cw, cd = _num(entry.get("warn")), _num(entry.get("danger"))
        if cw is not None:
            warn = cw
            source = "config"
        if cd is not None:
            danger = cd
            source = "config"
        if danger <= warn:
            danger = warn * 1.25
        thresholds[metric] = {"warn": float(warn), "danger": float(danger), "source": source}
    return thresholds


# ============================ THỐNG KÊ ============================


def series_of(records, metric):
    return [(r["ts"], r[metric]) for r in records if r.get(metric) is not None]


def describe(values):
    arr = np.asarray(values, dtype=float)
    std = float(np.std(arr, ddof=1)) if arr.size > 1 else 0.0
    return {
        "n": int(arr.size),
        "mean": float(np.mean(arr)),
        "std": std,
        "min": float(np.min(arr)),
        "max": float(np.max(arr)),
        "p90": float(np.percentile(arr, 90)),
        "p99": float(np.percentile(arr, 99)),
    }


def zscore_of(value, stats):
    if value is None or stats is None or stats["std"] <= 1e-9:
        return None
    return (value - stats["mean"]) / stats["std"]


def trend_per_hour(series, window_min=60.0):
    """Độ dốc hồi quy tuyến tính trên `window_min` phút cuối, đơn vị /giờ."""
    if len(series) < 5:
        return None
    end_ts = series[-1][0]
    pts = [(t, v) for t, v in series if t >= end_ts - window_min * 60]
    if len(pts) < 5:
        return None
    x = np.array([t for t, _ in pts], dtype=float)
    y = np.array([v for _, v in pts], dtype=float)
    x = (x - x[0]) / 3600.0
    if float(np.ptp(x)) <= 0:
        return None
    slope = float(np.polyfit(x, y, 1)[0])
    return slope


# ============================ DỰ BÁO ============================

REVERT_MIN = float(os.environ.get("AI_REVERT_MIN", "360"))


def _revert_forecast(values, horizons):
    """Hồi quy về mức nền 24 giờ (damped mean reversion).

    Với tầm dự báo 2–12 giờ, cả ngoại suy tuyến tính lẫn ARIMA(1,1,1) đều cho
    ra đường gần như phẳng (mọi mốc trùng nhau) hoặc văng ra số vô lý khi chuỗi
    có cú nhảy. Mô hình ở đây: xuất phát từ mẫu cuối rồi tiến dần về trung vị
    24 giờ theo hàm mũ — nên mỗi mốc một khác, và không bao giờ ra ngoài dải
    đã từng đo.
    """
    last = float(values[-1])
    base = float(np.percentile(values, 50))
    return [base + (last - base) * math.exp(-h / REVERT_MIN) for h in horizons]


def forecast_metric(series, metric, horizons=HORIZONS, anchor=None):
    """Trả về (list giá trị dự báo cùng độ dài horizons, phương pháp).

    `anchor`: giá trị cảm biến hiện tại. Dự báo được neo vào giá trị này rồi
    tắt dần sai lệch (BIAS_DECAY_MIN), nhờ vậy mốc +2h không nhảy cóc so với
    số đang hiển thị trên dashboard.
    """
    spec = METRICS[metric]
    if len(series) < spec["minSamples"]:
        return None, "insufficient"

    values = np.array([v for _, v in series], dtype=float)
    raw = _revert_forecast(values, horizons)
    method = "mean-revert"

    # Neo vào giá trị hiện tại: lệch giữa "mẫu cuối của chuỗi" và "cảm biến bây giờ"
    # được cộng vào dự báo rồi tắt dần, để đường dự báo nối liền với số đang hiển thị.
    last = float(values[-1])
    offset = 0.0 if anchor is None else float(anchor) - last
    reference = last + offset                      # = anchor nếu có
    corrected = [
        value + offset * math.exp(-horizon / BIAS_DECAY_MIN)
        for horizon, value in zip(horizons, raw)
    ]

    # Kẹp tốc độ biến thiên: mỗi giờ không đổi quá `rate`
    limited = []
    for horizon, value in zip(horizons, corrected):
        span = spec["rate"] * horizon / 60.0
        limited.append(max(reference - span, min(reference + span, value)))

    # Kẹp vào DẢI ĐÃ QUAN SÁT trong 24h (p2..p98, mở rộng để chứa giá trị hiện tại)
    lo_obs = min(float(np.percentile(values, 2)), reference)
    hi_obs = max(float(np.percentile(values, 98)), reference)
    limited = [max(lo_obs, min(hi_obs, v)) for v in limited]

    # Kẹp về khoảng vật lý của cảm biến
    cleaned = [
        _r(max(spec["lo"], min(spec["hi"], v)), spec["dec"]) for v in limited
    ]
    return cleaned, method


# ============================ BẤT THƯỜNG ============================


def detect_anomaly(records, current):
    rows = []
    for rec in records:
        row = [rec.get(m) for m in ORDER]
        if all(v is not None for v in row):
            rows.append(row)

    result = {
        "isAnomaly": None,
        "method": "none",
        "score": None,
        "samples": len(rows),
        "reason": "",
    }
    if len(rows) < 30:
        result["reason"] = "chưa đủ 30 mẫu đủ 4 chỉ số"
        return result

    if any(current.get(m) is None for m in ORDER):
        result["reason"] = "thiếu chỉ số hiện tại"
        return result

    matrix = np.asarray(rows, dtype=float)
    vec = np.asarray([current[m] for m in ORDER], dtype=float)

    if IsolationForest is not None and StandardScaler is not None:
        try:
            scaler = StandardScaler().fit(matrix)
            model = IsolationForest(
                n_estimators=200, contamination=0.02, random_state=42
            ).fit(scaler.transform(matrix))
            scaled = scaler.transform([vec])
            result["isAnomaly"] = bool(int(model.predict(scaled)[0]) == -1)
            result["score"] = round(float(model.decision_function(scaled)[0]), 4)
            result["method"] = "isolation_forest"
            return result
        except Exception as exc:
            print("[AI] IsolationForest lỗi: {}".format(exc))

    mu = matrix.mean(axis=0)
    sd = matrix.std(axis=0, ddof=1)
    sd[sd <= 1e-9] = 1.0
    z = np.abs((vec - mu) / sd)
    result["isAnomaly"] = bool(float(z.max()) >= 4.0)
    result["score"] = round(float(z.max()), 2)
    result["method"] = "zscore"
    return result


# ============================ THỜI TIẾT ============================


def fetch_weather():
    ctx = {
        "ok": False,
        "city": SITE_NAME,
        "lat": _num(SITE_LAT),
        "lon": _num(SITE_LON),
        "tz": SITE_TZ,
        "cityTemp": None,
        "cityHum": None,
        "cityPm25": None,
        "cityPm25Forecast6h": None,
    }
    if not WEATHER_ENABLED:
        return ctx

    try:
        url = (
            "https://api.open-meteo.com/v1/forecast?latitude={}&longitude={}"
            "&current=temperature_2m,relative_humidity_2m&timezone={}".format(
                SITE_LAT, SITE_LON, urllib.parse.quote(SITE_TZ)
            )
        )
        data = _request("GET", url) or {}
        current = data.get("current") or {}
        ctx["cityTemp"] = _num(current.get("temperature_2m"))
        ctx["cityHum"] = _num(current.get("relative_humidity_2m"))
        ctx["ok"] = ctx["cityTemp"] is not None
    except Exception as exc:
        print("[AI] Lỗi lấy thời tiết: {}".format(exc))

    try:
        # Dùng UTC cho cả 2 phía để khỏi lệch múi giờ giữa runner và trạm
        url = (
            "https://air-quality-api.open-meteo.com/v1/air-quality?latitude={}&longitude={}"
            "&current=pm2_5&hourly=pm2_5&timezone=UTC&forecast_days=1".format(SITE_LAT, SITE_LON)
        )
        data = _request("GET", url) or {}
        ctx["cityPm25"] = _num((data.get("current") or {}).get("pm2_5"))
        hourly = data.get("hourly") or {}
        times = hourly.get("time") or []
        pm25 = hourly.get("pm2_5") or []
        now_hour = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:00")
        if now_hour in times:
            idx = times.index(now_hour)
            if ctx["cityPm25"] is None and idx < len(pm25):
                ctx["cityPm25"] = _num(pm25[idx])
            if idx + 6 < len(pm25):
                ctx["cityPm25Forecast6h"] = _num(pm25[idx + 6])
        ctx["ok"] = ctx["ok"] or ctx["cityPm25"] is not None
    except Exception as exc:
        print("[AI] Lỗi lấy chất lượng không khí: {}".format(exc))

    return ctx


# ============================ ĐÁNH GIÁ ============================


def evaluate(records, latest, current, anomaly, thresholds):
    reasons = []
    level = 0
    flagged = []

    for metric in ORDER:
        spec = METRICS[metric]
        value = current.get(metric)
        th = thresholds[metric]
        if value is None:
            continue
        text = "{value:.{dec}f}{unit}".format(
            value=value, dec=spec["dec"], unit=spec["unit"]
        )
        if value >= th["danger"]:
            level = 2
            flagged.append(metric)
            reasons.append(
                "{} {} vượt ngưỡng nguy hiểm ({:g}{})".format(
                    spec["label"], text, th["danger"], spec["unit"]
                )
            )
        elif value >= th["warn"]:
            level = max(level, 1)
            flagged.append(metric)
            reasons.append(
                "{} {} vượt ngưỡng cảnh báo ({:g}{})".format(
                    spec["label"], text, th["warn"], spec["unit"]
                )
            )

    # Lệch so với phân bố 24h (không thay thế ngưỡng tuyệt đối)
    for metric in ORDER:
        value = current.get(metric)
        series = series_of(records, metric)
        if value is None or len(series) < 60:
            continue
        stats = describe([v for _, v in series])
        z = zscore_of(value, stats)
        if z is not None and z >= 3.0 and metric not in flagged:
            level = max(level, 1)
            reasons.append(
                "{} cao hơn mức thường ngày {:.1f}σ".format(METRICS[metric]["label"], z)
            )

    if latest.get("alarm") is True:
        level = 2
        what = []
        if latest.get("smoke"):
            what.append("khói")
        if latest.get("dustAlarm"):
            what.append("bụi")
        reasons.append(
            "Thiết bị báo động: {}".format(" và ".join(what) if what else "vượt ngưỡng")
        )

    if latest.get("modbusOk") is False:
        level = max(level, 1)
        reasons.append("Mất kết nối RS485 với cảm biến nhiệt độ/độ ẩm")

    if anomaly.get("isAnomaly") is True:
        level = max(level, 1)
        reasons.append("Bất thường đa chiều: tổ hợp 4 chỉ số lệch khỏi mức thường ngày")

    if latest.get("warmup") is True:
        reasons.append("MQ-2 đang làm nóng, cảnh báo khói tạm tắt")

    if latest.get("noiseDust") is True:
        reasons.append("Tín hiệu bụi nhiễu, kết quả có thể sai lệch")

    return level, reasons


def build_summary(level, trends, forecasts):
    if level == 2:
        head = "Phát hiện tình trạng nguy hiểm trong hầm."
    elif level == 1:
        head = "Có chỉ số cần chú ý."
    else:
        head = "Tất cả các chỉ số trong ngưỡng an toàn."

    parts = [head]
    for metric in ("temp", "dust"):
        values = forecasts.get(metric) or []
        if len(values) < 2 or values[0] is None or values[-1] is None:
            continue
        spec = METRICS[metric]
        first, last = float(values[0]), float(values[-1])
        # Nói theo đúng đường dự báo (mốc đầu -> mốc cuối), không dùng độ dốc 1 giờ
        # để tránh câu "dự báo giảm" trong khi đường dự báo lại đi lên.
        scale = max(0.5, abs(first) * 0.03)
        if abs(last - first) < scale:
            word = "ổn định"
        else:
            word = "tăng" if last > first else "giảm"
        parts.append(
            "{} dự báo {} ({:.1f} → {:.1f}{} sau {} giờ).".format(
                spec["label"],
                word,
                first,
                last,
                spec["unit"],
                HORIZON_HOURS,
            )
        )
    return " ".join(parts)


# ============================ PIPELINE ============================


def analyse(dry_run=False, out_file=None):
    started = int(time.time())
    print("[AI] Bắt đầu phân tích (lookback {}h)".format(LOOKBACK_HOURS))

    latest = load_latest()
    if not latest:
        print("[AI] Không có /latest — bỏ qua lần chạy này.")
        return None

    records = load_history()
    thresholds = load_thresholds()

    current = {metric: _pick(latest, metric) for metric in ORDER}
    current_ts = None
    ts_raw = _num(latest.get("ts"))
    if ts_raw is not None:
        current_ts = ts_raw / 1000.0 if ts_raw > 1e11 else ts_raw  # Firestore trả ms
    age_sec = None if current_ts is None else max(0.0, time.time() - current_ts)

    forecasts = {}
    methods = {}
    trends = {}
    ready = {}
    for metric in ORDER:
        series = series_of(records, metric)
        values, method = forecast_metric(series, metric, anchor=current.get(metric))
        forecasts[metric] = values
        methods[metric] = method
        ready[metric] = values is not None
        trends[metric] = _r(trend_per_hour(series), 2) if series else None

    anomaly = detect_anomaly(records, current)
    weather = fetch_weather()
    level, reasons = evaluate(records, latest, current, anomaly, thresholds)
    summary = build_summary(level, trends, forecasts)

    # baseTs: mốc thời gian của điểm history cuối (để chart nối liền dự báo)
    base_ts = records[-1]["ts"] if records else int(current_ts or started)
    span_hours = 0.0
    if len(records) >= 2:
        span_hours = round((records[-1]["ts"] - records[0]["ts"]) / 3600.0, 2)

    payload = {
        "status": "ok" if records else "no-history",
        "schema": SCHEMA_VERSION,
        "generatedAt": started * 1000,
        "timestamp": started,
        "baseTs": int(base_ts),
        "binMinutes": BIN_MINUTES,
        "horizons": list(HORIZONS),
        "risk": {
            "level": level,
            "text": LEVEL_TEXT.get(level, "KHÔNG RÕ"),
            "reasons": reasons,
        },
        "summary": summary,
        "current": {m: _r(current[m], METRICS[m]["dec"]) for m in ORDER},
        "forecast": forecasts,
        "forecastMethod": methods,
        "trend": trends,
        "thresholds": thresholds,
        "anomaly": anomaly,
        "quality": {
            "samples": len(records),
            "coverageHours": span_hours,
            "ageSec": None if age_sec is None else round(age_sec, 1),
            "stale": bool(age_sec is not None and age_sec > STALE_SEC),
            "forecastReady": ready,
        },
        "device": {
            "fw": latest.get("fw"),
            "rssi": latest.get("rssi"),
            "uptime": latest.get("uptime"),
            "modbusOk": latest.get("modbusOk"),
            "alarm": latest.get("alarm"),
            "warmup": latest.get("warmup"),
        },
        "external": weather,
    }

    if out_file:
        with open(out_file, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print("[AI] Đã ghi {}".format(out_file))

    if dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        push_data(OUTPUT_PATH, payload)

    print(
        "[AI] Xong: level={} ({}), mẫu={}, dự báo={}".format(
            level,
            LEVEL_TEXT.get(level),
            len(records),
            "có" if any(ready.values()) else "không",
        )
    )
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description="AI phân tích hầm kín")
    parser.add_argument("--dry-run", action="store_true", help="không ghi Firebase, chỉ in JSON")
    parser.add_argument("--out", default=None, help="ghi kết quả ra file JSON")
    args = parser.parse_args(argv)
    try:
        payload = analyse(dry_run=args.dry_run, out_file=args.out)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        if exc.code in (401, 403):
            print("[AI] Firebase từ chối quyền: HTTP {} {}".format(exc.code, exc.reason),
                  file=sys.stderr)
            print("[AI] Nghĩa là Rules đang chặn ghi /predictions/latest khi chưa đăng nhập.",
                  file=sys.stderr)
            print("[AI] Sửa: Firebase Console → Realtime Database → Rules, thêm dòng:", file=sys.stderr)
            print('        "predictions": { ".read": true, ".write": true }', file=sys.stderr)
            print("[AI] (hoặc đặt secret FIREBASE_DB_AUTH và bỏ comment trong forecast.yml)",
                  file=sys.stderr)
        else:
            print("[AI] Lỗi HTTP {}: {} {}".format(exc.code, exc.reason, body), file=sys.stderr)
        return 2
    except urllib.error.URLError as exc:
        print("[AI] Lỗi mạng: {}".format(getattr(exc, "reason", exc)), file=sys.stderr)
        print("[AI] Kiểm tra FIREBASE_DB_URL và kết nối của runner.", file=sys.stderr)
        return 2
    return 0 if payload is not None else 1


if __name__ == "__main__":
    sys.exit(main())
