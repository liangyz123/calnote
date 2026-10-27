#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
build_calnote_import.py —— 把「弄文笔记 SQLite」+「中华万年历导出 txt」
合并成 calnote（日历便签）可直接导入的 CSV。

分类规则（对齐 calnote 的 items.type）：
    弄文日记的每一段            → note（笔记），标题用日历日期，正文不塞进标题
    万年历 lineType 1/8（记事）  → note（笔记），同样标题=日期，天气保留
    万年历 lineType 2（生日）    → birthday（每年重复，农历生日标 is_lunar）
    万年历 lineType 3/5（提醒）  → schedule（日程，带提醒分钟）
    万年历 lineType 4（清单）    → todo（待办，子项转 todo_items）

天气：
    ① 原备份里 data 字段带的 城市/天气/高温/低温 原样保留
    ② 原备份没有的日期（如弄文笔记 2023-01-03 之后），用 --weather 的历史天气缓存补齐
       缓存由 tools/fetch_weather.py 生成

用法：
    python3 tools/build_calnote_import.py \
        --ecalendar ecalendarTableData600.txt \
        --db nowen-note.db --start-date 2023-01-03 \
        --weather data/weather_cache.json \
        --out calnote_import.csv
然后在 calnote 页面「导入」里选这个 csv（或命令行 --apply 直接写库）。
"""
import argparse
import csv
import json
import os
import re
import sqlite3
import sys
import zlib
from datetime import date, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nowen_extract import entries_from_note, load_notes   # 弄文日记解析（同一套代码）

# calnote items 表列（与 /api/import 的表头一致，导出文件同构，可反复导入）
FIELDS = ["type", "title", "date", "time", "end_time", "all_day", "content",
          "tags", "location", "remind_minutes", "is_lunar", "yearly", "done",
          "weather", "todo_items"]
EXTRA = "_assumed"                 # 只在统计里用，不写进导入文件

TAG_ECALENDAR = "万年历记事"
TAG_NOWEN = "弄文日记"
TAG_ASSUMED = "时刻为默认"        # 源数据没时刻 / 写着 00:00 → 补成早上，标记出来好回退


# --------------------------------------------------------------- 通用工具
def clean_text(s):
    """记事 title（HTML）→ 纯文本，一段一行。"""
    s = re.sub(r"<\s*/?\s*p\b[^>]*>", "\n", s or "", flags=re.I)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = (s.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
          .replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&"))
    lines = [l.strip() for l in s.splitlines()]
    return "\n".join(l for l in lines if l)


def parse_json(s):
    try:
        return json.loads(s or "{}")
    except (ValueError, TypeError):
        return {}


def date_str(y, m, d):
    return "%04d-%02d-%02d" % (int(y), int(m), int(d))


def day_title(y, m, d, hh, mi):
    """笔记标题用日历时间：2023.01.03，多段时补上时刻 2023.01.03 17:41"""
    base = "%04d.%02d.%02d" % (int(y), int(m), int(d))
    return base if (not hh and not mi) else "%s %02d:%02d" % (base, int(hh), int(mi))


def _temp(v):
    """31 / 31℃ / 31° → 31°；空值返回空串"""
    s = str(v if v is not None else "").strip()
    if not s:
        return ""
    num = re.sub(r"[^\d.\-]", "", s)
    if num in ("", "-", "."):
        return ""
    return num + "°"


def weather_str(city, weather, hi, lo):
    """拼成 calnote 里的天气串：兴安县 多云 11°/6°"""
    parts = []
    if city:
        parts.append(str(city).strip())
    if weather:
        parts.append(str(weather).strip())
    t = [x for x in (_temp(hi), _temp(lo)) if x]
    if t:
        parts.append("/".join(t))
    return " ".join(parts)


def blank():
    return {k: "" for k in FIELDS}


# --------------------------------------------------------------- 万年历备份 → 条目
def from_ecalendar(path, start=None, end=None):
    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            lt = int(o.get("lineType") or 1)
            sy, sm, sd = o.get("syear") or 0, o.get("smonth") or 0, o.get("sdate") or 0
            hh, mi = int(o.get("shour") or 0), int(o.get("sminute") or 0)
            data = parse_json(o.get("data"))
            note_text = (o.get("note") or "").strip()
            body = clean_text(o.get("title") or "")

            if lt == 2:                                   # 生日
                if sm and sd:
                    d = date_str(sy or 2000, sm, sd)
                    lunar = 0 if sy else 1               # syear=0 表示只记了农历
                else:
                    continue
                rows.append({**blank(),
                             EXTRA: False,
                             "type": "生日", "title": body or note_text or data.get("peoples", {}).get("name", "") or "生日",
                             "date": d, "content": note_text if note_text != body else "",
                             "is_lunar": "1" if lunar else "0",
                             "yearly": "1" if int(o.get("cycle") or 0) == 1 else "0",
                             "remind_minutes": "0",
                             "tags": "万年历生日"})
                continue

            if lt == 4:                                   # 待办清单
                items = data.get("list") or []
                todo = [{"text": i.get("text", ""), "done": int(i.get("done") or 0)} for i in items]
                rows.append({**blank(),
                             EXTRA: False,
                             "type": "待办", "title": body or "待办",
                             "date": date_str(sy, sm, sd) if (sy and sm and sd) else "",
                             "time": "%02d:%02d" % (hh, mi),
                             "content": note_text,
                             "done": "1" if data.get("isDone") else "0",
                             "todo_items": json.dumps(todo, ensure_ascii=False),
                             "tags": "万年历待办"})
                continue

            if lt in (3, 5):                              # 提醒 / 闹钟 → 日程
                place = (data.get("place") or {}).get("address") if isinstance(data.get("place"), dict) else ""
                adv = int(o.get("advance") or 0)
                ny, nm, nd, nh, nmi = (o.get("nyear") or 0, o.get("nmonth") or 0,
                                       o.get("ndate") or 0, o.get("nhour") or 0, o.get("nminute") or 0)
                end_time = "%02d:%02d" % (nh, nmi) if (ny == sy and nm == sm and nd == sd and (nh or nmi)) else ""
                rows.append({**blank(),
                             "type": "日程", "title": body or "提醒",
                             "date": date_str(sy, sm, sd) if (sy and sm and sd) else "",
                             "time": "%02d:%02d" % (hh, mi) if (hh or mi) else "",
                             "end_time": end_time,
                             "location": place or "",
                             "remind_minutes": str(adv // 60 if adv > 0 else 0),
                             EXTRA: False,
                             "content": note_text if note_text != body else "",
                             "weather": weather_str(data.get("city"), data.get("weather"),
                                                    data.get("temph"), data.get("templ")),
                             "tags": "万年历提醒"})
                continue

            # lineType 1 / 8：记事（日记内容）→ 笔记
            if not (sy and sm and sd):
                continue
            weather = weather_str(data.get("city"), data.get("weather"),
                                  data.get("temph"), data.get("templ"))
            content = body
            if note_text and note_text != body:
                content = (content + "\n" + note_text).strip()
            rows.append({**blank(),
                         "type": "笔记",
                         "title": day_title(sy, sm, sd, hh, mi),
                         "date": date_str(sy, sm, sd),
                         "time": "%02d:%02d" % (hh, mi) if (hh or mi) else "",
                         "content": content,
                         "weather": weather,
                         "all_day": "1" if (data.get("is_allday") or data.get("isAllDayTask")) else "0",
                         "location": ((data.get("place") or {}).get("address")
                                      if isinstance(data.get("place"), dict) else "") or "",
                         "tags": TAG_ECALENDAR, EXTRA: False})
    return [r for r in rows if r["date"]
            and (not start or r["date"] >= start)
            and (not end or r["date"] <= end)]


# --------------------------------------------------------------- 弄文笔记 → 条目
def from_nowen(db, start=None, end=None, include_normal=False, default_time=None,
               morning_time=(7, 30)):
    """morning_time：日记源数据里写成 00:00（当天没填时间）的，补成当天早上这个时刻。
    传 None 关闭。补过的会打上「时刻为默认」标签，方便以后筛出来改。"""
    rows = []
    for note in load_notes(db, include_normal):
        for e in entries_from_note(note, default_time):
            if end and e["date"] > end:
                continue
            hh, mi = e["hour"], e["minute"]
            y, m, d = e["date"]
            tags = TAG_NOWEN
            assumed = False
            if morning_time and e.get("time_src") == "标题" and (hh, mi) == (0, 0):
                hh, mi = morning_time
                tags = TAG_NOWEN + "," + TAG_ASSUMED
                assumed = True
            day = date_str(y, m, d)
            if start and day < start:
                continue
            rows.append({**blank(),
                         "type": "笔记",
                         "title": day_title(y, m, d, hh, mi),
                         "date": day,
                         "time": "%02d:%02d" % (hh, mi),
                         "content": e["text"],
                         "tags": tags, EXTRA: assumed})
    return rows


# --------------------------------------------------------------- 天气回填 + 去重
def fill_weather(rows, cache_path, city="兴安县"):
    if not cache_path or not os.path.exists(cache_path):
        return rows, 0
    with open(cache_path, encoding="utf-8") as f:
        cache = json.load(f)
    n = 0
    for r in rows:
        if r["type"] != "笔记" or r["weather"]:
            continue
        w = cache.get(r["date"])
        if not w:
            continue
        r["weather"] = weather_str(w.get("city") or city, w.get("weather"),
                                   w.get("temph"), w.get("templ"))
        n += 1
    return rows, n


def dedupe(rows):
    """同一份内容可能既在万年历备份里、又在弄文库里，按 日期+时间+正文 去重。"""
    seen, out, dropped = set(), [], 0
    # 优先保留有天气的那条
    for r in sorted(rows, key=lambda x: (x["date"], x["time"], -len(x["weather"]))):
        key = (r["type"], r["date"], r["content"].strip())
        if key in seen:
            dropped += 1
            continue
        seen.add(key)
        out.append(r)
    return out, dropped


def write_csv(rows, path):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS, extrasaction="ignore", quoting=csv.QUOTE_ALL)
        w.writeheader()
        for r in sorted(rows, key=lambda x: (x["date"], x["time"], x["type"], x["title"])):
            w.writerow(r)


# 中文类型 → calnote / 弄文笔记里的英文标识
TYPE_KEY = {"笔记": "note", "生日": "birthday", "日程": "schedule",
            "待办": "todo", "纪念日": "anniversary", "倒数日": "countdown"}


def write_jsonl(rows, path):
    """每行一条 JSON，字段风格照搬弄文笔记（notes 表）：
    type / journal_date / title / content / contentText / contentFormat /
    createdAt / updatedAt / isPinned / isFavorite / isArchived / isTrashed /
    version / sortOrder + calnote 专有可能的标签 tags
    """
    def b(v):
        return v in ("1", 1, True)
    with open(path, "w", encoding="utf-8") as f:
        for r in sorted(rows, key=lambda x: (x["date"], x["time"], x["type"], x["title"])):
            key = TYPE_KEY.get(r["type"], "note")
            content = r["content"] or r["title"]
            note = {
                "id": "%s-%s-%s" % (key, r["date"].replace("-", ""),
                                    zlib.crc32((r["title"] + content).encode("utf-8")) % 10 ** 8),
                "type": key,
                "note_type": "journal" if key == "note" else "normal",
                "journal_date": r["date"] if key == "note" else "",
                "date": r["date"],
                "title": r["title"],
                "content": content,
                "contentText": content,
                "contentFormat": "plain",
                "notebook": "日历便签导入",
                "tags": r["tags"],
                "weather": r["weather"],
                "location": r["location"],
                "time": r["time"],
                "end_time": r["end_time"],
                "all_day": b(r["all_day"]),
                "is_lunar": b(r["is_lunar"]),
                "yearly": b(r["yearly"]),
                "done": b(r["done"]),
                "remind_minutes": int(r["remind_minutes"] or 0),
                "todo_items": (json.loads(r["todo_items"]) if r["todo_items"] else []),
                "isPinned": False,
                "isFavorite": False,
                "isArchived": False,
                "isTrashed": False,
                "has_attachment": False,
                "image_count": 0,
                "version": 1,
                "sortOrder": 0,
                "createdAt": "%s %s:00" % (r["date"], r["time"] or "00:00"),
                "updatedAt": "%s %s:00" % (r["date"], r["time"] or "00:00"),
                "source": r["tags"],
                "type_label": r["type"],
            }
            f.write(json.dumps(note, ensure_ascii=False) + "\n")


def apply_to_db(rows, db_path):
    """--apply：直接写进 calnote 的 SQLite（比页面导入更快，字段值自动转英文类型标识）"""
    conn = sqlite3.connect(db_path)
    have = {(r[0], r[1], (r[2] or "")[:120]) for r in conn.execute(
        "select date, type, content from items").fetchall()}
    ok = skip = 0
    for r in rows:
        key = (r["date"], TYPE_KEY.get(r["type"], "note"), (r["content"] or "")[:120])
        if key in have:
            skip += 1
            continue
        have.add(key)
        conn.execute(
            """INSERT INTO items (type, title, content, date, time, end_time, all_day, location,
                                  remind_minutes, is_lunar, yearly, done, tags, weather, todo_items)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (TYPE_KEY.get(r["type"], "note"), r["title"], r["content"], r["date"], r["time"],
             r["end_time"], 1 if r["all_day"] == "1" else 0, r["location"],
             int(r["remind_minutes"] or 0), 1 if r["is_lunar"] == "1" else 0,
             1 if r["yearly"] == "1" else 0, 1 if r["done"] == "1" else 0,
             r["tags"], r["weather"], r["todo_items"]))
        ok += 1
    conn.execute("PRAGMA user_version=5")
    conn.commit()
    conn.close()
    return ok, skip


# --------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="弄文笔记 + 中华万年历 → calnote 导入文件")
    ap.add_argument("--db", help="nowen-note.db（弄文笔记数据库）")
    ap.add_argument("--ecalendar", help="中华万年历导出 txt")
    ap.add_argument("--out", default="calnote_import.csv", help="输出文件；--format jsonl 时默认写 .jsonl")
    ap.add_argument("--format", choices=("csv", "jsonl", "both"), default="both",
                    help="csv=calnote 表格导入；jsonl=弄文笔记那样子一行一条 JSON；both=两个都写")
    ap.add_argument("--start-date", help="弄文日记起始日期 YYYY-MM-DD（避免与万年历备份重复）")
    ap.add_argument("--end-date", help="弄文日记结束日期 YYYY-MM-DD")
    ap.add_argument("--ecalendar-start", help="万年历备份起始日期，默认不限")
    ap.add_argument("--ecalendar-end", help="万年历备份结束日期，默认不限")
    ap.add_argument("--weather", help="历史天气缓存 JSON（tools/fetch_weather.py 生成）")
    ap.add_argument("--city", default="兴安县", help="历史天气回填时用哪个城市名")
    ap.add_argument("--include-normal", action="store_true", help="弄文的普通笔记也导出")
    ap.add_argument("--morning-time", default="07:30",
                    help="源数据里写成 00:00（没填时间）的日记补成这个时刻；写 off 表示不补")
    ap.add_argument("--default-time", help="弄文笔记缺时间时的兜底 HH:MM（默认用笔记创建时刻）")
    ap.add_argument("--apply", help="直接写入指定的 calnote 数据库文件")
    args = ap.parse_args()

    rows = []
    if args.ecalendar:
        # 起止日期只用于限制弄文日记（避免与备份重复），万年历备份本身全量保留
        got = from_ecalendar(args.ecalendar, args.ecalendar_start, args.ecalendar_end)
        print("万年历备份：{} 条".format(len(got)))
        rows += got
    if args.db:
        dt = None
        if args.default_time:
            m = re.fullmatch(r"(\d{1,2})[:：]?(\d{2})?", args.default_time)
            if not m:
                sys.exit("--default-time 应形如 07:30")
            dt = (int(m.group(1)) % 24, int(m.group(2) or 0) % 60)
        mt = None
        if args.morning_time and args.morning_time.lower() != "off":
            mm2 = re.fullmatch(r"(\d{1,2})[:：]?(\d{2})?", args.morning_time)
            if not mm2:
                sys.exit("--morning-time 应形如 07:30")
            mt = (int(mm2.group(1)) % 24, int(mm2.group(2) or 0) % 60)
        got = from_nowen(args.db, args.start_date, args.end_date, args.include_normal, dt, mt)
        print("弄文笔记（{}起）：{} 条".format(args.start_date or "最早", len(got)))
        rows += got
    if not rows:
        sys.exit("没有可导出的内容，请指定 --db 或 --ecalendar")

    assumed = sum(1 for r in rows if r.get(EXTRA))
    rows, n = fill_weather(rows, args.weather, args.city)
    print("历史天气回填：{} 条笔记补上天气".format(n))
    if assumed:
        print("默认时刻补齐：{} 条原本 00:00 的日记改成 {}（标签「{}」）".format(
            assumed, args.morning_time, TAG_ASSUMED))
    rows, dropped = dedupe(rows)
    print("去重：丢弃 {} 条重复，合并后 {} 条".format(dropped, len(rows)))
    stat = {}
    for r in rows:
        stat[r["type"]] = stat.get(r["type"], 0) + 1
    print("分类统计：" + "，".join("%s %d" % (k, v) for k, v in sorted(stat.items())))
    print("日期范围：{} → {}".format(min(r["date"] for r in rows), max(r["date"] for r in rows)))

    out = args.out
    base = re.sub(r"\.csv$|\.jsonl$", "", out)
    wrote = []
    if args.format in ("csv", "both"):
        path = base + ".csv"
        write_csv(rows, path)
        wrote.append(path)
    if args.format in ("jsonl", "both"):
        path = base + ".jsonl"
        write_jsonl(rows, path)
        wrote.append(path)
    print("已生成导入文件 → " + "，".join(wrote))
    if args.apply:
        ok, skip = apply_to_db(rows, args.apply)
        print("已写入数据库 {}：新增 {} 条，重复跳过 {} 条".format(args.apply, ok, skip))


if __name__ == "__main__":
    main()
