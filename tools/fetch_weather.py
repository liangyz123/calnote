#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史天气抓取（Open-Meteo 存档接口，免费无需 key），结果缓存成 JSON，供导入脚本使用。

用法：
    python3 tools/fetch_weather.py --start 2023-01-03 --end 2026-10-17 \
        --city 兴安县 --lat 25.6128 --lon 110.6720 --out data/weather_cache.json

缓存结构：
    {"2023-01-03": {"city": "兴安县", "weather": "多云", "temph": "12", "templ": "4",
                    "precip": "0.0"}, ...}
"""
import argparse
import json
import os
import urllib.parse
import urllib.request
from datetime import date, timedelta

# WMO 天气代码 → 中文（与中华万年历记事里的叫法保持一致）
WMO = {
    0: "晴", 1: "晴", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小雨", 53: "小雨", 55: "中雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨",
    85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨", 99: "雷阵雨",
}


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": "calnote/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch(start, end, lat, lon, tz="Asia/Shanghai"):
    """按半年分段抓取，避免单次区间过长被拒。"""
    daily = {}
    cur = date.fromisoformat(start)
    stop = date.fromisoformat(end)
    while cur <= stop:
        seg_end = min(cur + timedelta(days=180), stop)
        params = urllib.parse.urlencode({
            "latitude": lat, "longitude": lon,
            "start_date": cur.isoformat(), "end_date": seg_end.isoformat(),
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum",
            "timezone": tz,
        })
        url = "https://archive-api.open-meteo.com/v1/archive?" + params
        try:
            data = _get(url)
        except Exception as e:                       # 网络不通就跳过，不影响主流程
            print("  ! 拉取失败 %s~%s：%s" % (cur, seg_end, e))
            cur = seg_end + timedelta(days=1)
            continue
        d = data.get("daily") or {}
        for i, day in enumerate(d.get("time") or []):
            code = (d.get("weather_code") or [None] * (i + 1))[i]
            hi = (d.get("temperature_2m_max") or [None] * (i + 1))[i]
            lo = (d.get("temperature_2m_min") or [None] * (i + 1))[i]
            pr = (d.get("precipitation_sum") or [None] * (i + 1))[i]
            if code is None and hi is None:
                continue
            daily[day] = {
                "weather": WMO.get(int(code), "") if code is not None else "",
                "temph": "" if hi is None else str(int(round(hi))),
                "templ": "" if lo is None else str(int(round(lo))),
                "precip": "" if pr is None else str(pr),
            }
        print("  已取 %s ~ %s（累计 %d 天）" % (cur, seg_end, len(daily)))
        cur = seg_end + timedelta(days=1)
    return daily


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", required=True)
    ap.add_argument("--end", required=True)
    ap.add_argument("--lat", type=float, default=25.6128, help="纬度，默认兴安县")
    ap.add_argument("--lon", type=float, default=110.6720, help="经度，默认兴安县")
    ap.add_argument("--city", default="兴安县")
    ap.add_argument("--tz", default="Asia/Shanghai")
    ap.add_argument("--out", default="data/weather_cache.json")
    args = ap.parse_args()

    cache = {}
    if os.path.exists(args.out):
        with open(args.out, encoding="utf-8") as f:
            cache = json.load(f)
    got = fetch(args.start, args.end, args.lat, args.lon, args.tz)
    for k, v in got.items():
        v["city"] = args.city
        cache[k] = v
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, sort_keys=True)
    print("天气缓存：%d 天 → %s" % (len(cache), args.out))


if __name__ == "__main__":
    main()
