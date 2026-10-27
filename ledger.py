# -*- coding: utf-8 -*-
"""
日历便签 v9 · 记账模块（第 2 期）
==================================
六张表：账本 / 账本成员 / 账户 / 分类 / 流水 / 预算（预算表本期只建不用，第 4 期接上）。
金额一律以「分」存整数，接口只接受 12 或 12.3 / 12.34 这类写法，杜绝浮点累加误差。
转账走双分录（dir='transfer' + to_account_id），只动两个账户余额，不进收支统计。

本模块不自己建 Flask，由 app.py 调 register(app, ctx) 把上下文（数据库、当前用户、
权限判断）注入进来，避免和 app.py 互相 import。
"""

import io
import json
import os
import re
import sqlite3
from datetime import date, datetime
from urllib.parse import quote

# 常用分类与账户：首次进账本页自动备好，不用先建结构才能记第一笔
DEFAULT_EXPENSE = ["餐饮", "交通", "购物", "居住", "医药", "通讯", "娱乐", "人情", "工作", "其他"]
DEFAULT_INCOME = ["工资", "兼职", "报销", "其他收入"]
DEFAULT_ACCOUNTS = [("现金", "cash"), ("微信", "wechat"), ("支付宝", "alipay"), ("银行卡", "bank")]
KINDS = ("expense", "income")
DIRS = ("out", "in", "transfer")


def register(app, ctx):
    get_db = ctx["get_db"]
    current_uid = ctx["current_uid"]
    is_admin = ctx.get("is_admin", lambda: False)

    # ------------------------- 建表与预置数据 -------------------------

    DDL = """
    CREATE TABLE IF NOT EXISTS ledger_book (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        name      TEXT NOT NULL,
        owner_id  INTEGER NOT NULL DEFAULT 1,
        note      TEXT DEFAULT '',
        archived  INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS book_member (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id   INTEGER NOT NULL,
        user_id   INTEGER NOT NULL,
        perm      TEXT DEFAULT 'rw',
        created_at TEXT DEFAULT (datetime('now','localtime')),
        UNIQUE(book_id, user_id));
    CREATE TABLE IF NOT EXISTS account (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id       INTEGER NOT NULL,
        name          TEXT NOT NULL,
        type          TEXT DEFAULT 'other',
        icon          TEXT DEFAULT '',                -- 可选 emoji 图标，界面显示它
        opening_cents INTEGER DEFAULT 0,
        sort          INTEGER DEFAULT 0,
        archived      INTEGER DEFAULT 0,
        owner_id      INTEGER DEFAULT 1,
        created_at    TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS category (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id   INTEGER NOT NULL,
        name      TEXT NOT NULL,
        kind      TEXT DEFAULT 'expense',
        color     TEXT DEFAULT '',
        parent_id INTEGER DEFAULT 0,
        sort      INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS entry (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id       INTEGER NOT NULL,
        account_id    INTEGER DEFAULT 0,
        to_account_id INTEGER DEFAULT 0,
        category_id   INTEGER DEFAULT 0,
        day           TEXT NOT NULL,
        cents         INTEGER NOT NULL DEFAULT 0,
        dir           TEXT NOT NULL,
        note          TEXT DEFAULT '',
        created_by    INTEGER DEFAULT 1,
        created_at    TEXT DEFAULT (datetime('now','localtime')),
        updated_at    TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS ledger_trash (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        kind        TEXT NOT NULL,                  -- entry / category：删掉的是什么
        book_id     INTEGER DEFAULT 0,              -- 回到哪个账本
        payload     TEXT NOT NULL DEFAULT '{}',     -- 原行内容 JSON，恢复就照这份插回去
        label       TEXT DEFAULT '',                -- 桶里给人看的一行摘要
        owner_id    INTEGER DEFAULT 1,
        deleted_at  TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS budget (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        book_id     INTEGER NOT NULL,
        category_id INTEGER NOT NULL,
        period      TEXT NOT NULL,
        cents       INTEGER NOT NULL DEFAULT 0,
        UNIQUE(book_id, category_id, period));
    """
    INDEXES = (
        "CREATE INDEX IF NOT EXISTS idx_entry_book_day ON entry(book_id, day)",
        "CREATE INDEX IF NOT EXISTS idx_entry_account ON entry(account_id)",
        "CREATE INDEX IF NOT EXISTS idx_entry_to_account ON entry(to_account_id)",
        "CREATE INDEX IF NOT EXISTS idx_entry_category ON entry(category_id)",
        "CREATE INDEX IF NOT EXISTS idx_entry_by ON entry(created_by)",
        "CREATE INDEX IF NOT EXISTS idx_account_book ON account(book_id)",
        "CREATE INDEX IF NOT EXISTS idx_category_book ON category(book_id)",
        "CREATE INDEX IF NOT EXISTS idx_member_user ON book_member(user_id)",
    )

    def init_ledger_db(conn):
        conn.executescript(DDL)
        # 老库升级：分类、账户各加一个"图标"字段（emoji；没填界面就显示名字第一个字）
        for _t in ("category", "account"):
            try:
                if "icon" not in [x[1] for x in conn.execute("PRAGMA table_info(%s)" % _t)]:
                    conn.execute("ALTER TABLE %s ADD COLUMN icon TEXT DEFAULT ''" % _t)
            except sqlite3.Error:
                pass
        for sql in INDEXES:
            try:
                conn.execute(sql)
            except sqlite3.Error:
                pass

    TRASH_KEEP_DAYS = 30            # 记账桶保留天数（和便签那边一致，便签用环境变量，这里固定 30）

    def _trash_snapshot(conn, kind, book_id, row, label):
        """删之前把整行存进 ledger_trash，误删能原样恢复。"""
        try:
            data = {k: row[k] for k in row.keys()}
        except Exception:
            data = {}
        conn.execute("INSERT INTO ledger_trash(kind,book_id,payload,label,owner_id) VALUES (?,?,?,?,?)",
                     (kind, book_id or 0, json.dumps(data, ensure_ascii=False, default=str),
                      (label or "")[:80], current_uid()))

    def _entry_label(e):
        try:
            sign = {"in": "收", "out": "支", "transfer": "转"}.get(e["dir"], e["dir"] or "")
            return "%s %s %.2f %s" % ((e["day"] or "")[:10], sign, (e["cents"] or 0) / 100.0,
                                      ((e["note"] or "")[:18]))
        except Exception:
            return ""

    def purge_ledger_trash(conn):
        """超过 30 天的快照丢掉（只影响"还能不能恢复"，不动正常数据）。"""
        try:
            n = conn.execute("DELETE FROM ledger_trash WHERE deleted_at < datetime('now','localtime',?)",
                             ("-%d days" % TRASH_KEEP_DAYS,)).rowcount
            conn.commit()
            return n or 0
        except sqlite3.Error:
            return 0

    def read_bill_lines(raw, filename):
        """账单 → 行列表。.xlsx 走 openpyxl（新版微信/支付宝给的就是 xlsx），
        csv/txt 走多编码试探；xlsx 的每一行按 CSV 规则转字符串，后面的解析完全复用。"""
        name = (filename or "").lower()
        if name.endswith((".xlsx", ".xlsm")) or raw[:2] == b"PK":
            try:
                import openpyxl
            except ImportError:
                raise ValueError("这个容器里没有 openpyxl，读不了 xlsx：请用最新整包重建一次镜像；"
                                 "或先在 WPS 里另存为 CSV 再导")
            import csv as _csvmod
            buf = []
            wb = openpyxl.load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
            try:
                ws = wb.worksheets[0]
                for row in ws.iter_rows(values_only=True):
                    cells = ["" if c is None else str(c).strip() for c in row]
                    if not any(cells):
                        continue
                    sink = io.StringIO()
                    _csvmod.writer(sink).writerow(cells)
                    buf.append(sink.getvalue().rstrip("\r\n"))
            finally:
                wb.close()
            return buf
        text = None
        for enc in ("utf-8-sig", "utf-8", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            raise ValueError("认不出这个文件的编码（试过 UTF-8 和 GB18030）")
        return [ln for ln in text.replace("\ufeff", "").splitlines() if ln.strip()]

    def ensure_default_book(conn, uid, name="日常"):
        """给还没账本的用户建一套开箱能用的账本 + 账户 + 分类。"""
        has = conn.execute("SELECT 1 FROM book_member WHERE user_id=? LIMIT 1", (uid,)).fetchone()
        if has:
            return None
        cur = conn.execute("INSERT INTO ledger_book (name, owner_id) VALUES (?,?)", (name, uid))
        bid = cur.lastrowid
        conn.execute("INSERT OR IGNORE INTO book_member (book_id, user_id, perm) VALUES (?,?, 'rw')", (bid, uid))
        for i, (nm, tp) in enumerate(DEFAULT_ACCOUNTS):
            conn.execute("INSERT INTO account (book_id,name,type,opening_cents,sort,owner_id) VALUES (?,?,?,?,?,?)",
                         (bid, nm, tp, 0, i, uid))
        for i, nm in enumerate(DEFAULT_EXPENSE):
            conn.execute("INSERT INTO category (book_id,name,kind,sort) VALUES (?,?,'expense',?)", (bid, nm, i))
        for i, nm in enumerate(DEFAULT_INCOME):
            conn.execute("INSERT INTO category (book_id,name,kind,sort) VALUES (?,?,'income',?)", (bid, nm, i))
        conn.commit()
        return bid

    # ------------------------- 小工具 -------------------------

    def to_cents(v):
        """字符串直接解析成分，不走浮点：12 / 12.3 / 12.34 / -8.5。空或非法返回 None。"""
        s = str(v if v is not None else "").strip().replace(",", "").replace("￥", "").replace("元", "")
        if not s:
            return None
        m = re.fullmatch(r"(-?)(\d+)(?:\.(\d{1,2}))?", s)
        if not m:
            return None
        cents = int(m.group(2)) * 100 + int((m.group(3) or "0").ljust(2, "0"))
        return -cents if m.group(1) == "-" else cents

    def fmt(cents):
        c = int(cents or 0)
        return ("-" if c < 0 else "") + "%d.%02d" % (abs(c) // 100, abs(c) % 100)

    def valid_day(s):
        s = str(s or "").strip()[:10]
        try:
            return date.fromisoformat(s).isoformat()
        except ValueError:
            return None

    def month_of(s):
        return str(s or "")[:7] if len(str(s or "")) >= 7 else None

    def prev_month(ym):
        try:
            y, m = int(ym[:4]), int(ym[5:7])
        except (ValueError, IndexError):
            return None
        return "%04d-%02d" % (y - 1, 12) if m == 1 else "%04d-%02d" % (y, m - 1)

    def my_book_ids(conn, uid):
        rows = conn.execute(
            "SELECT b.id AS id FROM ledger_book b JOIN book_member m ON m.book_id=b.id "
            "WHERE m.user_id=? AND b.archived=0", (uid,)).fetchall()
        ids = [r["id"] for r in rows]
        if not ids:                                  # 第一次进来：现场备一套
            bid = ensure_default_book(conn, uid)
            ids = [bid] if bid else []
        return ids

    def can_book(conn, uid, book_id, need_rw=True):
        """账本权限：成员即可读写；只读成员（perm='r'）不能记账。"""
        r = conn.execute("SELECT perm FROM book_member WHERE book_id=? AND user_id=?",
                         (book_id, uid)).fetchone()
        if r is None:
            return False
        return True if not need_rw else (r["perm"] == "rw")

    def err(msg, code=400):
        from flask import jsonify
        return jsonify({"error": msg}), code

    # ------------------------- 账本 -------------------------

    @app.route("/api/ledger/books")
    def led_books():
        from flask import jsonify
        conn = get_db()
        uid = current_uid()
        ids = my_book_ids(conn, uid)
        out = []
        for b in conn.execute("SELECT * FROM ledger_book WHERE id IN (%s) ORDER BY id"
                              % ",".join("?" * len(ids)), ids).fetchall() if ids else []:
            n = conn.execute("SELECT COUNT(*) c, COALESCE(SUM(cents),0) s FROM entry WHERE book_id=?",
                             (b["id"],)).fetchone()
            mem = conn.execute("SELECT COUNT(*) c FROM book_member WHERE book_id=?", (b["id"],)).fetchone()["c"]
            out.append({"id": b["id"], "name": b["name"], "note": b["note"],
                        "owner_id": b["owner_id"], "mine": b["owner_id"] == uid,
                        "admin": int(b["owner_id"]) == 1,
                        "entries": n["c"], "sum_cents": n["s"], "members": mem})
        conn.close()
        return jsonify({"books": out, "default": out[0]["id"] if out else 0})

    @app.route("/api/ledger/books", methods=["POST"])
    def led_add_book():
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()[:20]
        if not name:
            return err("账本名字别空着")
        conn = get_db()
        uid = current_uid()
        cur = conn.execute("INSERT INTO ledger_book (name, owner_id, note) VALUES (?,?,?)",
                           (name, uid, str(data.get("note", "")).strip()[:60]))
        bid = cur.lastrowid
        conn.execute("INSERT OR IGNORE INTO book_member (book_id, user_id, perm) VALUES (?,?, 'rw')", (bid, uid))
        for nm in (data.get("members") or []):
            try:
                mid = int(nm)
            except (TypeError, ValueError):
                continue
            conn.execute("INSERT OR IGNORE INTO book_member (book_id, user_id, perm) VALUES (?,?, 'rw')", (bid, mid))
        for i, (nm, tp) in enumerate(DEFAULT_ACCOUNTS):
            conn.execute("INSERT INTO account (book_id,name,type,opening_cents,sort,owner_id) VALUES (?,?,?,?,?,?)",
                         (bid, nm, tp, 0, i, uid))
        for i, nm in enumerate(DEFAULT_EXPENSE):
            conn.execute("INSERT INTO category (book_id,name,kind,sort) VALUES (?,?,'expense',?)", (bid, nm, i))
        for i, nm in enumerate(DEFAULT_INCOME):
            conn.execute("INSERT INTO category (book_id,name,kind,sort) VALUES (?,?,'income',?)", (bid, nm, i))
        conn.commit()
        conn.close()
        return jsonify({"book": {"id": bid, "name": name}}), 201

    @app.route("/api/ledger/books/<int:bid>", methods=["PUT", "DELETE"])
    def led_edit_book(bid):
        from flask import jsonify, request
        conn = get_db()
        uid = current_uid()
        b = conn.execute("SELECT * FROM ledger_book WHERE id=?", (bid,)).fetchone()
        if b is None or not can_book(conn, uid, bid, need_rw=False):
            conn.close()
            return err("账本不存在或你无权访问", 404)
        if b["owner_id"] != uid and not is_admin():
            conn.close()
            return err("只有建账本的人能改名或归档", 403)
        if request.method == "DELETE":
            if request.args.get("wipe") != "1":
                # 默认行为：归档（从下拉里消失，账都还在）
                conn.execute("UPDATE ledger_book SET archived=1 WHERE id=?", (bid,))
                conn.commit()
                conn.close()
                return jsonify({"ok": 1, "archived": 1})
            # ?wipe=1：真删。整本先打一份快照进桶（流水/分类/账户/预算一起），30 天内可恢复
            import json as _j
            snap = {"book": dict(b),
                    "entries": [dict(r) for r in conn.execute("SELECT * FROM entry WHERE book_id=?", (bid,))],
                    "categories": [dict(r) for r in conn.execute("SELECT * FROM category WHERE book_id=?", (bid,))],
                    "accounts": [dict(r) for r in conn.execute("SELECT * FROM account WHERE book_id=?", (bid,))],
                    "budgets": [dict(r) for r in conn.execute("SELECT * FROM budget WHERE book_id=?", (bid,))]}
            n = len(snap["entries"])
            conn.execute("INSERT INTO ledger_trash(kind,book_id,payload,label,owner_id) VALUES (?,?,?,?,?)",
                         ("book", bid, _j.dumps(snap, ensure_ascii=False, default=str),
                          "账本「%s」· %d 笔流水" % (b["name"], n), b["owner_id"] or uid))
            for t in ("entry", "category", "account", "budget"):
                conn.execute("DELETE FROM %s WHERE book_id=?" % t, (bid,))
            conn.execute("DELETE FROM ledger_book WHERE id=?", (bid,))
            conn.execute("DELETE FROM book_member WHERE book_id=?", (bid,))
            conn.commit()
            conn.close()
            return jsonify({"ok": 1, "wiped": 1, "entries": n, "kept_days": TRASH_KEEP_DAYS})
        data = request.get_json(silent=True) or {}
        if data.get("name"):
            conn.execute("UPDATE ledger_book SET name=? WHERE id=?", (str(data["name"]).strip()[:20], bid))
        if "note" in data:
            conn.execute("UPDATE ledger_book SET note=? WHERE id=?", (str(data["note"] or "").strip()[:60], bid))
        if "members" in data:
            want = set()
            for m in (data.get("members") or []):
                try:
                    want.add((int(m if isinstance(m, (int, str)) and not isinstance(m, dict) else m["id"]),
                              m.get("perm", "rw") if isinstance(m, dict) else "rw"))
                except (TypeError, ValueError, KeyError, AttributeError):
                    continue
            have = {(r["user_id"], r["perm"]) for r in conn.execute("SELECT user_id,perm FROM book_member WHERE book_id=?", (bid,))}
            for pair in want:
                if pair not in have:
                    conn.execute("INSERT OR REPLACE INTO book_member (book_id,user_id,perm) VALUES (?,?,?)",
                                 (bid, pair[0], pair[1] if pair[1] in ("rw", "r") else "rw"))
            keep = {p[0] for p in want} | {b["owner_id"]}
            for r in conn.execute("SELECT user_id FROM book_member WHERE book_id=?", (bid,)).fetchall():
                if r["user_id"] not in keep:
                    conn.execute("DELETE FROM book_member WHERE book_id=? AND user_id=?", (bid, r["user_id"]))
        conn.commit()
        conn.close()
        return jsonify({"ok": 1})

    @app.route("/api/ledger/setup")
    def led_setup():
        """账本的账户与分类 + 各账户余额（一次给齐，页面不用再发多个请求）。"""
        from flask import jsonify, request
        conn = get_db()
        uid = current_uid()
        ids = my_book_ids(conn, uid)
        bid = request.args.get("book", type=int) or (ids[0] if ids else 0)
        if not bid or not can_book(conn, uid, bid, need_rw=False):
            conn.close()
            return err("账本不存在或你无权访问", 404)
        accounts = []
        for a in conn.execute("SELECT * FROM account WHERE book_id=? AND archived=0 ORDER BY sort,id",
                              (bid,)).fetchall():
            bal = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN dir='out' THEN -cents WHEN dir='in' THEN cents ELSE 0 END),0) s,"
                "       COALESCE(SUM(CASE WHEN dir='transfer' AND account_id=? THEN -cents ELSE 0 END),0) o,"
                "       COALESCE(SUM(CASE WHEN dir='transfer' AND to_account_id=? THEN cents ELSE 0 END),0) i"
                " FROM entry WHERE account_id=? OR to_account_id=?",
                (a["id"], a["id"], a["id"], a["id"])).fetchone()
            accounts.append({"id": a["id"], "name": a["name"], "icon": (a["icon"] if "icon" in a.keys() else ""),
                             "type": a["type"],
                             "opening": a["opening_cents"], "balance": int(a["opening_cents"] or 0)
                             + int(bal["s"] or 0) + int(bal["o"] or 0) + int(bal["i"] or 0),
                             "count": conn.execute("SELECT COUNT(*) c FROM entry WHERE account_id=? OR to_account_id=?",
                                                    (a["id"], a["id"])).fetchone()["c"]})
        cats = {"expense": [], "income": []}
        for c in conn.execute("SELECT * FROM category WHERE book_id=? ORDER BY kind,sort,id", (bid,)).fetchall():
            cats.setdefault(c["kind"], []).append({"id": c["id"], "name": c["name"], "color": c["color"],
                                                   "kind": c["kind"], "sort": c["sort"],
                                                   "icon": (c["icon"] if "icon" in c.keys() else "")})
        members = [{"id": r["user_id"], "perm": r["perm"],
                    "name": r["display_name"] or r["username"], "username": r["username"]}
                   for r in conn.execute(
                "SELECT m.user_id,m.perm,u.display_name,u.username FROM book_member m JOIN users u ON u.id=m.user_id "
                "WHERE m.book_id=? ORDER BY m.user_id", (bid,)).fetchall()]
        conn.close()
        return jsonify({"book": bid, "accounts": accounts, "categories": cats, "members": members})

    @app.route("/api/ledger/accounts", methods=["POST"])
    def led_add_account():
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()[:20]
        if not name:
            return err("账户名字别空着")
        conn = get_db()
        uid = current_uid()
        try:
            bid = int(data.get("book") or 0)
        except (TypeError, ValueError):
            conn.close()
            return err("账本参数不对")
        if not can_book(conn, uid, bid):
            conn.close()
            return err("账本不对", 403)
        opening = to_cents(data.get("opening", 0)) or 0
        cur = conn.execute("INSERT INTO account (book_id,name,type,opening_cents,sort,owner_id,icon)"
                           " VALUES (?,?,?,?,?,?,?)",
                           (bid, name, str(data.get("type") or "other")[:12], opening,
                            int(data.get("sort") or 0), uid, str(data.get("icon") or "").strip()[:4]))
        conn.commit()
        out = {"id": cur.lastrowid, "name": name, "type": str(data.get("type") or "other"),
               "opening": opening, "icon": str(data.get("icon") or "").strip()[:4]}
        conn.close()
        return jsonify({"account": out}), 201

    @app.route("/api/ledger/accounts/<int:aid>", methods=["PUT", "DELETE"])
    def led_edit_account(aid):
        from flask import jsonify, request
        conn = get_db()
        uid = current_uid()
        a = conn.execute("SELECT * FROM account WHERE id=?", (aid,)).fetchone()
        if a is None or not can_book(conn, uid, a["book_id"]):
            conn.close()
            return err("账户不存在或无权修改", 404)
        if request.method == "DELETE":
            used = conn.execute("SELECT COUNT(*) c FROM entry WHERE account_id=? OR to_account_id=?",
                                (aid, aid)).fetchone()["c"]
            if used and request.args.get("wipe") != "1":
                conn.close()
                return err("这个账户里有 %d 笔流水。选「停用」只是不再出现；要真删我会把这些流水的账户清空并进垃圾桶，30 天可恢复。" % used)
            _trash_snapshot(conn, "account", a["book_id"], a, a["name"])
            if used:      # 流水不删，只把账户留空（恢复账户时按快照重新挂回）
                conn.execute("UPDATE entry SET account_id=0, updated_at=datetime('now','localtime') "
                             "WHERE account_id=?", (aid,))
                conn.execute("UPDATE entry SET to_account_id=0, updated_at=datetime('now','localtime') "
                             "WHERE to_account_id=?", (aid,))
            if request.args.get("wipe") == "1":
                conn.execute("DELETE FROM account WHERE id=?", (aid,))
            else:
                conn.execute("UPDATE account SET archived=1 WHERE id=?", (aid,))
            conn.commit()
            conn.close()
            return jsonify({"ok": 1, "archived": request.args.get("wipe") != "1", "trashed": True})
        data = request.get_json(silent=True) or {}
        if data.get("name"):
            conn.execute("UPDATE account SET name=? WHERE id=?", (str(data["name"]).strip()[:20], aid))
        if "icon" in data:          # emoji 图标，可清空
            conn.execute("UPDATE account SET icon=? WHERE id=?", (str(data["icon"] or "").strip()[:4], aid))
        if "opening" in data:
            v = to_cents(data.get("opening"))
            if v is not None:
                conn.execute("UPDATE account SET opening_cents=? WHERE id=?", (v, aid))
        conn.commit()
        conn.close()
        return jsonify({"ok": 1})

    @app.route("/api/ledger/categories", methods=["POST"])
    def led_add_category():
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        name = str(data.get("name", "")).strip()[:12]
        kind = data.get("kind") if data.get("kind") in KINDS else "expense"
        if not name:
            return err("分类名字别空着")
        conn = get_db()
        uid = current_uid()
        bid = int(data.get("book") or 0)
        if not can_book(conn, uid, bid):
            conn.close()
            return err("账本不对", 403)
        if conn.execute("SELECT id FROM category WHERE book_id=? AND name=? AND kind=?",
                        (bid, name, kind)).fetchone():
            conn.close()
            return err("这个分类已经有了")
        cur = conn.execute("INSERT INTO category (book_id,name,kind,color,sort,icon) VALUES (?,?,?,?,?,?)",
                           (bid, name, kind, str(data.get("color") or "")[:9], int(data.get("sort") or 99),
                            str(data.get("icon") or "").strip()[:4]))
        conn.commit()
        out = {"id": cur.lastrowid, "name": name, "kind": kind}
        conn.close()
        return jsonify({"category": out}), 201

    @app.route("/api/ledger/categories/<int:cid>", methods=["PUT"])
    def led_edit_category(cid):
        """分类改名 / 换图标 / 调序号（收支方向不许改，避免把支出悄悄变成收入）。"""
        from flask import jsonify, request
        conn = get_db()
        uid = current_uid()
        c = conn.execute("SELECT * FROM category WHERE id=?", (cid,)).fetchone()
        if c is None or not can_book(conn, uid, c["book_id"]):
            conn.close()
            return err("分类不存在或无权修改", 404)
        d = request.get_json(silent=True) or {}
        nm = c["name"]
        if d.get("name"):
            nm = str(d["name"]).strip()[:12]
            if conn.execute("SELECT id FROM category WHERE book_id=? AND name=? AND kind=? AND id<>?",
                            (c["book_id"], nm, c["kind"], cid)).fetchone():
                conn.close()
                return err("这个收支下已经有同名分类")
            conn.execute("UPDATE category SET name=? WHERE id=?", (nm, cid))
        if "icon" in d:
            conn.execute("UPDATE category SET icon=? WHERE id=?", (str(d["icon"] or "").strip()[:4], cid))
        if "color" in d:
            conn.execute("UPDATE category SET color=? WHERE id=?", (str(d["color"] or "").strip()[:9], cid))
        if str(d.get("sort") or "").strip().lstrip("-").isdigit():
            conn.execute("UPDATE category SET sort=? WHERE id=?", (int(d["sort"]), cid))
        conn.commit()
        conn.close()
        return jsonify({"ok": 1, "id": cid, "name": nm})

    @app.route("/api/ledger/categories/<int:cid>", methods=["DELETE"])
    def led_del_category(cid):
        from flask import jsonify, request
        conn = get_db()
        uid = current_uid()
        c = conn.execute("SELECT * FROM category WHERE id=?", (cid,)).fetchone()
        if c is None or not can_book(conn, uid, c["book_id"]):
            conn.close()
            return err("分类不存在或无权删除", 404)
        used = conn.execute("SELECT COUNT(*) c FROM entry WHERE category_id=?", (cid,)).fetchone()["c"]
        if used and request.args.get("force") != "1":
            conn.close()
            return err("这个分类下有 %d 笔流水。要真删就点「仍然删除」，那几笔会变成未分类（流水本身不丢）" % used)
        hard = request.args.get("hard") == "1"
        if not hard:
            _trash_snapshot(conn, "category", c["book_id"], c, c["name"])
        # 分类下面如果还挂着流水，把它们挪到「其他」（没有就置 0＝未分类），流水本身一条不丢
        if used:
            other = conn.execute("SELECT id FROM category WHERE book_id=? AND name='其他'",
                                 (c["book_id"],)).fetchone()
            target = other["id"] if other else 0
            conn.execute("UPDATE entry SET category_id=?, updated_at=datetime('now','localtime') "
                         "WHERE category_id=?", (target, cid))
        conn.execute("DELETE FROM category WHERE id=?", (cid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": 1})

    # ------------------------- 流水 -------------------------

    def _acct_id(conn, bid, given, create_kind=None):
        """账户可以传 id，也可以直接传名字（如「微信」）；名字不存在就顺手建一个。"""
        if given in (None, ""):
            return 0
        if isinstance(given, (int,)) or str(given).isdigit():
            return int(given)
        nm = str(given).strip()[:20]
        r = conn.execute("SELECT id FROM account WHERE book_id=? AND name=? AND archived=0",
                         (bid, nm)).fetchone()
        if r:
            return r["id"]
        if create_kind:
            cur = conn.execute("INSERT INTO account (book_id,name,type,opening_cents,sort,owner_id)"
                               " VALUES (?,?,?,0,99,?)", (bid, nm, create_kind, current_uid()))
            return cur.lastrowid
        return 0

    def _resolve_cat(conn, bid, kind, given):
        """分类可以传 id 也可以传名字（名字不在就自动建，记一笔时不用先去做结构）。"""
        if given in (None, ""):
            r = conn.execute("SELECT id FROM category WHERE book_id=? AND kind=? AND name='其他'",
                             (bid, kind)).fetchone()
            return r["id"] if r else 0
        s = str(given).strip()
        if s.isdigit():
            return int(s)
        r = conn.execute("SELECT id FROM category WHERE book_id=? AND name=?", (bid, s[:12])).fetchone()
        if r:
            return r["id"]
        cur = conn.execute("INSERT INTO category (book_id,name,kind,sort) VALUES (?,?,?,?)",
                           (bid, s[:12], kind, 99))
        return cur.lastrowid

    @app.route("/api/ledger/entries", methods=["GET"])
    def led_entries():
        from flask import jsonify, request
        conn = get_db()
        uid = current_uid()
        ids = my_book_ids(conn, uid)
        bid = request.args.get("book", type=int) or (ids[0] if ids else 0)
        if not bid or not can_book(conn, uid, bid, need_rw=False):
            conn.close()
            return err("账本不存在或你无权访问", 404)
        mon = month_of(request.args.get("month") or date.today().isoformat())
        where = ["e.book_id=?", "substr(e.day,1,7)=?"]
        params = [bid, mon]
        if request.args.get("category"):
            where.append("e.category_id=?")
            params.append(int(request.args["category"]))
        if request.args.get("account"):
            where.append("(e.account_id=? OR e.to_account_id=?)")
            params += [int(request.args["account"])] * 2
        if request.args.get("dir"):
            where.append("e.dir=?")
            params.append(request.args["dir"])
        q = (request.args.get("q") or "").strip()
        if q:
            where.append("(e.note LIKE ? OR c.name LIKE ?)")
            params += ["%" + q + "%"] * 2
        sql_where = " WHERE " + " AND ".join(where)
        total = conn.execute("SELECT COUNT(*) c FROM entry e LEFT JOIN category c ON c.id=e.category_id"
                             + sql_where, params).fetchone()["c"]
        try:
            limit = max(1, min(int(request.args.get("limit") or 200), 1000))
            off = max(0, int(request.args.get("offset") or 0))
        except ValueError:
            limit, off = 200, 0
        rows = conn.execute(
            "SELECT e.*, c.name AS category_name, c.kind AS category_kind, c.color AS category_color,"
            " a.name AS account_name, t.name AS to_account_name, u.display_name AS by_name, u.username AS by_user"
            " FROM entry e"
            " LEFT JOIN category c ON c.id=e.category_id"
            " LEFT JOIN account a ON a.id=e.account_id"
            " LEFT JOIN account t ON t.id=e.to_account_id"
            " LEFT JOIN users u ON u.id=e.created_by"
            + sql_where + " ORDER BY e.day DESC, e.id DESC LIMIT ? OFFSET ?", params + [limit, off]).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            out.append({"id": d["id"], "day": d["day"], "dir": d["dir"], "cents": d["cents"],
                        "amount": fmt(d["cents"]), "note": d["note"],
                        "category": {"id": d["category_id"], "name": d["category_name"] or "",
                                     "kind": d["category_kind"] or "", "color": d["category_color"] or ""},
                        "account": {"id": d["account_id"], "name": d["account_name"] or ""},
                        "to_account": {"id": d["to_account_id"], "name": d["to_account_name"] or ""},
                        "by": d["by_name"] or d["by_user"] or "", "created_by": d["created_by"],
                        "mine": d["created_by"] == uid})
        conn.close()
        return jsonify({"entries": out, "total": total, "month": mon, "book": bid})

    @app.route("/api/ledger/entries", methods=["POST"])
    def led_add_entry():
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        conn = get_db()
        uid = current_uid()
        try:
            bid = int(data.get("book") or 0)
        except (TypeError, ValueError):
            conn.close()
            return err("账本参数不对")
        if not bid:
            ids = my_book_ids(conn, uid)
            bid = ids[0] if ids else 0
        if not bid or not can_book(conn, uid, bid):
            conn.close()
            return err("账本不对或你只有只读权限", 403)
        direction = str(data.get("dir") or "")
        if direction not in DIRS:
            conn.close()
            return err("方向只能是 支出/收入/转账")
        cents = to_cents(data.get("amount"))
        if cents is None or cents == 0:
            conn.close()
            return err("金额要填对，比如 12 或 12.34")
        if direction == "out" and cents < 0:
            cents = -cents
        if direction == "in" and cents < 0:
            conn.close()
            return err("收入金额别写负数")
        if direction == "transfer" and cents < 0:
            conn.close()
            return err("转账金额别写负数")
        day = valid_day(data.get("day")) or date.today().isoformat()
        acc = _acct_id(conn, bid, data.get("account"), "other")
        to_acc = _acct_id(conn, bid, data.get("to_account"), "other")
        for a in (acc, to_acc):
            if a and conn.execute("SELECT id FROM account WHERE id=? AND book_id=?", (a, bid)).fetchone() is None:
                conn.close()
                return err("账户不属于这个账本")
        if direction == "transfer":
            if not acc or not to_acc or acc == to_acc:
                conn.close()
                return err("转账要选好「从哪个账户」和「到哪个账户」，而且不能是同一个")
            kind = "expense"
            cid = 0
        else:
            kind = "income" if direction == "in" else "expense"
            cid = _resolve_cat(conn, bid, kind, data.get("category"))
            if not acc:
                return err("先选一个账户")
        cur = conn.execute(
            "INSERT INTO entry (book_id,account_id,to_account_id,category_id,day,cents,dir,note,created_by)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (bid, acc, to_acc if direction == "transfer" else 0, cid, day, abs(cents), direction,
             str(data.get("note") or "")[:120], uid))
        conn.commit()
        r = conn.execute("SELECT * FROM entry WHERE id=?", (cur.lastrowid,)).fetchone()
        conn.close()
        return jsonify({"entry": {"id": r["id"], "day": r["day"], "dir": r["dir"], "cents": r["cents"],
                                  "amount": fmt(r["cents"]), "note": r["note"]}}), 201

    @app.route("/api/ledger/entries/<int:eid>", methods=["PUT", "DELETE"])
    def led_edit_entry(eid):
        from flask import jsonify, request
        conn = get_db()
        uid = current_uid()
        e = conn.execute("SELECT * FROM entry WHERE id=?", (eid,)).fetchone()
        if e is None or not can_book(conn, uid, e["book_id"]):
            conn.close()
            return err("流水不存在或无权修改", 404)
        if request.method == "DELETE":
            if request.args.get("hard") != "1":
                _trash_snapshot(conn, "entry", e["book_id"], e, _entry_label(e))
            conn.execute("DELETE FROM entry WHERE id=?", (eid,))
            conn.commit()
            conn.close()
            return jsonify({"ok": 1, "trashed": request.args.get("hard") != "1",
                            "kept_days": TRASH_KEEP_DAYS})
        data = request.get_json(silent=True) or {}
        cents = to_cents(data.get("amount")) if "amount" in data else e["cents"]
        if cents is None or cents == 0:
            conn.close()
            return err("金额要填对")
        day = valid_day(data.get("day")) or e["day"]
        direction = data.get("dir") if data.get("dir") in DIRS else e["dir"]
        acc = _acct_id(conn, e["book_id"], data.get("account", e["account_id"]), "other")
        to_acc = _acct_id(conn, e["book_id"], data.get("to_account", e["to_account_id"]), "other")
        cid = e["category_id"]
        if "category" in data and direction != "transfer":
            cid = _resolve_cat(conn, e["book_id"], "income" if direction == "in" else "expense", data.get("category"))
        conn.execute(
            "UPDATE entry SET cents=?, day=?, dir=?, account_id=?, to_account_id=?, category_id=?, note=?,"
            " updated_at=datetime('now','localtime') WHERE id=?",
            (abs(cents), day, direction, acc, to_acc, cid, str(data.get("note", e["note"]) or "")[:120], eid))
        conn.commit()
        conn.close()
        return jsonify({"ok": 1})

    # ------------------------- 汇总（分类/环比/每日） -------------------------

    @app.route("/api/ledger/summary")
    def led_summary():
        from flask import jsonify, request
        conn = get_db()
        uid = current_uid()
        ids = my_book_ids(conn, uid)
        bid = request.args.get("book", type=int) or (ids[0] if ids else 0)
        if not bid or not can_book(conn, uid, bid, need_rw=False):
            conn.close()
            return err("账本不存在或你无权访问", 404)
        mon = month_of(request.args.get("month") or date.today().isoformat())
        pm = prev_month(mon)

        def agg(period):
            row = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN dir='out' THEN cents END),0) expense,"
                "       COALESCE(SUM(CASE WHEN dir='in' THEN cents END),0) income,"
                "       COUNT(*) n FROM entry WHERE book_id=? AND substr(day,1,7)=?",
                (bid, period)).fetchone()
            return {"expense": int(row["expense"] or 0), "income": int(row["income"] or 0), "n": int(row["n"] or 0)}

        cur = agg(mon)
        pre = agg(pm) if pm else {"expense": 0, "income": 0, "n": 0}
        cats = {}
        for r in conn.execute(
                "SELECT e.category_id cid, COALESCE(c.name,'未分类') nm, COALESCE(c.kind,'expense') kind,"
                "       COALESCE(c.color,'') color, SUM(e.cents) s, COUNT(*) n"
                " FROM entry e LEFT JOIN category c ON c.id=e.category_id"
                " WHERE e.book_id=? AND substr(e.day,1,7)=? AND e.dir IN ('out','in')"
                " GROUP BY e.category_id ORDER BY s DESC", (bid, mon)).fetchall():
            cats.setdefault(r["kind"], []).append({"id": r["cid"], "name": r["nm"], "color": r["color"],
                                                   "cents": int(r["s"] or 0), "count": int(r["n"] or 0)})
        daily = {r["d"]: int(r["s"] or 0) for r in conn.execute(
            "SELECT day d, COALESCE(SUM(CASE WHEN dir='out' THEN cents ELSE -cents END),0) s"
            " FROM entry WHERE book_id=? AND substr(day,1,7)=? AND dir IN ('out','in') GROUP BY day",
            (bid, mon)).fetchall()}
        trend = [dict(zip(["period", "expense", "income"],
                          (r["p"], int(r["e"] or 0), int(r["i"] or 0))))
                 for r in conn.execute(
            "SELECT substr(day,1,7) p,"
            " SUM(CASE WHEN dir='out' THEN cents END) e, SUM(CASE WHEN dir='in' THEN cents END) i"
            " FROM entry WHERE book_id=? AND dir IN ('out','in')"
            " GROUP BY p ORDER BY p DESC LIMIT 12", (bid,)).fetchall()]
        bud = []
        try:
            for r in conn.execute(
                    "SELECT b.id, b.category_id, b.cents, COALESCE(c.name,'（分类已删）') nm"
                    " FROM budget b LEFT JOIN category c ON c.id=b.category_id"
                    " WHERE b.book_id=? AND b.period=? ORDER BY b.cents DESC", (bid, mon)).fetchall():
                used = int(conn.execute(
                    "SELECT COALESCE(SUM(cents),0) s FROM entry WHERE book_id=? AND category_id=?"
                    " AND dir='out' AND substr(day,1,7)=?", (bid, r["category_id"], mon)).fetchone()["s"] or 0)
                pct = round(used * 100.0 / r["cents"], 1) if r["cents"] else 0.0
                bud.append({"id": r["id"], "name": r["nm"], "limit": int(r["cents"]), "used": used,
                            "left": int(r["cents"]) - used, "pct": pct,
                            "state": "over" if pct > 100 else ("warn" if pct >= BUDGET_WARN_PCT else "ok"),
                            "limit_text": fmt(r["cents"]), "used_text": fmt(used)})
        except sqlite3.Error as e:
            app.logger.warning("预算统计查询失败：%s", e)
            bud = []
        conn.close()
        mom = None
        if pre["expense"]:
            mom = round((cur["expense"] - pre["expense"]) * 100.0 / pre["expense"], 1)
        return jsonify({
            "budgets": bud, "warn_pct": BUDGET_WARN_PCT,
            "book": bid, "month": mon,
            "expense": cur["expense"], "income": cur["income"], "balance": cur["income"] - cur["expense"],
            "count": cur["n"],
            "prev": {"month": pm, "expense": pre["expense"], "income": pre["income"]},
            "mom_pct": mom,
            "by_category": cats, "daily": daily, "trend": sorted(trend, key=lambda x: x["period"]),
            "expense_text": fmt(cur["expense"]), "income_text": fmt(cur["income"]),
        })

    # 给日历用的两块数据（不改 items 表，避免同一笔钱两处存）
    def day_ledger(day_str):
        """某天账目：给 /api/day 用。原来这里写了 c.name 却没 JOIN category，
        SQL 报错被 except 吞成「今天没账」，看着像功能没数据 —— 现在不再吞错。"""
        conn = get_db()
        uid = current_uid()
        rows = conn.execute(
            "SELECT e.dir d, e.cents c FROM entry e"
            " JOIN book_member m ON m.book_id=e.book_id AND m.user_id=?"
            " WHERE e.day=? AND e.dir IN ('out','in') ORDER BY e.id", (uid, day_str)).fetchall()
        list_rows = conn.execute(
            "SELECT e.id, e.dir, e.cents, COALESCE(c.name,'未分类') cname, COALESCE(a.name,'') aname, e.note"
            " FROM entry e LEFT JOIN category c ON c.id=e.category_id"
            " LEFT JOIN account a ON a.id=e.account_id"
            " JOIN book_member m ON m.book_id=e.book_id AND m.user_id=? WHERE e.day=? ORDER BY e.id",
            (uid, day_str)).fetchall()
        conn.close()
        out_c = sum(int(r["c"]) for r in rows if r["d"] == "out")
        in_c = sum(int(r["c"]) for r in rows if r["d"] == "in")
        return {"day": day_str, "expense": out_c, "income": in_c, "balance": in_c - out_c,
                "count": len(rows), "expense_text": fmt(out_c), "income_text": fmt(in_c),
                "items": [{"id": r["id"], "dir": r["dir"], "cents": int(r["cents"]),
                           "amount": fmt(r["cents"]), "category": r["cname"], "account": r["aname"],
                           "note": r["note"]} for r in list_rows]}

    def month_ledger(ym):
        conn = get_db()
        uid = current_uid()
        rows = conn.execute(
            "SELECT e.day d,"
            " COALESCE(SUM(CASE WHEN e.dir='out' THEN e.cents END),0) o,"
            " COALESCE(SUM(CASE WHEN e.dir='in' THEN e.cents END),0) i"
            " FROM entry e JOIN book_member m ON m.book_id=e.book_id AND m.user_id=?"
            " WHERE substr(e.day,1,7)=? AND e.dir IN ('out','in') GROUP BY e.day", (uid, ym)).fetchall()
        conn.close()
        return {r["d"]: {"out": int(r["o"] or 0), "in": int(r["i"] or 0)} for r in rows}

    # ================= 第 4 期：预算 / 图表 / 导入导出 =================

    BUDGET_WARN_PCT = float(os.environ.get("BUDGET_WARN_PCT", "80"))

    def dec_cents(v):
        """金额用 Decimal 解析成「分」，不走浮点；解不出来返回 None（由调用方计为失败行，
        绝不悄悄当 0 或跳过不算）。超过两位小数按四舍五入到分。"""
        from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
        t = str(v if v is not None else "").strip().replace("￥", "").replace("\u00a5", "")
        t = t.replace(",", "").replace("元", "").replace(" ", "")
        if not t:
            return None
        try:
            d = Decimal(t)
        except (InvalidOperation, ValueError):
            return None
        if not d.is_finite():
            return None
        return int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))

    def norm_day(v):
        t = str(v or "").strip()[:19]
        if not t:
            return None
        t = t.replace("/", "-").replace(".", "-")
        m = re.match(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
        if not m:
            return None
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3))).isoformat()
        except ValueError:
            return None

    # ---------------- 预算 ----------------
    @app.route("/api/ledger/budgets")
    def led_budgets():
        from flask import jsonify, request
        conn = get_db()
        uid = current_uid()
        ids = my_book_ids(conn, uid)
        bid = request.args.get("book", type=int) or (ids[0] if ids else 0)
        if not bid or not can_book(conn, uid, bid, need_rw=False):
            conn.close()
            return err("账本不存在或你无权访问", 404)
        mon = month_of(request.args.get("month") or date.today().isoformat())
        rows = conn.execute(
            "SELECT b.id, b.category_id, b.cents, COALESCE(c.name,'（分类已删）') nm"
            " FROM budget b LEFT JOIN category c ON c.id=b.category_id"
            " WHERE b.book_id=? AND b.period=? ORDER BY b.cents DESC", (bid, mon)).fetchall()
        out, tot_l, tot_u = [], 0, 0
        for r in rows:
            used = int(conn.execute(
                "SELECT COALESCE(SUM(cents),0) s FROM entry WHERE book_id=? AND category_id=?"
                " AND dir='out' AND substr(day,1,7)=?", (bid, r["category_id"], mon)).fetchone()["s"] or 0)
            pct = round(used * 100.0 / r["cents"], 1) if r["cents"] else 0.0
            out.append({"id": r["id"], "category_id": r["category_id"], "name": r["nm"],
                        "limit": int(r["cents"]), "used": used, "left": int(r["cents"]) - used,
                        "pct": pct, "state": "over" if pct > 100 else ("warn" if pct >= BUDGET_WARN_PCT else "ok"),
                        "limit_text": fmt(r["cents"]), "used_text": fmt(used)})
            tot_l += int(r["cents"]); tot_u += used
        conn.close()
        tpct = round(tot_u * 100.0 / tot_l, 1) if tot_l else 0.0
        return jsonify({"month": mon, "book": bid, "warn_pct": BUDGET_WARN_PCT, "budgets": out,
                        "total": {"limit": tot_l, "used": tot_u, "pct": tpct,
                                  "state": "over" if tpct > 100 else ("warn" if tpct >= BUDGET_WARN_PCT else "ok"),
                                  "limit_text": fmt(tot_l), "used_text": fmt(tot_u)}})

    @app.route("/api/ledger/budgets", methods=["POST"])
    def led_set_budget():
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        conn = get_db()
        uid = current_uid()
        bid = int(data.get("book") or 0)
        if not can_book(conn, uid, bid):
            conn.close()
            return err("账本不对", 403)
        mon = month_of(data.get("period")) or date.today().isoformat()[:7]
        cents = dec_cents(data.get("amount"))
        if cents is None or cents <= 0:
            conn.close()
            return err("预算金额要填对（比如 1500）")
        cid = data.get("category")
        cid = int(cid) if str(cid).isdigit() else 0
        if not cid:
            nm = str(data.get("category_name") or "").strip()[:12]
            row = conn.execute("SELECT id FROM category WHERE book_id=? AND name=? AND kind='expense'",
                               (bid, nm)).fetchone()
            if row is None and nm:
                row = {"id": conn.execute("INSERT INTO category (book_id,name,kind,sort) VALUES (?,?,'expense',99)",
                                          (bid, nm)).lastrowid}
            cid = row["id"] if row else 0
        if not cid:
            conn.close()
            return err("要说给哪个分类设预算")
        if conn.execute("SELECT id FROM category WHERE id=? AND book_id=?", (cid, bid)).fetchone() is None:
            conn.close()
            return err("分类不属于这个账本")
        conn.execute("INSERT OR REPLACE INTO budget (book_id,category_id,period,cents) VALUES (?,?,?,?)",
                     (bid, cid, mon, cents))
        conn.commit()
        nm = conn.execute("SELECT name FROM category WHERE id=?", (cid,)).fetchone()
        conn.close()
        return jsonify({"ok": 1, "category": nm["name"] if nm else "", "period": mon,
                        "amount": fmt(cents)}), 201

    @app.route("/api/ledger/budgets/<int:bdid>", methods=["DELETE"])
    def led_del_budget(bdid):
        from flask import jsonify
        conn = get_db()
        uid = current_uid()
        r = conn.execute("SELECT * FROM budget WHERE id=?", (bdid,)).fetchone()
        if r is None or not can_book(conn, uid, r["book_id"]):
            conn.close()
            return err("预算不存在", 404)
        conn.execute("DELETE FROM budget WHERE id=?", (bdid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": 1})

    # ---------------- 导出 ----------------
    @app.route("/api/ledger/trash")
    def led_trash():
        """记账垃圾桶：谁删的、什么时候、原来长什么样、还剩几天。"""
        from flask import jsonify, request
        conn = get_db()
        purge_ledger_trash(conn)
        uid = current_uid()
        admin = bool(ctx.get("is_admin") and ctx["is_admin"]())
        ids = my_book_ids(conn, uid)
        w = "" if admin else (" WHERE (owner_id=? OR book_id IN (%s))" % ",".join("?" * len(ids)) if ids else " WHERE owner_id=?")
        p = [] if admin else ([uid] + (ids or []) if ids else [uid])
        out = []
        for r in conn.execute("SELECT * FROM ledger_trash" + w + " ORDER BY deleted_at DESC LIMIT 500", p):
            try:
                left = TRASH_KEEP_DAYS - (datetime.now() - datetime.strptime(
                    r["deleted_at"], "%Y-%m-%d %H:%M:%S")).days
            except (ValueError, TypeError):
                left = TRASH_KEEP_DAYS
            out.append({"id": r["id"], "kind": r["kind"], "book_id": r["book_id"], "label": r["label"],
                        "deleted_at": r["deleted_at"], "left_days": max(0, left)})
        conn.close()
        return jsonify({"items": out, "count": len(out), "kept_days": TRASH_KEEP_DAYS})

    def _restore_book(conn, row):
        """把整本账（账本/分类/账户/流水/预算）按快照插回去。名字冲突就加后缀。"""
        b = row.get("book") or {}
        nm = b.get("name") or "恢复的账本"
        if conn.execute("SELECT id FROM ledger_book WHERE name=? AND owner_id=?",
                        (nm, b.get("owner_id") or current_uid())).fetchone():
            nm = nm + "（恢复）"
        cur = conn.execute("INSERT INTO ledger_book (name,owner_id,note,archived) VALUES (?,?,?,0)",
                           (nm[:20], b.get("owner_id") or current_uid(), b.get("note") or ""))
        nb = cur.lastrowid
        catmap, accmap = {}, {}
        for c in row.get("categories") or []:
            cur2 = conn.execute("INSERT INTO category (book_id,name,kind,color,sort,icon) VALUES (?,?,?,?,?,?)",
                                (nb, c.get("name"), c.get("kind"), c.get("color") or "", c.get("sort") or 99,
                                 c.get("icon") or ""))
            catmap[c.get("id")] = cur2.lastrowid
        for a in row.get("accounts") or []:
            cur3 = conn.execute("INSERT INTO account (book_id,name,type,opening_cents,sort,owner_id,archived)"
                                " VALUES (?,?,?,?,?,?,?)",
                                (nb, a.get("name"), a.get("type") or "cash", a.get("opening_cents") or 0,
                                 a.get("sort") or 99, a.get("owner_id") or current_uid(), a.get("archived") or 0))
            accmap[a.get("id")] = cur3.lastrowid
        n = 0
        for e in row.get("entries") or []:
            conn.execute("INSERT INTO entry (book_id,account_id,to_account_id,category_id,day,cents,dir,note,"
                         "created_by,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                         (nb, accmap.get(e.get("account_id"), 0), accmap.get(e.get("to_account_id"), 0),
                          catmap.get(e.get("category_id"), 0), e.get("day"), e.get("cents"), e.get("dir"),
                          e.get("note") or "", e.get("created_by") or current_uid(),
                          e.get("created_at") or None, e.get("updated_at") or None))
            n += 1
        for bd in row.get("budgets") or []:
            conn.execute("INSERT INTO budget (book_id,category_id,month,cents) VALUES (?,?,?,?)",
                         (nb, catmap.get(bd.get("category_id"), 0), bd.get("month"), bd.get("cents") or 0))
        conn.commit()
        return n


    @app.route("/api/ledger/trash/restore", methods=["POST"])
    def led_trash_restore():
        """恢复：流水按原样插回；分类把图标/颜色/序号都还原。"""
        from flask import jsonify, request
        d = request.get_json(silent=True) or {}
        ids = [int(x) for x in (d.get("ids") or []) if str(x).strip().isdigit()]
        conn = get_db()
        back = 0
        for tid in ids[:200]:
            t = conn.execute("SELECT * FROM ledger_trash WHERE id=?", (tid,)).fetchone()
            if t is None:
                continue
            try:
                row = json.loads(t["payload"])
            except ValueError:
                conn.execute("DELETE FROM ledger_trash WHERE id=?", (tid,))
                continue
            if t["kind"] == "book":
                try:
                    row = json.loads(t["payload"])
                except ValueError:
                    row = {}
                back += _restore_book(conn, row)
                conn.execute("DELETE FROM ledger_trash WHERE id=?", (tid,))
                continue
            if t["kind"] == "entry":
                cur = conn.execute(
                    "INSERT INTO entry (book_id,account_id,to_account_id,category_id,day,cents,dir,note,created_by)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (row.get("book_id"), row.get("account_id"), row.get("to_account_id") or 0,
                     row.get("category_id"), row.get("day"), row.get("cents"), row.get("dir"),
                     row.get("note") or "", row.get("created_by") or current_uid()))
                conn.execute("UPDATE entry SET updated_at=datetime('now','localtime') WHERE id=?",
                             (cur.lastrowid,))
            elif t["kind"] == "book":
                rows = json.loads('{"rows":[]}')      # 占位，真正恢复在下面函数里做
            elif t["kind"] == "account":
                conn.execute("INSERT INTO account (book_id,name,type,opening_cents,sort,owner_id,archived,icon)"
                             " VALUES (?,?,?,?,?,?,0,?)",
                             (row.get("book_id"), row.get("name"), row.get("type") or "cash",
                              row.get("opening_cents") or 0, row.get("sort") or 99,
                              row.get("owner_id") or current_uid(), str(row.get("icon") or "")[:4]))
            elif t["kind"] == "category":
                conn.execute(
                    "INSERT OR IGNORE INTO category (book_id,name,kind,color,sort,icon) VALUES (?,?,?,?,?,?)",
                    (row.get("book_id"), row.get("name"), row.get("kind"), row.get("color") or "",
                     row.get("sort") or 99, row.get("icon") or ""))
            conn.execute("DELETE FROM ledger_trash WHERE id=?", (tid,))
            back += 1
        conn.commit()
        conn.close()
        return jsonify({"ok": 1, "restored": back})

    @app.route("/api/ledger/trash/purge", methods=["POST"])
    def led_trash_purge():
        """彻底删除（不留快照，不可再恢复）。"""
        from flask import jsonify, request
        d = request.get_json(silent=True) or {}
        ids = [int(x) for x in (d.get("ids") or []) if str(x).strip().isdigit()]
        conn = get_db()
        n = 0
        if not ids or (len(ids) == 1 and ids[0] == 0):        # ids:[0] = 全清
            n = conn.execute("SELECT COUNT(*) c FROM ledger_trash").fetchone()["c"]
            conn.execute("DELETE FROM ledger_trash")
        else:
            ph = ",".join("?" * len(ids))
            n = conn.execute("SELECT COUNT(*) c FROM ledger_trash WHERE id IN (%s)" % ph, ids).fetchone()["c"]
            conn.execute("DELETE FROM ledger_trash WHERE id IN (%s)" % ph, ids)
        conn.commit()
        conn.close()
        return jsonify({"ok": 1, "purged": n})

    @app.route("/api/ledger/export")
    def led_export():
        import csv as _csv
        from flask import request, Response
        conn = get_db()
        uid = current_uid()
        ids = my_book_ids(conn, uid)
        bid = request.args.get("book", type=int) or (ids[0] if ids else 0)
        if not bid or not can_book(conn, uid, bid, need_rw=False):
            conn.close()
            return err("账本不存在或你无权访问", 404)
        mon = request.args.get("month") or ""
        b = conn.execute("SELECT name FROM ledger_book WHERE id=?", (bid,)).fetchone()
        sql = ("SELECT e.*, COALESCE(c.name,'') cn, COALESCE(a.name,'') an, COALESCE(t.name,'') tn"
               " FROM entry e LEFT JOIN category c ON c.id=e.category_id"
               " LEFT JOIN account a ON a.id=e.account_id LEFT JOIN account t ON t.id=e.to_account_id"
               " WHERE e.book_id=?")
        args = [bid]
        if mon:
            sql += " AND substr(e.day,1,7)=?"
            args.append(month_of(mon))
        sql += " ORDER BY e.day, e.id"
        buf = io.StringIO()
        w = _csv.writer(buf)
        w.writerow(["日期", "方向", "金额", "分类", "账户", "转入账户", "备注", "记账人"])
        DIR_CN = {"out": "支出", "in": "收入", "transfer": "转账"}
        n = 0
        for r in conn.execute(sql, args).fetchall():
            w.writerow([r["day"], DIR_CN.get(r["dir"], r["dir"]), fmt(r["cents"]), r["cn"], r["an"],
                        r["tn"], r["note"], str(r["created_by"])])
            n += 1
        conn.close()
        fn = "记账导出-%s-%s.csv" % ((b["name"] if b else "账本"), (month_of(mon) if mon else "全部"))
        data = buf.getvalue().encode("utf-8-sig")     # 带 BOM，Excel 打开中文不乱码
        resp = Response(data, mimetype="text/csv")
        resp.headers["Content-Disposition"] = "attachment; filename*=UTF-8''%s" % quote(fn)
        resp.headers["Cache-Control"] = "no-store"
        return resp

    # ---------------- 导入（自动识别本应用 / 微信 / 支付宝） ----------------
    # 关键词 → 我们的分类名。微信「交易类型」和支付宝「交易分类」用词不同，各自映射；
    # 命不中的一律归「其他」并把原始分类留在备注里，不丢信息、不瞎猜。
    WECHAT_CAT = {"餐饮": "餐饮", "餐厅": "餐饮", "外卖": "餐饮", "美团": "餐饮", "饿了么": "餐饮",
                  "交通": "交通", "出行": "交通", "打车": "交通", "地铁": "交通", "公交": "交通",
                  "加油": "交通", "停车": "交通", "火车": "交通", "机票": "交通", "航班": "交通",
                  "购物": "购物", "淘宝": "购物", "京东": "购物", "拼多多": "购物", "超市": "购物",
                  "日用": "购物", "服饰": "购物", "数码": "购物",
                  "住房": "居住", "物业": "居住", "水电": "居住", "燃气": "居住", "房租": "居住",
                  "医疗": "医药", "药品": "医药", "医院": "医药",
                  "通讯": "通讯", "话费": "通讯", "流量": "通讯", "宽带": "通讯",
                  "娱乐": "娱乐", "游戏": "娱乐", "电影": "娱乐", "视频": "娱乐", "会员": "娱乐",
                  "转账": "人情", "红包": "人情", "人情": "人情", "礼物": "人情",
                  "工资": "工资", "报销": "报销", "退款": "其他", "信用卡": "其他",
                  "还款": "其他", "保险": "其他", "教育": "其他", "培训": "其他"}

    @app.route("/api/ledger/import", methods=["POST"])
    def led_import():
        import csv as _csv
        from flask import jsonify, request
        f = request.files.get("file")
        if f is not None and f.filename:
            raw = f.read()
        else:
            raw = str((request.get_json(silent=True) or {}).get("csv", "")).encode("utf-8")
        if len(raw) > 8 * 1024 * 1024:
            return err("账单文件太大（上限 8MB）")
        try:
            lines = read_bill_lines(raw, getattr(f, "filename", "") or "")
        except ValueError as e:
            return err(str(e))
        except Exception as e:                       # 损坏的 xlsx 等
            return err("这个文件读不出来：%s" % str(e)[:60])
        if not lines:
            return err("文件是空的或没有数据行")

        conn = get_db()
        uid = current_uid()
        ids = my_book_ids(conn, uid)
        form = request.form if f is not None and f.filename else (request.get_json(silent=True) or {})
        def g(k, dft=""):
            try:
                return form.get(k, dft) if hasattr(form, "get") else dft
            except Exception:
                return dft
        try:
            bid = int(g("book") or 0) or (ids[0] if ids else 0)
        except (TypeError, ValueError):
            conn.close()
            return err("账本参数不对")
        if not bid or not can_book(conn, uid, bid):
            conn.close()
            return err("账本不对或你只有只读权限", 403)
        dry = str(g("dry_run", "1")) in ("1", "true", "yes")
        acc_def = _acct_id(conn, bid, g("account") or None, "other")
        if not acc_def:
            acc_def = conn.execute("SELECT id FROM account WHERE book_id=? AND archived=0 ORDER BY sort,id",
                                   (bid,)).fetchone()
            acc_def = acc_def["id"] if acc_def else 0
        if not acc_def:
            conn.close()
            return err("这个账本还没有账户，先建一个再导入")
        catmap = {r["name"]: r["id"] for r in
                  conn.execute("SELECT id,name FROM category WHERE book_id=?", (bid,)).fetchall()}

        # 找表头行：微信/支付宝导出前面可能有若干说明行，必须扫到真正含「交易时间」的那一行
        header_row, hdr = None, None
        for i, ln in enumerate(lines[:60]):
            cells = next(_csv.reader([ln]))
            joined = "".join(cells)
            if ("交易时间" in joined and ("金额" in joined or "收入金额" in joined)) or "日期" in cells and "方向" in cells:
                header_row, hdr = i, [c.strip().replace("\ufeff", "") for c in cells]
                break
        if hdr is None:
            conn.close()
            return err("没找到表头行（要含「交易时间」「金额」或本应用导出的「日期/方向/金额」）")
        joined = "".join(hdr)
        if "日期" in hdr and "方向" in hdr:
            source = "calnote"
        elif "金额(元)" in joined or "交易类型" in joined or "当前状态" in joined:
            # 「收/付款方式」微信支付宝都有，不能用来分辨；这三列才是微信账单独有的
            source = "wechat"
        else:
            source = "alipay"

        def col(*names):
            for nm in names:
                for j, h in enumerate(hdr):
                    if h == nm or (nm in h and len(h) <= len(nm) + 4):
                        return j
            return None

        def cell(row, j):
            return (row[j].strip() if j is not None and j < len(row) else "")

        c_day = col("交易时间", "日期")
        c_amt = col("金额(元)", "金额", "支出金额", "收入金额")
        c_io = col("收/支", "方向")
        c_other = col("交易对方", "对方账号", "商家")
        c_type = col("交易类型", "交易分类", "分类")
        c_status = col("当前状态", "交易状态", "状态")
        c_note = col("备注", "商品说明", "商品", "交易备注")
        if c_day is None or c_amt is None:
            conn.close()
            return err("表头认出来了，但缺少「交易时间/日期」或「金额」列：%s" % "、".join(hdr[:8]))

        SKIP_STATUS = ("交易关闭", "已关闭", "已全额退款", "退款成功", "已退款")
        DIR_ALIAS = {"支出": "out", "收入": "in", "out": "out", "in": "in", "转账": "transfer"}
        added, dup, skipped, failed = 0, 0, 0, 0
        samples, errors = [], []
        seen = set()
        for r0 in conn.execute("SELECT day,cents,dir,note FROM entry WHERE book_id=?", (bid,)).fetchall():
            seen.add((r0["day"], int(r0["cents"]), r0["dir"], (r0["note"] or "").strip()[:40]))

        for ln in lines[header_row + 1:]:
            row = next(_csv.reader([ln]))
            if not row or not any(c.strip() for c in row):
                continue
            day = norm_day(cell(row, c_day))
            cents = dec_cents(cell(row, c_amt))
            if not day or cents is None or cents == 0:
                failed += 1
                if len(errors) < 12:
                    errors.append("跳过一行：%s" % (ln[:70]))
                continue
            iotxt = cell(row, c_io)
            direction = DIR_ALIAS.get(iotxt)
            if source != "calnote" and not direction:
                skipped += 1          # 微信/支付宝里「/」「不计收入」这类本来就不该入账
                continue
            if not direction:
                failed += 1
                if len(errors) < 12:
                    errors.append("方向认不出：%s" % ln[:70])
                continue
            if source == "calnote":
                cents = abs(cents)
            else:
                st = cell(row, c_status)
                if any(k in st for k in SKIP_STATUS):
                    skipped += 1
                    continue
                if direction == "out" and "退款" in (cell(row, c_type) + st):
                    direction = "in"
            raw_cat = cell(row, c_type)
            other = cell(row, c_other)
            note = cell(row, c_note)
            if source == "calnote":
                cat_name = raw_cat or "其他"
            else:
                key = (raw_cat + other + note)
                cat_name = next((v for k, v in WECHAT_CAT.items() if k in key), "其他")
                note = " ｜".join([x for x in ((raw_cat or "").strip(), other.strip(), note.strip()) if x])[:120]
            cid = catmap.get(cat_name)
            if cid is None:
                if dry:
                    cid = -1                     # 预览模式不建分类，只报会归到哪个名字
                else:
                    ck = "income" if direction == "in" else "expense"
                    cid = conn.execute("INSERT INTO category (book_id,name,kind,sort) VALUES (?,?,?,99)",
                                       (bid, cat_name, ck)).lastrowid
                    catmap[cat_name] = cid
            sig = (day, abs(cents), direction, note.strip()[:40])
            if sig in seen:
                dup += 1
                continue
            seen.add(sig)
            added += 1
            if len(samples) < 8:
                samples.append({"day": day, "dir": direction, "amount": fmt(abs(cents)),
                                "category": cat_name, "note": note[:40]})
            if not dry:
                conn.execute(
                    "INSERT INTO entry (book_id,account_id,to_account_id,category_id,day,cents,dir,note,created_by)"
                    " VALUES (?,?,0,?,?,?,?,?,?)",
                    (bid, acc_def, cid, day, abs(cents), direction, note[:120], uid))
        if not dry:
            conn.commit()
        conn.close()
        return jsonify({"source": {"calnote": "本应用导出", "wechat": "微信账单",
                                   "alipay": "支付宝账单"}[source],
                        "dry_run": 1 if dry else 0, "would_add": added, "dup": dup,
                        "skipped": skipped, "failed": failed, "account": acc_def,
                        "samples": samples, "errors": errors,
                        "note": ("预览，没写库；确认无误后把 dry_run 设为 0 再导一次" if dry
                                 else "已写入 %d 笔" % added)})

    ctx["day_ledger"] = day_ledger
    ctx["month_ledger"] = month_ledger
    ctx["init_ledger_db"] = init_ledger_db
    ctx["fmt_cents"] = fmt
    return ctx
