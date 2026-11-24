#!/usr/bin/env python3
"""Phân tích và dự báo từ /history của Firebase Realtime Database.

Chạy:  python forecast.py          (ghi kết quả lên /predictions/latest)
       python forecast.py --dry    (chỉ in kết quả, không ghi)
Biến môi trường: FIREBASE_DB_URL, FIREBASE_API_KEY, AI_EMAIL, AI_PASSWORD
"""
import json
import math
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np

KEYS = ["t", "h", "dust", "mq2"]
NAMES = {"t": "nhiệt độ", "h": "độ ẩm", "dust": "bụi", "mq2": "khói (MQ-2)"}
UNITS = {"t": "°C", "h": "%RH", "dust": "µg/m³", "mq2": "mV"}
DEC = {"t": 1, "h": 1, "dust": 0, "mq2": 0}
# (mức chú ý, mức cảnh báo): chỉnh theo thực tế hầm của bạn
THRESH = {"t": (33, 38), "h": (85, 95), "dust": (250, 500), "mq2": (1200, 1800)}
# biến động mỗi giờ nhỏ hơn mức này được coi là ổn định
FLAT = {"t": 0.3, "h": 1.5, "dust": 15, "mq2": 30}
LEVELS = ["Bình thường", "Cần chú ý", "Cảnh báo"]
BIN_MIN = 5         # gộp dữ liệu 5 phút một điểm
STEPS = 12          # 12 bước x 5 phút = dự báo 60 phút
MIN_BINS = 24       # cần ít nhất 2 giờ dữ liệu

DB = os.environ.get("FIREBASE_DB_URL", "").rstrip("/")
API_KEY = os.environ.get("FIREBASE_API_KEY", "")
AI_EMAIL = os.environ.get("AI_EMAIL", "")
AI_PASSWORD = os.environ.get("AI_PASSWORD", "")


# ---------------- Firebase (REST) ----------------

def http(method, url, body=None):
    data = None if body is None else json.dumps(body, allow_nan=False).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        raise RuntimeError("HTTP %d %s: %s" % (e.code, url.split("?")[0], e.read().decode()[:200]))


def fetch_history(now):
    since = int(now) - 86400
    q = urllib.parse.urlencode({"orderBy": '"$key"', "startAt": '"%d"' % since})
    return http("GET", "%s/history.json?%s" % (DB, q)) or {}


def fetch_latest():
    return http("GET", "%s/latest.json" % DB) or {}


def sign_in():
    url = "https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key=" + API_KEY
    r = http("POST", url, {"email": AI_EMAIL, "password": AI_PASSWORD, "returnSecureToken": True})
    return r["idToken"]


# ---------------- Xử lý chuỗi thời gian ----------------

def num(v):
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else float("nan")


def rnd(k, v):
    if v is None or not math.isfinite(v):
        return None
    return int(round(float(v))) if DEC[k] == 0 else round(float(v), DEC[k])


def cap(text):
    return text[0].upper() + text[1:]


def binned(ts, v, t0, nb):
    out = np.full(nb, np.nan)
    idx = ((ts - t0) // (BIN_MIN * 60)).astype(int)
    for b in range(nb):
        m = (idx == b) & ~np.isnan(v)
        if m.any():
            out[b] = v[m].mean()
    return out


def fill(y):
    ok = ~np.isnan(y)
    if ok.sum() < MIN_BINS or ok.mean() < 0.5:
        return None
    x = np.arange(len(y))
    return np.interp(x, x[ok], y[ok])


def holt_run(y, a, b, phi):
    level, trend = y[0], y[1] - y[0]
    preds = np.full(len(y), np.nan)
    for i in range(1, len(y)):
        f = level + phi * trend
        preds[i] = f
        new = a * y[i] + (1 - a) * f
        trend = b * (new - level) + (1 - b) * phi * trend
        level = new
    return preds, level, trend


def forecast_series(y):
    """Chọn mô hình tốt nhất giữa: giữ nguyên, Holt giảm xu hướng, hồi quy AR(6)."""
    n = len(y)
    last = float(y[-1])
    if np.ptp(y) < 1e-9:
        return {"name": "hằng số", "f": [last] * STEPS, "mae": 0.0, "maeNaive": 0.0}

    split = int(n * 0.8)
    test = np.arange(split, n)
    mae_naive = float(np.mean(np.abs(y[test] - y[test - 1])))
    cands = {"giữ nguyên": (mae_naive, [last] * STEPS)}

    # Holt giảm xu hướng: chọn tham số theo sai số dự báo 1 bước trên toàn chuỗi
    best, best_e = None, 1e18
    for a in (0.2, 0.5, 0.8):
        for b in (0.05, 0.2):
            for phi in (0.8, 0.95):
                p, _, _ = holt_run(y, a, b, phi)
                e = float(np.mean(np.abs(y[2:] - p[2:])))
                if e < best_e:
                    best, best_e = (a, b, phi), e
    a, b, phi = best
    p, level, trend = holt_run(y, a, b, phi)
    mae_h = float(np.mean(np.abs(y[test] - p[test])))
    fh = [level + sum(phi ** j for j in range(1, k + 1)) * trend for k in range(1, STEPS + 1)]
    cands["Holt"] = (mae_h, fh)

    # Hồi quy tự hồi quy (ridge) trên 6 giá trị gần nhất
    p_ar = 6
    if split >= p_ar + 12:
        mu = float(y[:split].mean())
        z = y - mu
        X = np.array([z[i - p_ar:i][::-1] for i in range(p_ar, split)])
        t = z[p_ar:split]
        w = np.linalg.solve(X.T @ X + 1.0 * np.eye(p_ar), X.T @ t)
        pred = np.array([w @ z[i - p_ar:i][::-1] for i in test])
        mae_ar = float(np.mean(np.abs(pred - z[test])))
        buf = list(z[-p_ar:])
        lo, hi = y.min() - np.ptp(y), y.max() + np.ptp(y)
        far = []
        for _ in range(STEPS):
            zh = float(w @ np.array(buf[-p_ar:][::-1]))
            buf.append(zh)
            far.append(float(np.clip(mu + zh, lo, hi)))
        cands["AR"] = (mae_ar, far)

    name = min(cands, key=lambda k: cands[k][0])
    return {"name": name, "f": cands[name][1], "mae": cands[name][0], "maeNaive": mae_naive}


def lvl(k, v):
    if v is None or not math.isfinite(v):
        return 0
    c, a = THRESH[k]
    return 2 if v >= a else 1 if v >= c else 0


def trend_text(k, per_hour):
    if abs(per_hour) < FLAT[k]:
        return "ổn định"
    return ("tăng %.1f" if per_hour > 0 else "giảm %.1f") % abs(per_hour) + " " + UNITS[k] + "/giờ"


# ---------------- Phân tích chính ----------------

def analyze(hist, latest, now=None):
    now = now or time.time()
    out = {"binMinutes": BIN_MIN, "steps": STEPS}
    rows = sorted((int(k), v) for k, v in (hist or {}).items() if isinstance(v, dict))
    if len(rows) < MIN_BINS * BIN_MIN * 0.5:
        out.update(status="not_enough_data",
                   risk={"level": 0, "text": LEVELS[0], "reasons": []},
                   summary="Chưa đủ dữ liệu để phân tích. Cần ít nhất khoảng 2 giờ dữ liệu trong /history.")
        return out

    ts = np.array([r[0] for r in rows], dtype=float)
    raw = {k: np.array([num(r[1].get(k)) for r in rows]) for k in KEYS}
    t0 = int(ts[0] // (BIN_MIN * 60) * (BIN_MIN * 60))
    nb = int((ts[-1] - t0) // (BIN_MIN * 60)) + 1

    stats, trend, current, quality, anomaly, fc, model = {}, {}, {}, {}, {}, {}, {}
    series = {}
    for k in KEYS:
        v = raw[k][~np.isnan(raw[k])]
        if len(v) == 0:
            quality[k] = "sparse"
            continue
        stats[k] = {"mean": rnd(k, v.mean()), "min": rnd(k, v.min()), "max": rnd(k, v.max()),
                    "std": rnd(k, v.std()), "n": int(len(v))}
        y = fill(binned(ts, raw[k], t0, nb))
        if y is None:
            quality[k] = "sparse"
        elif float(v.std()) < 1e-9 and len(v) >= MIN_BINS:
            quality[k] = "stuck"
            series[k] = y
        else:
            quality[k] = "ok"
            series[k] = y

    for k, y in series.items():
        current[k] = rnd(k, y[-1])
        m = min(len(y), 24)
        trend[k] = rnd(k, np.polyfit(np.arange(m), y[-m:], 1)[0] * (60 / BIN_MIN)) if m >= 6 else None
        med = float(np.median(y))
        mad = 1.4826 * float(np.median(np.abs(y - med)))
        anomaly[k] = bool(abs(y[-1] - med) / max(mad, FLAT[k] / 2) > 4)
        if quality[k] == "ok":
            r = forecast_series(y)
            fc[k] = [rnd(k, x) for x in r["f"]]
            model[k] = {"name": r["name"], "mae": round(r["mae"], 3), "maeNaive": round(r["maeNaive"], 3)}

    # ---- Đánh giá rủi ro ----
    risk, reasons = 0, []
    if latest and latest.get("alarm"):
        risk = 2
        reasons.append("Thiết bị đang báo cảnh báo khói hoặc bụi")
    for k in fc:
        now_v = current.get(k)
        lv_now = lvl(k, now_v)
        lv_f = max(lvl(k, v) for v in fc[k])
        if lv_now > 0:
            reasons.append("%s hiện %s %s, mức %s" % (cap(NAMES[k]), now_v, UNITS[k], LEVELS[lv_now].lower()))
        elif lv_f > 0:
            i = next(i for i, v in enumerate(fc[k]) if lvl(k, v) >= lv_f)
            reasons.append("Dự báo %s chạm mức %s sau khoảng %d phút" % (NAMES[k], LEVELS[lv_f].lower(), (i + 1) * BIN_MIN))
        if anomaly.get(k):
            reasons.append("%s đang lệch bất thường so với 24 giờ qua" % cap(NAMES[k]))
        risk = max(risk, lv_now, min(lv_f, 1))   # dự báo chỉ nâng tối đa lên "Cần chú ý"

    # ---- Tóm tắt bằng lời ----
    s = []
    parts = ["%s trung bình %s %s (từ %s đến %s)" % (NAMES[k], stats[k]["mean"], UNITS[k], stats[k]["min"], stats[k]["max"])
             for k in ("t", "h", "dust") if k in stats and quality.get(k) != "sparse"]
    if parts:
        s.append("Trong 24 giờ qua: " + "; ".join(parts) + ".")
    parts = ["%s %s" % (NAMES[k], trend_text(k, trend[k])) for k in fc if trend.get(k) is not None]
    if parts:
        s.append("Xu hướng 2 giờ gần nhất: " + "; ".join(parts) + ".")
    parts = ["%s khoảng %s %s" % (NAMES[k], fc[k][-1], UNITS[k]) for k in fc]
    if parts:
        s.append("Dự báo sau 60 phút: " + "; ".join(parts) + ".")
    bad = [NAMES[k] for k in KEYS if quality.get(k) in ("stuck", "sparse")]
    if bad:
        s.append("Chưa dự báo được %s do dữ liệu đứng yên hoặc thiếu (nghi cảm biến lỗi)." % ", ".join(bad))

    out.update(status="ok", baseTs=int(t0 + (nb - 1) * BIN_MIN * 60),
               risk={"level": risk, "text": LEVELS[risk], "reasons": reasons},
               summary=" ".join(s), stats24h=stats, trend=trend, current=current,
               forecast=fc, model=model, quality=quality, anomaly=anomaly)
    return out


def main():
    dry = "--dry" in sys.argv
    now = time.time()
    out = analyze(fetch_history(now), fetch_latest(), now)
    out["generatedAt"] = {".sv": "timestamp"}
    if dry:
        print(json.dumps(out, ensure_ascii=False, indent=2, allow_nan=False))
        return
    token = sign_in()
    http("PUT", "%s/predictions/latest.json?auth=%s" % (DB, token), out)
    print("Đã ghi /predictions/latest:", out["risk"]["text"], "|", out["status"])


if __name__ == "__main__":
    main()
