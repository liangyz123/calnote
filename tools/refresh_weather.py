#!/usr/bin/env python3
"""按新算法（看实际降水量）重刷已存条目里的天气文本。

用法（容器里跑，或用 NAS 上的 python）：
    python tools/refresh_weather.py            # 只预览：列出会怎么改，不写库
    python tools/refresh_weather.py --apply    # 真正写入
    python tools/refresh_weather.py --apply --from 2023-01-01 --to 2026-12-31

它会：① 用 Open-Meteo 重新取当天天气 → 写 data/weather_cache.json（带 src/mm 标记）；
      ② 把 items 里的天气文本换成校准后的结果（只有原来带"雨/雪"而实际没降水的才改）。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
os.environ.setdefault("DB_PATH", os.path.join(ROOT, "data", "calnote.db"))
import app as A                                                 # noqa: E402

WET = re.compile("雨|雪|霰|雷")
DRY = {"晴", "多云", "阴"}


def day_weather(ds):
    d = datetime.strptime(ds, "%Y-%m-%d").date()
    txt = A.fetch_weather_online(d)
    cw = A.weather_cache().get(ds) or {}
    return txt, cw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写库（默认只预览）")
    ap.add_argument("--from", dest="dfrom", default="")
    ap.add_argument("--to", dest="dto", default="")
    a = ap.parse_args()
    conn = sqlite3.connect(os.environ["DB_PATH"])
    conn.row_factory = sqlite3.Row
    q = "SELECT id,date,weather,title FROM items WHERE weather<>''"
    if a.dfrom:
        q += " AND date>=?"
    if a.dto:
        q += " AND date<=?"
    rows = conn.execute(q, [x for x in (a.dfrom, a.dto) if x]).fetchall() if (a.dfrom or a.dto) else conn.execute(q).fetchall()
    days = sorted({r["date"] for r in rows})
    print("扫描 %d 条记录，涉及 %d 天" % (len(rows), len(days)))
    cache = {}
    for ds in days:
        txt, cw = day_weather(ds)
        if txt:
            cache[ds] = (txt, cw)
    changed = 0
    for r in rows:
        now = cache.get(r["date"])
        if not now:
            continue
        new_txt, cw = now
        new_desc = new_txt.split(" ", 1)[1] if " " in new_txt else new_txt
        old = r["weather"] or ""
        old_desc = old.split(" ", 1)[1] if " " in old else old
        if WET.search(old_desc) and not WET.search(new_desc) and (cw.get("mm") or 0) < A.RAIN_MIN_MM:
            tail = old.split(" ", 1)[0]
            fixed = (tail + " " + new_desc).strip()
            changed += 1
            print("  改：%s  「%s」→「%s」  (实测降水 %smm)" % (r["date"], old, fixed, cw.get("mm")))
            if a.apply:
                conn.execute("UPDATE items SET weather=? WHERE id=?", (fixed, r["id"]))
    if a.apply:
        conn.commit()
        print("已写入：修正 %d 条天气文本" % changed)
    else:
        print("预览模式：会修正 %d 条（加 --apply 才写库）" % changed)
    conn.close()


if __name__ == "__main__":
    main()
