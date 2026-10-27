#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""pull_attachments.py —— 把老笔记里的图片搬到 calnote 的附件目录里。

支持两个来源（可以只跑一个，也可以一起跑）：

1) 中华万年历导出 txt：记事里 data.medias 的图片是 2015 年前后的 CDN 外链
   python3 tools/pull_attachments.py --ecalendar ecalendarTableData600.txt \
           --db data/calnote.db --dir data/attachments

2) 弄文笔记：附件本体不在 SQLite 里，只存了相对路径（2026/09/xxx.png），
   要把弄文的数据目录一起给它（在 NAS 上跑，路径填弄文容器挂载的 data 目录）
   python3 tools/pull_attachments.py --nowen-db nowen-note.db \
           --nowen-files /vol1/docker/nowen/data --db data/calnote.db --dir data/attachments

重复跑安全：同一条内容 + 同一个文件名已经登记过就跳过。
"""
import argparse
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import urllib.parse
import urllib.request
import uuid
from datetime import datetime

UA = {"User-Agent": "Mozilla/5.0 (calnote)"}
MAX_SIZE = 20 * 1024 * 1024          # 与 app.py 的上传限制保持一致


def nowen_paths(db_path):
    """弄文 attachments 表：{note_id: [{filename, path, mime, size}]}"""
    con = sqlite3.connect("file:%s?mode=ro&immutable=1" % os.path.abspath(db_path), uri=True)
    con.row_factory = sqlite3.Row
    out = {}
    for r in con.execute("select noteId, filename, path, mimeType, size from attachments"):
        out.setdefault(r["noteId"], []).append(dict(r))
    # 日记用 journal_date，普通笔记/文件类没有它 → 退回用创建日期
    notes = {r["id"]: ((r["journal_date"] or r["createdAt"] or "")[:10])
             for r in con.execute("select id, journal_date, createdAt from notes")}
    con.close()
    return out, notes


def norm_ext(name, stored, mime):
    ext = os.path.splitext(name)[1] or os.path.splitext(stored)[1]
    if not ext and mime:
        ext = mimetypes.guess_extension(mime.split(";")[0].strip()) or ""
    return (ext or ".bin")[:10].lower()


def register(conn, att_dir, item_id, filename, src_open, size, mime):
    """把一份文件落到附件目录并登记；返回 'ok' / 'skip' / 'fail'"""
    have = conn.execute("SELECT 1 FROM attachments WHERE item_id=? AND filename=? LIMIT 1",
                        (item_id, filename)).fetchone()
    if have:
        return "skip"
    if size and size > MAX_SIZE:
        print("   超过 20MB 跳过：%s (%s)" % (filename, size))
        return "fail"
    stored = uuid.uuid4().hex + norm_ext(filename, "", mime)
    dst = os.path.join(att_dir, stored)
    os.makedirs(att_dir, exist_ok=True)
    try:
        with open(dst, "wb") as f:
            shutil.copyfileobj(src_open, f)
    except Exception as e:
        if os.path.exists(dst):
            os.remove(dst)
        print("   写入失败 %s：%s" % (filename, e))
        return "fail"
    conn.execute("INSERT INTO attachments (item_id, filename, stored_name, mime, size) VALUES (?,?,?,?,?)",
                 (item_id, filename, stored, mime or "", os.path.getsize(dst)))
    return "ok"


def pick_item(conn, day, time_str):
    """按日期找那条笔记；同一天多条就挑时间最接近的一条"""
    rows = conn.execute("SELECT id, time FROM items WHERE date=? ORDER BY time, id", (day,)).fetchall()
    if not rows:
        return None
    if not time_str or len(rows) == 1:
        return rows[0]["id"]
    def mins(t):
        try:
            h, m = t.split(":")
            return int(h) * 60 + int(m)
        except Exception:
            return 0
    target = mins(time_str)
    return min(rows, key=lambda r: abs(mins(r["time"]) - target))["id"]


def pull_ecalendar(path, conn, att_dir, limit_host=None):
    """万年历：medias[].path 是外链，下载后落到附件目录"""
    stats = {"ok": 0, "skip": 0, "fail": 0, "noitem": 0}
    n = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            o = json.loads(line)
            try:
                data = json.loads(o.get("data") or "{}")
            except ValueError:
                data = {}
            medias = [m for m in (data.get("medias") or []) if isinstance(m, dict) and m.get("path")]
            if not medias:
                continue
            sy, sm, sd = int(o.get("syear") or 0), int(o.get("smonth") or 0), int(o.get("sdate") or 0)
            if not (sy and sm and sd):
                continue
            day = "%04d-%02d-%02d" % (sy, sm, sd)
            n += len(medias)
            item_id = pick_item(conn, day, "")
            if item_id is None:
                stats["noitem"] += len(medias)
                print("  %s 那天没有对应内容，%d 张图跳过" % (day, len(medias)))
                continue
            for m in medias:
                url = m["path"]
                host = urllib.parse.urlsplit(url).netloc
                if limit_host and host != limit_host:
                    continue
                name = m.get("name") or os.path.basename(urllib.parse.urlsplit(url).path) or "图片.jpg"
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=25) as resp:
                        mime = resp.headers.get("Content-Type", "")
                        got = register(conn, att_dir, item_id, re.sub(r"\s+", " ", name).strip(),
                                       resp, int(m.get("size") or 0), mime.split(";")[0])
                    stats[got] = stats.get(got, 0) + 1
                    if got == "ok":
                        print("  %s ← %s" % (day, name))
                except Exception as e:
                    stats["fail"] += 1
                    print("  下载失败 %s：%s" % (url[:70], e))
    print("万年历图片：共 %d 张，%s" % (n, stats))


def pull_nowen(db_path, files_root, conn, att_dir):
    """弄文：附件文件在 files_root 下，按 attachments.path 相对定位"""
    by_note, note_days = nowen_paths(db_path)
    stats = {"ok": 0, "skip": 0, "fail": 0, "miss": 0}
    for note_id, atts in by_note.items():
        day = note_days.get(note_id) or ""
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            stats["miss"] += len(atts)
            continue
        item_id = pick_item(conn, day, "")
        if item_id is None:
            stats["miss"] += len(atts)
            print("  弄文笔记 %s（%s）在 calnote 里找不到对应内容，%d 个附件跳过"
                  % (note_id[:8], day, len(atts)))
            continue
        for a in atts:
            src = None
            for cand in (os.path.join(files_root, a["path"]),
                         os.path.join(files_root, "attachments", a["path"]),
                         os.path.join(files_root, os.path.basename(a["path"]))):
                if os.path.isfile(cand):
                    src = cand
                    break
            if src is None:
                stats["miss"] += 1
                print("  找不到文件：%s（%s）" % (a["filename"], a["path"]))
                continue
            with open(src, "rb") as fh:
                got = register(conn, att_dir, item_id, a["filename"], fh,
                               a["size"] or os.path.getsize(src), a["mimeType"])
            stats[got] = stats.get(got, 0) + 1
            if got == "ok":
                print("  %s ← %s" % (day, a["filename"]))
    print("弄文附件：共 %d 个，%s" % (sum(len(v) for v in by_note.values()), stats))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ecalendar", help="中华万年历导出 txt（每行一条 JSON）")
    ap.add_argument("--nowen-db", help="nowen-note.db")
    ap.add_argument("--nowen-files", help="弄文存附件的目录（NAS 上弄文的数据目录）")
    ap.add_argument("--db", default="data/calnote.db", help="calnote 数据库")
    ap.add_argument("--dir", default="data/attachments", help="calnote 附件目录")
    ap.add_argument("--host", help="只下的域名（比如 img2034.static.suishenyun.net）")
    args = ap.parse_args()

    if not (args.ecalendar or args.nowen_db):
        ap.error("至少给 --ecalendar 或 --nowen-db 之一")
    if args.nowen_db and not args.nowen_files:
        ap.error("--nowen-db 需要配合 --nowen-files 指到附件本体目录（SQLite 里只存相对路径）")
    if not os.path.exists(args.db):
        sys.exit("找不到 calnote 数据库 %s，先把内容导入再生成附件" % args.db)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        if args.ecalendar:
            pull_ecalendar(args.ecalendar, conn, args.dir, args.host)
        if args.nowen_db:
            pull_nowen(args.nowen_db, args.nowen_files, conn, args.dir)
        conn.commit()
        total = conn.execute("SELECT COUNT(*) c FROM attachments").fetchone()["c"]
        print("现在库里共 %d 个附件（%s）" % (total, args.dir))
    finally:
        conn.close()


if __name__ == "__main__":
    main()
