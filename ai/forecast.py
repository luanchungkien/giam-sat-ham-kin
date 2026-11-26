
import os
import time
import math
import requests
import numpy as np
import pandas as pd
from datetime import datetime
from statsmodels.tsa.arima.model import ARIMA
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

# ===== CẤU HÌNH =====
# Lấy từ biến môi trường của GitHub Actions (để bảo mật)
FIREBASE_DB_URL = os.environ.get("FIREBASE_DB_URL", "https://khoitrongham-default-rtdb.asia-southeast1.firebasedatabase.app")

DEFAULT_THRESHOLDS = {
    "temp": {"warn": 40.0, "danger": 50.0},
    "hum":  {"warn": 80.0, "danger": 90.0},
    "dust": {"warn": 150.0, "danger": 300.0},
    "mq2":  {"warn": 1000.0, "danger": 2000.0},
}

def fetch_data(path, params=None):
    r = requests.get(f"{FIREBASE_DB_URL}/{path}.json", params=params, timeout=10)
    r.raise_for_status()
    return r.json()

def push_data(path, data):
    r = requests.put(f"{FIREBASE_DB_URL}/{path}.json", json=data, timeout=10)
    r.raise_for_status()
    print(f"Đã cập nhật {path} lên Firebase")

# ===== THUẬT TOÁN AI =====
def learn_thresholds(history, metric):
    values = [v[metric] for v in history.values() if v and metric in v and v[metric] is not None]
    if len(values) < 10: return None
    arr = np.array(values, dtype=float)
    return {
        "warn": round(float(np.percentile(arr, 90)), 2),
        "danger": round(float(np.percentile(arr, 99)), 2),
        "mean": round(float(np.mean(arr)), 2),
        "std": round(float(np.std(arr)), 2),
    }

def detect_anomaly_isolation_forest(history, latest):
    features = ["temp", "hum", "dust", "mq2"]
    X_train = []
    for v in history.values():
        if not v: continue
        row = [v.get(f) for f in features]
        if all(x is not None and not math.isnan(x) for x in row):
            X_train.append(row)
    if len(X_train) < 20: return {"is_anomaly": False}
    
    scaler = StandardScaler()
    X_scaled = scaler.fit_transform(X_train)
    model = IsolationForest(contamination=0.05, random_state=42)
    model.fit(X_scaled)
    
    x_new = [latest.get(f) for f in features]
    if any(x is None or math.isnan(x) for x in x_new): return {"is_anomaly": False}
    
    x_scaled = scaler.transform([x_new])
    pred = model.predict(x_scaled)[0]
    return {"is_anomaly": pred == -1, "score": round(float(model.decision_function(x_scaled)[0]), 4)}

def forecast_arima(history, metric, steps=30):
    keys = list(history.keys())[-30:]
    values = [history[k][metric] for k in keys if history[k] and metric in history[k] and history[k][metric] is not None]
    if len(values) < 10: return None
    try:
        series = pd.Series(values)
        model = ARIMA(series, order=(1, 1, 1)).fit()
        forecast = model.forecast(steps=steps)
        val = float(forecast.iloc[-1]) if hasattr(forecast, 'iloc') else float(forecast[-1])
        slope = (values[-1] - values[0]) / len(values)
        trend = "tăng" if slope > 0.3 else "giảm" if slope < -0.3 else "ổn định"
        return {"value": round(val, 2), "trend": trend, "confidence": max(0, min(100, 100 - model.aic/10))}
    except Exception as e:
        return {"value": None, "trend": "Lỗi dự báo", "confidence": 0}

# ===== LẤY DỮ LIỆU THỜI TIẾT =====
def get_external_context():
    ctx = {"city_temp": None, "city_humidity": None, "city_pm25": None, "city_pm25_forecast_6h": None}
    try:
        lat, lon = 21.0285, 105.8542 # Tọa độ Hà Nội, bạn có thể đổi
        w_res = requests.get(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&current=temperature_2m,relative_humidity_2m&timezone=auto", timeout=10).json()
        ctx["city_temp"] = w_res["current"]["temperature_2m"]
        ctx["city_humidity"] = w_res["current"]["relative_humidity_2m"]
        
        a_res = requests.get(f"https://air-quality-api.open-meteo.com/v1/air-quality?latitude={lat}&longitude={lon}&hourly=pm2_5&timezone=auto&forecast_days=1", timeout=10).json()
        now_str = datetime.now().strftime("%Y-%m-%dT%H:00")
        times = a_res["hourly"]["time"]
        if now_str in times:
            idx = times.index(now_str)
            ctx["city_pm25"] = a_res["hourly"]["pm2_5"][idx]
            if idx + 6 < len(a_res["hourly"]["pm2_5"]):
                ctx["city_pm25_forecast_6h"] = a_res["hourly"]["pm2_5"][idx + 6]
    except Exception as e:
        print(f"Lỗi lấy thời tiết: {e}")
    return ctx

# ===== HÀM CHÍNH =====
def main():
    print("Bắt đầu chạy AI...")
    latest = fetch_data("latest")
    if not latest:
        print("Không có dữ liệu latest.")
        return

    since = int(time.time()) - 24 * 3600
    history = fetch_data("history", {"orderBy": '"$key"', "startAt": f'"{since}"'})
    history = dict(sorted((history or {}).items(), key=lambda x: int(x[0])))

    # Tính toán
    metrics = ["temp", "hum", "dust", "mq2"]
    learned = {k: learn_thresholds(history, k) for k in metrics}
    iso = detect_anomaly_isolation_forest(history, latest)
    forecasts = {k: forecast_arima(history, k) for k in metrics}
    external_ctx = get_external_context()

    # Đánh giá trạng thái
    issues = []
    level = "ok"
    for k in metrics:
        val = latest.get(k)
        if val is None: continue
        th = learned[k] if learned[k] else DEFAULT_THRESHOLDS[k]
        if val >= th["danger"]:
            issues.append(f"{k} nguy hiểm"); level = "danger"
        elif val >= th["warn"]:
            issues.append(f"{k} cảnh báo")
            if level != "danger": level = "warn"
    
    if iso.get("is_anomaly"):
        issues.append("Bất thường đa chiều")
        if level != "danger": level = "warn"

    # Đóng gói kết quả
    result = {
        "timestamp": int(time.time()),
        "level": level,
        "issues": issues,
        "forecasts": forecasts,
        "isolation_forest": iso,
        "external_context": external_ctx
    }

    # Đẩy lên Firebase
    push_data("ai_analysis", result)

if __name__ == "__main__":
    main()
