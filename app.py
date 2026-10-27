# -*- coding: utf-8 -*-
"""
日历便签 (calnote) —— 后端主程序
=================================
一个把"日历 + 便签"合二为一的私有 Web 应用：
  - 六种内容：普通日程 / 生日 / 纪念日 / 倒数日 / 笔记 / 待办
  - 所有内容都按日期存储（SQLite）
  - 支持农历、节日显示，CSV 批量导入

运行方式：
  python app.py
环境变量：
  PORT    服务端口，默认 8000
  DB_PATH 数据库文件路径，默认 ./data/calnote.db
"""

import csv
import shutil
import io
import json
import os
import re
import base64
import sqlite3
import hashlib
import hmac
import html as _html
import secrets
import threading
import time
import uuid
from datetime import date, datetime, timedelta

import ledger
import vault
from flask import Flask, g, has_request_context, jsonify, redirect, request, send_file, send_from_directory, url_for
from lunar_python import Lunar, LunarMonth, Solar

# ------------------------- 基础配置 -------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("DB_PATH", os.path.join(BASE_DIR, "data", "calnote.db"))
PORT = int(os.environ.get("PORT", "8000"))
# 附件存放目录（和数据库放一起，都在数据卷里）
ATTACH_DIR = os.path.join(os.path.dirname(DB_PATH), "attachments")

DATA_DIR = os.path.dirname(DB_PATH)
THUMB_DIR = os.path.join(DATA_DIR, "thumbs")            # NAS 引用的缩略图缓存（可随时整目录删除重建）
THUMB_MAX_FILES = int(os.environ.get("THUMB_MAX_FILES", "8000"))
THUMB_MAX_MB = int(os.environ.get("THUMB_MAX_MB", "500"))
NAS_MAX_FILES = int(os.environ.get("NAS_MAX_FILES", "24"))   # 一屏先来一小批，往下滑自动续加载
NAS_PREVIEW_MAX_MB = int(os.environ.get("NAS_PREVIEW_MAX_MB", "0"))   # 0=不限制预览体积

# 允许被引用/浏览的 NAS 目录（只读）。改这里或用环境变量 NAS_ROOTS 覆盖：
#   NAS_ROOTS="资料:/vol3/1000/01资料,相片:/vol3/1000/相片,制造分厂:/vol4/@team/制造分厂"
DEFAULT_NAS_ROOTS = "资料:/vol3/1000/01资料,相片:/vol3/1000/相片,制造分厂:/vol4/@team/制造分厂"


def _parse_roots(raw=None):
    out = []
    seen = set()
    for part in (raw or os.environ.get("NAS_ROOTS", DEFAULT_NAS_ROOTS)).split(","):
        part = part.strip()
        if not part or ":" not in part:
            continue
        alias, path = part.split(":", 1)
        alias, path = alias.strip().strip("/"), path.strip()
        if not alias or not path:
            continue
        real = os.path.realpath(path)
        if alias in seen or os.sep.join(real.split(os.sep)[:2]) in ("", os.sep):
            continue
        seen.add(alias)
        out.append({"alias": alias, "path": path, "real": real})
    return out


NAS_ROOTS = _parse_roots()

# 六种内容类型（中文名 ↔ 英文标识）
TYPE_NAMES = {
    "schedule": "普通日程",
    "birthday": "生日",
    "anniversary": "纪念日",
    "countdown": "倒数日",
    "note": "笔记",
    "todo": "待办",
}
# 反过来：中文 → 英文（新建和 CSV 导入时用）
TYPE_ALIAS = {
    "普通日程": "schedule", "日程": "schedule", "schedule": "schedule",
    "生日": "birthday", "birthday": "birthday",
    "纪念日": "anniversary", "anniversary": "anniversary",
    "倒数日": "countdown", "countdown": "countdown",
    "笔记": "note", "note": "note",
    "待办": "todo", "todo": "todo",
}

csv.field_size_limit(10 * 1024 * 1024)   # 笔记正文可能很长

app = Flask(__name__, static_folder="static")
# 单次请求体上限（附件 20MB + 余量）。不设的话，超大文件会先整份吃进临时区。
MAX_BODY_MB = int(os.environ.get("MAX_BODY_MB", "26"))
app.config["MAX_CONTENT_LENGTH"] = MAX_BODY_MB * 1048576
# ============================ 账号、登录与数据可见性（v9 多用户） ============================
# 权限只有一条主规则，不扩散：
#   可见 = 本人的一切条目  +  他人的「非笔记且未标仅自己可见」条目
#   可写 = 同上（日程/生日/纪念日/待办/倒数日全家都能互相帮忙填，笔记严格私有）
# 笔记对家人完全不可见，管理员也读不到别人的笔记明文；密码本（v9 第 3 期）沿用这条。
#
# 默认关闭：users 表为空时行为和 v8.3 之前一样（无口令直用）。users 表非空即启用登录，
# 首次启动会把旧的单口令 AUTH_USER/AUTH_PASS 自动转成管理员账号，不会把自己锁在门外。
AUTH_USER = os.environ.get("AUTH_USER", "").strip()
AUTH_PASS = os.environ.get("AUTH_PASS", "").strip()
AUTH_MEMBERS = os.environ.get("AUTH_MEMBERS", "").strip()              # 逗号分隔的成员账号名
AUTH_MEMBER_PASS = os.environ.get("AUTH_MEMBER_PASS", "").strip()      # 成员初始口令（要求登录后改掉）
AUTH_TTL_DAYS = max(1, int(os.environ.get("AUTH_TTL_DAYS", "30")))
AUTH_COOKIE = "***"
AUTH_COOKIE_SECURE = os.environ.get("AUTH_COOKIE_SECURE", "") in ("1", "true", "yes")
# 全员强制下线开关：改这个值并重建容器，所有人的登录态立刻失效（泄露口令时用）
AUTH_EPOCH = os.environ.get("AUTH_EPOCH", "0").strip()
PBKDF2_ITER = int(os.environ.get("PBKDF2_ITER", "120000"))
ROLE_ADMIN, ROLE_MEMBER = "admin", "member"

# 会话签名密钥独立存文件，和登录口令解耦：给家人改口令不会把全家一起踢下线。
SECRET_FILES = [os.path.join(DATA_DIR, "auth_secret.key"),
                os.path.join(BASE_DIR, "data", "auth_secret.key")]
_auth_secret_cache = None
_auth_enabled = None            # users 表是否有可用账号 → 决定要不要登录


def _const_eq(a, b):
    """定长比较，避免逐字符短路的计时侧信道；compare_digest 不收非 ASCII，先 encode。"""
    return hmac.compare_digest(str(a or "").encode("utf-8"), str(b or "").encode("utf-8"))


def auth_on():
    if _auth_enabled is None:
        return bool(AUTH_USER and AUTH_PASS)
    return bool(_auth_enabled)


def _auth_secret():
    global _auth_secret_cache
    if _auth_secret_cache is None:
        val = b""
        for p in SECRET_FILES:
            try:
                with open(p, "rb") as f:
                    val = f.read().strip()
                if val:
                    break
            except OSError:
                continue
        if not val:
            val = secrets.token_hex(32).encode()
            for p in SECRET_FILES:
                try:
                    os.makedirs(os.path.dirname(p), exist_ok=True)
                    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    with os.fdopen(fd, "wb") as f:
                        f.write(val)
                    break
                except OSError:
                    continue        # 只读挂载时退回内存密钥（重启后需重新登录）
        _auth_secret_cache = val
    return _auth_secret_cache


def hash_password(plain):
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", str(plain).encode("utf-8"), salt, PBKDF2_ITER)
    return "pbkdf2_sha256$%d$%s$%s" % (PBKDF2_ITER,
                                       base64.b64encode(salt).decode(), base64.b64encode(dk).decode())


def verify_password(plain, stored):
    try:
        algo, iters, salt_b64, hash_b64 = str(stored).split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        got = hashlib.pbkdf2_hmac("sha256", str(plain).encode("utf-8"),
                                  base64.b64decode(salt_b64), int(iters))
        return hmac.compare_digest(got, base64.b64decode(hash_b64))
    except (ValueError, TypeError):
        return False


def init_users(conn):
    """建号与升级迁移：老库首次进 v9 时把单口令转成管理员账号，并按 AUTH_MEMBERS 补成员。"""
    global _auth_enabled
    conn.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username    TEXT NOT NULL UNIQUE,
        display_name TEXT DEFAULT '',
        role        TEXT DEFAULT 'member',
        pass_hash   TEXT DEFAULT '',
        session_epoch INTEGER DEFAULT 0,
        starter     INTEGER DEFAULT 0,
        active      INTEGER DEFAULT 1,
        created_at  TEXT DEFAULT (datetime('now','localtime')))""")
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    # 只有把 AUTH_USER 和 AUTH_PASS 都配齐了才建号。不配就一个账号都不建 ——
    # 否则随机口令只出现在容器日志里，人会被自己锁在门外。
    if n == 0 and AUTH_USER and AUTH_PASS:
        conn.execute("INSERT INTO users (username,display_name,role,pass_hash,starter) VALUES (?,?,?,?,0)",
                     (AUTH_USER, "我", ROLE_ADMIN, hash_password(AUTH_PASS)))
        for name in [x.strip() for x in AUTH_MEMBERS.split(",") if x.strip()]:
            if not AUTH_MEMBER_PASS:
                continue
            conn.execute("INSERT OR IGNORE INTO users (username,display_name,role,pass_hash,starter) VALUES (?,?,?,?,1)",
                         (name, name, ROLE_MEMBER, hash_password(AUTH_MEMBER_PASS)))
        conn.commit()
    cnt = conn.execute("SELECT COUNT(*) FROM users WHERE active=1").fetchone()[0]
    _auth_enabled = cnt > 0
    return cnt


def _gen_pass():
    alph = "abcdefghjkmnpqrstuvwxyz23456789"
    return "".join(secrets.choice(alph) for _ in range(12))


def _sig(body):
    return hmac.new(_auth_secret(), ("%s|%s" % (AUTH_EPOCH, body)).encode(), hashlib.sha256).hexdigest()


def _make_token(uid, epoch):
    exp = int(time.time()) + AUTH_TTL_DAYS * 86400
    body = "%d.%d.%d" % (int(uid), int(epoch), exp)
    return "%s.%s" % (body, _sig(body))


def _token_uid(tok):
    try:
        uid_s, ep_s, exp_s, sig = str(tok).split(".")
        uid, ep, exp = int(uid_s), int(ep_s), int(exp_s)
    except (TypeError, ValueError):
        return None
    if exp < time.time():
        return None
    if not hmac.compare_digest(sig, _sig("%d.%d.%d" % (uid, ep, exp))):
        return None
    u = _user_by_id(uid)
    if not u or not u["active"]:
        return None
    return u if int(u["session_epoch"]) == ep else None


def _user_by_id(uid):
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    except sqlite3.Error:
        r = None
    conn.close()
    return dict(r) if r else None


def _user_by_name(name):
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM users WHERE username=?", (str(name or "").strip(),)).fetchone()
    except sqlite3.Error:
        r = None
    conn.close()
    return dict(r) if r else None


def current_uid():
    u = getattr(g, "user", None)
    return int(u["id"]) if u else 1          # 未启用账号体系时数据都归 1 号（老库唯一主人）


def is_admin():
    u = getattr(g, "user", None)
    return bool(u) and u["role"] == ROLE_ADMIN


VIS_WHERE = "(owner_id={uid} OR (COALESCE(private,0)=0 AND type<>'note'))"


def apply_scope(conn):
    """把「读」收进一个临时视图：main.items 之上按当前用户过滤。

    项目里 30 多处 items 查询（列表、搜索、月份、当天、提醒、统计、导出、判重）都写死了
    FROM items，逐个补 WHERE 条件的做法一定会漏，漏一处就是越权读别人的笔记。改成 temp 视图
    遮蔽表名后，SQLite 解析时 temp 优先于 main，所有既有 SELECT 自动带上可见性；写操作走
    main.items 并各自显式校验，读被兜住、写可审计。
    """
    if not has_request_context():
        return        # 启动迁移、后台线程没有请求上下文，这时不建视图
    try:
        # 没开登录（单用户）时也要过滤垃圾桶，所以可见性条件按情况退化，桶过滤永远生效
        vis = ("1=1" if not auth_on() else VIS_WHERE.format(uid=int(current_uid())))
        conn.execute("DROP VIEW IF EXISTS temp.items")
        conn.execute("CREATE TEMP VIEW temp.items AS SELECT * FROM main.items WHERE ("
                     + vis + ") AND COALESCE(deleted_at,'')=''")   # 桶里的内容不进任何列表/统计/判重
        conn.execute("DROP VIEW IF EXISTS temp.attachments")
        conn.execute("CREATE TEMP VIEW temp.attachments AS SELECT * FROM main.attachments "
                     "WHERE COALESCE(deleted_at,'')=''")       # 移出的附件同理，界面上一律看不见
    except sqlite3.Error:
        pass


def _writable(conn, item_id):
    """写权限：条目必须在当前用户可见范围内（笔记只有本人）。"""
    row = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
    return row is not None


# ---------------- 登录 / 退出 / 会话 ----------------
_STATIC_EXT = (".js", ".css", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".ico",
               ".webmanifest", ".webp", ".txt", ".map")


def _is_public(path):
    if path in ("/login", "/logout", "/favicon.ico") or path == "/api/health":
        return True                                   # 探活不能要求登录，否则容器被判不健康
    if path.startswith("/static/"):
        return True
    return path.endswith(_STATIC_EXT)                 # 图标/脚本/样式本身不含数据


def _safe_next(target):
    """只允许站内相对路径，挡掉「双斜杠 + 外部域名」这类跳出本站的跳转。"""
    t = str(target or "")
    return t if t.startswith("/") and not t.startswith("//") else "/"


LOGIN_HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="theme-color" content="#2b6cb0">
<title>日历便签 · 登录</title>
<style>
 body{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,"PingFang SC","Microsoft YaHei",sans-serif;
      background:#f4f6f8;color:#222;display:flex;min-height:100vh;align-items:center;justify-content:center}
 .box{width:100%;max-width:340px;background:#fff;padding:26px 22px;border-radius:14px;
      box-shadow:0 2px 14px rgba(0,0,0,.08);margin:20px}
 h1{font-size:19px;margin:0 0 4px}.sub{color:#777;font-size:13px;margin:0 0 18px}
 label{display:block;font-size:13px;color:#555;margin:12px 0 4px}
 input{width:100%;box-sizing:border-box;padding:11px 12px;font-size:16px;border:1px solid #d5dbe1;
       border-radius:8px;background:#fbfcfd}
 input:focus{outline:none;border-color:#2b6cb0}
 button{width:100%;margin-top:20px;padding:12px;font-size:16px;font-weight:600;color:#fff;
        background:#2b6cb0;border:0;border-radius:8px}
 button:active{opacity:.85}
 .err{color:#c53030;font-size:14px;margin:14px 0 -6px}
</style></head><body><form class="box" method="post" action="/login?next=__NEXT__">
<h1>日历便签</h1><p class="sub">家庭私有应用，请用成员账号登录</p>__ERR__
<label for="u">账号</label><input id="u" name="user" autocomplete="username" autofocus>
<label for="p">口令</label><input id="p" name="pass" type="password" autocomplete="current-password">
<button type="submit">登录</button></form></body></html>"""


@app.route("/login", methods=["GET", "POST"])
def login_page():
    if not auth_on():
        return redirect("/")
    nxt = _safe_next(request.args.get("next"))
    if request.method == "POST":
        u = _user_by_name(request.form.get("user"))
        p = str(request.form.get("pass") or "")
        if not u or not u["active"] or not verify_password(p, u["pass_hash"]):
            return _login_html(nxt, "账号或口令不对"), 401
        resp = redirect(nxt)
        _set_login_cookie(resp, u)
        return resp
    return _login_html(nxt, "")


def _set_login_cookie(resp, u):
    resp.set_cookie(AUTH_COOKIE, _make_token(u["id"], u["session_epoch"]),
                    max_age=AUTH_TTL_DAYS * 86400, httponly=True,
                    samesite="Lax", secure=AUTH_COOKIE_SECURE)
    return resp


def _login_html(nxt, msg):
    err = '<p class="err">%s</p>' % _html.escape(msg) if msg else ""
    # 模板里有 CSS 的 width:100% 之类的百分号，不能用 % 格式化，直接替换占位符
    return (LOGIN_HTML.replace("__NEXT__", _html.escape(nxt, quote=True)).replace("__ERR__", err))


@app.route("/logout")
def logout_page():
    resp = redirect("/login" if auth_on() else "/")
    resp.delete_cookie(AUTH_COOKIE)
    return resp


def _user_from_basic():
    """兼容 curl -u 账号:口令（脚本、反代、调试用），校验走 users 表哈希。"""
    auth = request.authorization
    if not auth:
        return None
    u = _user_by_name(auth.username)
    if u and u["active"] and verify_password(auth.password or "", u["pass_hash"]):
        return u
    return None


@app.before_request
def require_login():
    if not auth_on():
        g.user = None
        return None
    if _is_public(request.path):
        g.user = None
        return None
    tok = request.cookies.get(AUTH_COOKIE)
    u = _token_uid(tok) if tok else None
    if u is None:
        u = _user_from_basic()
    if u is None:
        accept = str(request.headers.get("Accept") or "")
        if request.path.startswith("/api/") or (accept and "html" not in accept and "*/*" not in accept):
            return jsonify({"error": "没有登录", "login": "/login"}), 401
        q = request.query_string.decode("utf-8", "replace")
        return redirect(url_for("login_page", next=(request.path + ("?" + q if q else ""))))
    g.user = u
    return None


@app.after_request
def _security_headers(resp):
    p = request.path
    if p.startswith("/api/") and getattr(g, "user", None):
        resp.headers["X-Frame-Options"] = "SAMEORIGIN"
    return resp


# ---------------- 成员管理（管理员） ----------------
def _pub(u):
    return {"id": u["id"], "username": u["username"], "display_name": u["display_name"] or u["username"],
            "role": u["role"], "active": int(u["active"] or 0), "starter": int(u["starter"] or 0),
            "items": 0}


@app.route("/api/me")
def api_me():
    u = getattr(g, "user", None)
    if not u:
        return jsonify({"ok": 1, "user": {"id": 1, "username": AUTH_USER or "calnote",
                                          "display_name": "我", "role": ROLE_ADMIN, "single": 1}})
    d = _pub(u)
    conn = get_db()
    try:
        d["items"] = conn.execute("SELECT COUNT(*) c FROM items WHERE owner_id=?", (u["id"],)).fetchone()["c"]
    except sqlite3.Error:
        pass
    conn.close()
    return jsonify({"ok": 1, "user": d})


@app.route("/api/members")
def api_members():
    u = getattr(g, "user", None)
    if not u:
        return jsonify({"members": [], "single": 1})
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    except sqlite3.Error:
        rows = []
    if u["role"] != ROLE_ADMIN:
        rows = [r for r in rows if r["id"] == u["id"]]
    out = []
    for r in rows:
        d = _pub(r)
        if u["role"] == ROLE_ADMIN:
            try:
                d["items"] = conn.execute("SELECT COUNT(*) c FROM items WHERE owner_id=?", (r["id"],)).fetchone()["c"]
            except sqlite3.Error:
                pass
        out.append(d)
    conn.close()
    return jsonify({"members": out, "me": u["id"], "admin": u["role"] == ROLE_ADMIN})


@app.route("/api/members", methods=["POST"])
def api_add_member():
    if not is_admin():
        return jsonify({"error": "只有管理员能建账号"}), 403
    data = request.get_json(silent=True) or {}
    name = str(data.get("username", "")).strip().lower()
    if not re.fullmatch(r"[a-z0-9_\-\u4e00-\u9fa5]{2,20}", name):
        return jsonify({"error": "账号名用 2-20 个字母数字下划线或中文"}), 400
    pwd = str(data.get("password") or "").strip()
    if len(pwd) < 6:
        return jsonify({"error": "口令至少 6 位"}), 400
    role = ROLE_ADMIN if str(data.get("role")) == ROLE_ADMIN else ROLE_MEMBER
    conn = get_db()
    if conn.execute("SELECT id FROM users WHERE username=?", (name,)).fetchone():
        conn.close()
        return jsonify({"error": "已经有这个账号了"}), 400
    cur = conn.execute("INSERT INTO users (username,display_name,role,pass_hash,starter) VALUES (?,?,?,?,0)",
                       (name, str(data.get("display_name", "")).strip()[:20] or name, role, hash_password(pwd)))
    conn.commit()
    r = conn.execute("SELECT * FROM users WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify({"member": _pub(dict(r))}), 201


@app.route("/api/members/<int:uid>", methods=["PUT"])
def api_update_member(uid):
    me = getattr(g, "user", None)
    if not me:
        return jsonify({"error": "没有登录"}), 401
    data = request.get_json(silent=True) or {}
    if me["id"] != uid and me["role"] != ROLE_ADMIN:
        return jsonify({"error": "只能改自己的账号"}), 403
    conn = get_db()
    r = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if r is None:
        conn.close()
        return jsonify({"error": "账号不存在"}), 404
    if "display_name" in data:
        conn.execute("UPDATE users SET display_name=? WHERE id=?",
                     (str(data.get("display_name") or "").strip()[:20] or r["username"], uid))
    if "active" in data and me["role"] == ROLE_ADMIN:
        on = 1 if data.get("active") else 0
        if not on and r["role"] == ROLE_ADMIN:
            adm = conn.execute("SELECT COUNT(*) c FROM users WHERE role='admin' AND active=1").fetchone()["c"]
            if adm <= 1 and r["active"]:
                conn.close()
                return jsonify({"error": "至少要留一个可用的管理员"}), 400
        conn.execute("UPDATE users SET active=?, session_epoch=session_epoch+1 WHERE id=?", (on, uid))
    conn.commit()
    conn.close()
    return jsonify({"ok": 1})


@app.route("/api/members/<int:uid>/password", methods=["POST"])
def api_member_password(uid):
    """本人改口令要验旧口令（并使自己的登录态失效重登）；管理员重置成员口令不需要旧的。"""
    me = getattr(g, "user", None)
    if not me:
        return jsonify({"error": "没有登录"}), 401
    data = request.get_json(silent=True) or {}
    new = str(data.get("password") or "")
    if len(new) < 6:
        return jsonify({"error": "新口令至少 6 位"}), 400
    conn = get_db()
    r = conn.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
    if r is None:
        conn.close()
        return jsonify({"error": "账号不存在"}), 404
    if me["id"] == uid:
        if not verify_password(str(data.get("old") or ""), r["pass_hash"]):
            conn.close()
            return jsonify({"error": "当前口令不对"}), 403
    elif me["role"] != ROLE_ADMIN:
        conn.close()
        return jsonify({"error": "只能改自己的口令"}), 403
    conn.execute("UPDATE users SET pass_hash=?, starter=0, session_epoch=session_epoch+1 WHERE id=?",
                 (hash_password(new), uid))
    conn.commit()
    conn.close()
    resp = jsonify({"ok": 1, "relogin": me["id"] == uid})
    if me["id"] == uid:
        resp.delete_cookie(AUTH_COOKIE)          # 改完自己的口令重新登录，符合「只踢自己」
    return resp


@app.errorhandler(Exception)
def _unhandled(e):
    """接口出错也要回 JSON：前端 api() 拿 HTML 会解析失败，用户只看到白屏。"""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if e.code == 404 and request.path.startswith("/api/"):
            return jsonify({"error": "接口不存在"}), 404
        return e
    app.logger.exception("接口异常 %s %s", request.method, request.path)
    return jsonify({"error": "服务端出错了，把这条时间告诉我：%s" % datetime.now().strftime("%H:%M:%S")}), 500


@app.errorhandler(413)
def _too_big(e):
    return jsonify({"error": "太大，超了单次请求 %dMB 上限" % MAX_BODY_MB}), 413


@app.after_request
def _cache_headers(resp):
    """静态资源走协商缓存：改动后立即生效，没变就 304 不重传。
    附件图片另有 immutable 长缓存（随机文件名，内容不会变）。"""
    p = request.path
    if p in ("/login", "/logout"):
        resp.headers["Cache-Control"] = "no-store"     # 登录页不缓存，避免带着旧跳转
        return resp
    if (p in ("/", "/index.html") or p.endswith((".js", ".css", ".html", ".webmanifest"))
            or p.startswith("/static/")):
        if resp.status_code == 200 and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-cache"
    return resp


# 记账（v9 第 2 期）与密码本（第 3 期）的注册上下文：路由写在各自模块里，
# 连接、当前用户、权限判断由这里注入，避免模块和 app.py 互相 import
LEDGER = {}
VAULT = {}
_CTX = {}

# ------------------------- 数据库 -------------------------

# 连接层参数：NAS 机械盘 / FUSE 挂载上 SQLite 容易被短时间锁住，这里统一开忙等 +
# 省 fsync 的日志模式。如果 data 目录挂在 NFS/SMB 这类共享文件系统上，WAL 不安全，
# 用 DB_WAL=0 关掉即可（其余优化不受影响）。
DB_BUSY_MS = int(os.environ.get("DB_BUSY_MS", "10000"))
DB_TIMEOUT = float(os.environ.get("DB_TIMEOUT", "15"))
DB_WAL = os.environ.get("DB_WAL", "1") != "0"


def get_db():
    """连接 SQLite 数据库（连接级 PRAGMA：忙等 / synchronous / 页缓存）。"""
    conn = sqlite3.connect(DB_PATH, timeout=DB_TIMEOUT)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA busy_timeout=%d" % DB_BUSY_MS)
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA temp_store=MEMORY")
        conn.execute("PRAGMA cache_size=-4000")
    except sqlite3.Error:
        pass
    apply_scope(conn)      # 读自动落到当前用户的可见范围，不必逐条 SQL 补 WHERE
    return conn


def setup_journal(conn):
    """journal_mode=WAL 是持久属性，只在启动时设一次；失败就退回默认模式。"""
    if not DB_WAL:
        return ""
    try:
        mode = str(conn.execute("PRAGMA journal_mode=WAL").fetchone()[0] or "").lower()
        return mode if mode == "wal" else ""
    except sqlite3.Error:
        return ""


def init_db():
    """初始化数据库表结构（首次运行自动创建）"""
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = get_db()
    setup_journal(conn)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS items (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        type           TEXT NOT NULL,               -- 类型：schedule/birthday/anniversary/countdown/note/todo
        title          TEXT NOT NULL,               -- 标题
        content        TEXT DEFAULT '',             -- 内容/备注
        date           TEXT NOT NULL,               -- 日期 YYYY-MM-DD（所有内容按此时间线存储）
        time           TEXT DEFAULT '',             -- 开始时间 HH:MM（日程用）
        end_time       TEXT DEFAULT '',             -- 结束时间 HH:MM（日程用，可选）
        all_day        INTEGER DEFAULT 0,           -- 是否全天（1=全天日程，不显示时间）
        location       TEXT DEFAULT '',             -- 地点（日程用）
        remind_minutes INTEGER DEFAULT 0,           -- 提前多少分钟提醒（0=不提醒）
        is_lunar       INTEGER DEFAULT 0,           -- 生日/纪念日是否按农历（1=农历）
        yearly         INTEGER DEFAULT 0,           -- 是否每年重复（生日/纪念日用）
        done           INTEGER DEFAULT 0,           -- 待办是否完成（1=完成）
        tags           TEXT DEFAULT '',             -- 标签，用逗号分隔
        weather        TEXT DEFAULT '',             -- 天气（日程用，如：兴安县 多云 11°/6°）
        todo_items     TEXT DEFAULT '',             -- 待办子项清单（JSON 数组：[{"text":"..","done":0}]）
        created_at     TEXT DEFAULT (datetime('now','localtime')),
        updated_at     TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_items_date ON items(date);
    CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
    CREATE INDEX IF NOT EXISTS idx_items_dup ON items(date, type);
    CREATE TABLE IF NOT EXISTS attachments (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id     INTEGER NOT NULL,               -- 属于哪条内容
        filename    TEXT NOT NULL,                  -- 原始文件名
        stored_name TEXT NOT NULL,                  -- 磁盘上的文件名（随机，防冲突）
        mime        TEXT DEFAULT '',                -- 文件类型
        size        INTEGER DEFAULT 0,              -- 文件大小（字节）
        created_at  TEXT DEFAULT (datetime('now','localtime'))
    );
    CREATE INDEX IF NOT EXISTS idx_attachments_item ON attachments(item_id);
    """)
    # 旧数据库升级：补上新增的列（已存在则跳过）
    cols = [r[1] for r in conn.execute("PRAGMA table_info(items)").fetchall()]
    if "end_time" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN end_time TEXT DEFAULT ''")
    if "all_day" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN all_day INTEGER DEFAULT 0")
    if "location" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN location TEXT DEFAULT ''")
    conn.execute("CREATE TABLE IF NOT EXISTS ui_state (key TEXT PRIMARY KEY, val TEXT DEFAULT '')")

    if "weather" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN weather TEXT DEFAULT ''")
    if "todo_items" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN todo_items TEXT DEFAULT ''")
    if "folder_id" not in cols:                      # v5.12：笔记归属文件夹（与类型并存）
        conn.execute("ALTER TABLE items ADD COLUMN folder_id INTEGER DEFAULT 0")
    if "birth_year" not in cols:                     # v5.12：生日/纪念日出生年，用来算年龄
        conn.execute("ALTER TABLE items ADD COLUMN birth_year INTEGER DEFAULT 0")
    if "owner_id" not in cols:                       # v9：内容归属人（老数据全归 1 号主账号）
        conn.execute("ALTER TABLE items ADD COLUMN owner_id INTEGER DEFAULT 1")
    if "private" not in cols:                        # v9：任何类型都可设「仅自己可见」，默认不用
        conn.execute("ALTER TABLE items ADD COLUMN private INTEGER DEFAULT 0")
    # v9.5：删除不再当场真删。deleted_at = 进垃圾桶的时间（空=正常内容）
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE items ADD COLUMN deleted_at TEXT DEFAULT ''")
    _acols = [r[1] for r in conn.execute("PRAGMA table_info(attachments)").fetchall()]
    if "deleted_at" not in _acols:
        conn.execute("ALTER TABLE attachments ADD COLUMN deleted_at TEXT DEFAULT ''")
    for _c, _d in (("kind", "TEXT DEFAULT 'upload'"), ("path", "TEXT DEFAULT ''"),
                   ("sig", "TEXT DEFAULT ''"), ("missing", "INTEGER DEFAULT 0")):
        if _c not in [r[1] for r in conn.execute("PRAGMA table_info(attachments)").fetchall()]:
            conn.execute("ALTER TABLE attachments ADD COLUMN %s %s" % (_c, _d))
    conn.execute("CREATE INDEX IF NOT EXISTS idx_attachments_sig ON attachments(sig)")
    try:      # 桶是按 deleted_at 查的，给两列建个索引，桶里几百上千条也不卡
        conn.execute("CREATE INDEX IF NOT EXISTS idx_items_trash ON items(deleted_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_att_trash ON attachments(deleted_at)")
    except sqlite3.Error:
        pass
    conn.execute("""CREATE TABLE IF NOT EXISTS folders (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        name   TEXT NOT NULL,
        color  TEXT DEFAULT '',
        owner_id INTEGER DEFAULT 1,
        created_at TEXT DEFAULT (datetime('now','localtime')))""")
    if "owner_id" not in [r[1] for r in conn.execute("PRAGMA table_info(folders)").fetchall()]:
        conn.execute("ALTER TABLE folders ADD COLUMN owner_id INTEGER DEFAULT 1")
    # 查询热点上缺的索引：文件夹筛、按类型翻、NAS 引用体检都要用
    init_users(conn)                                 # v9：建号 + 把旧单口令迁成管理员账号
    if LEDGER.get("init_ledger_db"):                 # v9 第 2 期：记账的六张表
        LEDGER["init_ledger_db"](conn)
    if VAULT.get("init_vault_db"):                   # v9 第 3 期：密码本的三张表
        VAULT["init_vault_db"](conn)
    for _sql in (
        "CREATE INDEX IF NOT EXISTS idx_items_folder ON items(folder_id)",
        "CREATE INDEX IF NOT EXISTS idx_items_owner ON items(owner_id)",
        "CREATE INDEX IF NOT EXISTS idx_items_type_date ON items(type, date)",
        "CREATE INDEX IF NOT EXISTS idx_items_todo ON items(type, done, date)",
        "CREATE INDEX IF NOT EXISTS idx_attachments_kind ON attachments(kind, missing)",
    ):
        try:
            conn.execute(_sql)
        except sqlite3.Error:
            pass
    conn.commit()
    conn.close()


# ------------------------- 农历 / 节日 -------------------------

# 历史天气缓存（tools/fetch_weather.py 生成；部署时放 data/ 目录即可）
# 查找顺序：数据卷里的 → 项目 data/ → 镜像里打包的（数据卷挂载会盖掉 /app/data）
WEATHER_FILES = [
    os.path.join(os.path.dirname(DB_PATH), "weather_cache.json"),
    os.path.join(BASE_DIR, "data", "weather_cache.json"),
    os.path.join(BASE_DIR, "weather_cache.json"),
]
# 缺天气时后端可代拉一次（需要 NAS 能上外网），默认城市：兴安县
WEATHER_CITY = os.environ.get("WEATHER_CITY", "兴安县")
# 降水量门槛（毫米）：低于它就不说“雨/雪”，避免“码说有毛毛雨、实际一天没下”
RAIN_MIN_MM = float(os.environ.get("RAIN_MIN_MM", "0.5"))   # 与 README / compose 口径一致
WEATHER_TIMEOUT = float(os.environ.get("WEATHER_TIMEOUT", "5"))
WEATHER_LAT = float(os.environ.get("WEATHER_LAT", "25.6128"))
WEATHER_LON = float(os.environ.get("WEATHER_LON", "110.6720"))
WMO_CN = {
    0: "晴", 1: "晴", 2: "多云", 3: "阴", 45: "雾", 48: "雾凇",
    51: "小雨", 53: "小雨", 55: "中雨", 56: "冻雨", 57: "冻雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "阵雨", 81: "阵雨", 82: "强阵雨", 85: "阵雪", 86: "阵雪",
    95: "雷阵雨", 96: "雷阵雨", 99: "雷阵雨",
}
_weather_cache = None
_weather_miss = set()
_LIVE_DONE = set()        # 今天已经自动更新过的日期（每天一次）
_wx_lock = threading.RLock()      # 缓存同时被请求线程和后台线程读写
_wx_dirty = False
_wx_flushed_at = 0.0
WX_FLUSH_SEC = float(os.environ.get("WX_FLUSH_SEC", "30"))   # 攒够这个时间窗才整体落盘一次
_bg_doing = set()
_bg_lock = threading.Lock()


def run_in_bg(key, fn, *args):
    """把可延后的慢活（联网取天气、缓存落盘）丢后台，别压着用户的那一次请求。
    同一个 key 正在跑就不重复起。"""
    with _bg_lock:
        if key in _bg_doing:
            return False
        _bg_doing.add(key)

    def _runner():
        try:
            fn(*args)
        except Exception:
            pass
        finally:
            with _bg_lock:
                _bg_doing.discard(key)

    threading.Thread(target=_runner, daemon=True).start()
    return True


def weather_cache():
    """惰性加载天气缓存文件：{"2023-01-03": {"city","weather","temph","templ"}}"""
    global _weather_cache
    with _wx_lock:
        if _weather_cache is None:
            _weather_cache = {}
            for p in WEATHER_FILES:
                try:
                    with open(p, encoding="utf-8") as f:
                        data = json.load(f)
                    if isinstance(data, dict):
                        _weather_cache.update(data)
                        break
                except (OSError, ValueError):
                    continue
        return _weather_cache


def cached_weather(day_str):
    w = weather_cache().get(day_str)
    if not w or _stale_wx(w):
        return ""                        # 没校准过的旧数据直接当没有，交给联网重取
    return join_weather(w.get("city") or WEATHER_CITY, w.get("weather"),
                        w.get("temph"), w.get("templ"))


def fetch_weather_online(d):
    """去 Open-Meteo 拿某天天气（近 4 天走预报、更早走 ERA5 存档），带降水量校准。

    偏差的主要来源是「WMO 码一有毛毛雨就算雨」。这里改成：
    · 日累计降水 < RAIN_MIN_MM 毫米 → 绝不写雨/雪，按云量写 晴/多云/阴
    · 确实有降水 → 写明雨量（如「小雨 1.4mm」），方便你核对
    """
    import urllib.request
    import urllib.parse
    day = d.isoformat()
    if d < date(1940, 1, 1) or (d - date.today()).days > 15 or day in _weather_miss:
        return ""
    soon = (date.today() - d).days <= 4
    host = "https://api.open-meteo.com/v1/forecast" if soon else "https://archive-api.open-meteo.com/v1/archive"
    daily = "weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum"
    if soon:
        daily += ",precipitation_probability_max"
    q = {"latitude": WEATHER_LAT, "longitude": WEATHER_LON, "timezone": "Asia/Shanghai", "daily": daily,
         "start_date": day, "end_date": day}
    url = host + "?" + urllib.parse.urlencode(q)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "calnote/1.0"})
        with urllib.request.urlopen(req, timeout=WEATHER_TIMEOUT) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception:
        _weather_miss.add(day)
        return ""
    dd = data.get("daily") or {}
    if not dd.get("time"):
        _weather_miss.add(day)
        return ""

    def first(key):
        v = dd.get(key)
        return v[0] if isinstance(v, list) and v else None

    code = first("weather_code")
    hi, lo = first("temperature_2m_max"), first("temperature_2m_min")
    mm = first("precipitation_sum")
    prob = first("precipitation_probability_max")
    mm = 0.0 if mm is None else float(mm)
    code = -1 if code is None else int(code)
    wet_words = ("雨", "雪", "霰", "冰粒")
    desc = WMO_CN.get(code, "")
    precip_is_wet = any(w in desc for w in wet_words)
    hitmp = "" if hi is None else str(int(round(float(hi))))
    lotmp = "" if lo is None else str(int(round(float(lo))))

    if precip_is_wet and (mm < RAIN_MIN_MM or (prob is not None and float(prob) < 25 and mm < 0.5)):
        # 码说有降水、实际几乎没下：按云量重写，宁可不写雨
        desc = {0: "晴", 1: "晴", 2: "多云", 3: "阴"}.get(code if code in (0, 1, 2, 3) else 3, "阴"
                                                          if code >= 45 else "多云")
        extra = ""
    elif precip_is_wet and mm > 0:
        extra = " %.1fmm" % mm
    else:
        extra = ""
    text = (desc + extra).strip()
    w = {"city": WEATHER_CITY, "weather": text, "temph": hitmp, "templ": lotmp,
         "src": "open-meteo-%s" % ("forecast" if soon else "era5"), "mm": round(mm, 1)}
    cache_weather(day, w)
    return join_weather(WEATHER_CITY, text, hitmp, lotmp)


def cache_weather(day, w):
    """写进内存缓存并标记待落盘（脏标记必须在锁内改，否则并工会丢更新）。"""
    global _wx_dirty
    with _wx_lock:
        weather_cache()[day] = w
        _wx_dirty = True
    mark_weather_dirty()


def mark_weather_dirty():
    """标记缓存有改动，并安排一次延迟落盘（几千天的缓存没必要每取一天就整写一遍）。"""
    run_in_bg("wx-flush", _delayed_weather_flush, WX_FLUSH_SEC)


def _delayed_weather_flush(sec):
    time.sleep(sec)
    _write_weather_cache(force=True)


def _write_weather_cache(force=False):
    """整体落盘：临时文件 + 原子替换，写一半崩溃也不会把档案文件搞坏。"""
    global _wx_dirty, _wx_flushed_at
    with _wx_lock:
        if not _wx_dirty or (not force and time.monotonic() - _wx_flushed_at < WX_FLUSH_SEC):
            return
        snapshot = dict(_weather_cache or {})
        _wx_dirty = False
        _wx_flushed_at = time.monotonic()
    try:
        path = WEATHER_FILES[0]
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, path)
    except OSError:
        with _wx_lock:
            _wx_dirty = True


def _stale_wx(entry):
    """旧缓存（wttr.in 那批、没有 src 标记的）不算可信来源"""
    return isinstance(entry, dict) and not entry.get("src")


def refresh_today_weather(day, uid):
    """后台任务：取当天实测天气，和库里文本不一致才改写（自己开连接）。

    后台线程没有请求上下文，g 用不了，所以可见范围按传进来的 uid 显式限定，
    并且只动 main.items 里属于本人或共享的条目，不会越界改写别人的私有笔记。"""
    d = parse_date(day)
    if d is None:
        return
    live_w = fetch_weather_online(d)
    if not live_w:
        return
    tail = live_w.split(" ", 1)[-1]
    scope = VIS.format(u=int(uid))
    conn = get_db()
    try:
        rows = conn.execute("SELECT weather FROM main.items"
                            " WHERE date=? AND weather<>''" + scope, (day,)).fetchall()
        if any((r["weather"] or "").split(" ", 1)[-1] != tail for r in rows):
            conn.execute("UPDATE main.items SET weather=? WHERE date=? AND weather<>''" + scope,
                         (live_w, day))
            conn.commit()
    except sqlite3.Error:
        pass
    finally:
        conn.close()


_LUNAR_DAY_MAX = int(os.environ.get("CAL_CACHE_DAYS", "1500"))
_lunar_day_cache = {}


def lunar_info(d):
    """某天农历信息（按日缓存：翻日历来回切月时不必重算）"""
    key = d.isoformat()
    v = _lunar_day_cache.get(key)
    if v is None:
        v = _lunar_info_raw(d)
        if len(_lunar_day_cache) > _LUNAR_DAY_MAX:
            _lunar_day_cache.clear()
        _lunar_day_cache[key] = v
    return v


def _lunar_info_raw(d):
    """返回某天的农历信息字典（农历日期、节日、节气、星期）"""
    y, m, day = d.year, d.month, d.day
    solar = Solar.fromYmd(y, m, day)
    lunar = solar.getLunar()
    festivals = list(solar.getFestivals()) + list(lunar.getFestivals())
    jieqi = lunar.getJieQi()
    return {
        "lunar": lunar.toString(),           # 例：二〇二六年九月初八
        "monthCn": lunar.getMonthInChinese(),# 例：九月
        "dayCn": lunar.getDayInChinese(),    # 例：初八
        "festivals": festivals,              # 公历+农历节日列表
        "jieqi": "" if jieqi is None else jieqi,  # 节气，例：寒露
        "weekCn": solar.getWeekInChinese(),  # 例：星期六
        "isWeekend": day in (5, 6),          # 是否周末
    }


_month_cal_cache = {}
_MONTH_CAL_MAX = int(os.environ.get("CAL_CACHE_MONTHS", "36"))


def month_calendar_info(year, month):
    """返回整个月每天的农历信息（整月结果按「年-月」缓存，格子来回翻不重算）"""
    key = (int(year), int(month))
    hit = _month_cal_cache.get(key)
    if hit is not None:
        return hit
    import calendar
    n = calendar.monthrange(year, month)[1]
    result = {}
    for day in range(1, n + 1):
        d = date(year, month, day)
        result[d.isoformat()] = lunar_info(d)
    if len(_month_cal_cache) > _MONTH_CAL_MAX:
        _month_cal_cache.clear()
    _month_cal_cache[key] = result
    return result


_almanac_cache = {}


def almanac_info(d):
    """某天的黄历（宜/忌/干支/冲煞/九星值日/彭祖百忌），按日缓存"""
    key = d.isoformat()
    hit = _almanac_cache.get(key)
    if hit is not None:
        return hit
    hit = _almanac_raw(d)
    if len(_almanac_cache) > _LUNAR_DAY_MAX:
        _almanac_cache.clear()
    _almanac_cache[key] = hit
    return hit


def _almanac_raw(d):
    lunar = Solar.fromYmd(d.year, d.month, d.day).getLunar()
    solar = Solar.fromYmd(d.year, d.month, d.day)
    yi = lunar.getDayYi() or []
    ji = lunar.getDayJi() or []
    return {
        "lunarFull": lunar.toString(),                 # 二〇二六年九月初八
        "lunarMonthDay": f"{lunar.getMonthInChinese()}月{lunar.getDayInChinese()}",
        "yi": yi[:6], "yiMore": max(0, len(yi) - 6),
        "ji": ji[:6], "jiMore": max(0, len(ji) - 6),
        "ganzhiYear": lunar.getYearInGanZhiByLiChun(),
        "ganzhiMonth": lunar.getMonthInGanZhiExact(),
        "ganzhiDay": lunar.getDayInGanZhiExact(),
        "zhiXing": lunar.getZhiXing(),                 # 值日九星，例：氐
        "chong": lunar.getDayChongDesc(),              # 例：(戊午)马
        "sha": lunar.getDaySha() or "",                # 例：南
        "shengXiao": lunar.getYearShengXiao(),         # 例：马
        "naYin": lunar.getDayNaYin(),                  # 例：海中金
        "jieqi": "" if lunar.getJieQi() is None else lunar.getJieQi(),
        "festivals": list(solar.getFestivals()) + list(lunar.getFestivals()),
        "week": solar.getWeekInChinese(),
    }


# ------------------------- 工具函数 -------------------------

def parse_date(s):
    """把 YYYY-MM-DD 字符串转成 date，格式不对返回 None"""
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


LUNAR_MONTH_CN = ["正", "二", "三", "四", "五", "六", "七", "八", "九", "十", "冬", "腊"]
LUNAR_DAY_CN = ["初一", "初二", "初三", "初四", "初五", "初六", "初七", "初八", "初九", "初十",
                "十一", "十二", "十三", "十四", "十五", "十六", "十七", "十八", "十九", "二十",
                "廿一", "廿二", "廿三", "廿四", "廿五", "廿六", "廿七", "廿八", "廿九", "三十"]


def lunar_month_cn(m):
    m = abs(int(m or 1))
    return LUNAR_MONTH_CN[m - 1] if 1 <= m <= 12 else str(m)


def lunar_day_cn(dd):
    dd = int(dd or 1)
    return LUNAR_DAY_CN[min(max(dd, 1), 30) - 1]


# ================= NAS 文件引用（v6.0：不上传、只引用） =================
IMG_EXT = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".svg")
DOC_INLINE = (".pdf",) + IMG_EXT + (".txt", ".md", ".csv", ".json", ".log")


def _mime_of(name):
    import mimetypes
    return mimetypes.guess_type(name)[0] or "application/octet-stream"


def _is_img_name(name):
    return str(name or "").lower().endswith(IMG_EXT)


def _roots_ready():
    return [r for r in NAS_ROOTS if os.path.isdir(r["real"])]


def _resolve_ref(stored):
    """把库里存的引用解析成真实绝对路径，并确保落在授权目录内。
    支持三种写法：`别名/相对路径`、`别名`、绝对路径。返回 (abs, err)。"""
    stored = (stored or "").strip()
    if not stored:
        return None, "没有路径"
    roots = _roots_ready()
    if not roots:
        return None, "没有可用的 NAS 目录（检查容器挂载）"
    if stored.startswith("/"):
        cand = os.path.realpath(stored)
    else:
        parts = stored.split("/", 1)
        alias = parts[0]
        hit = [r for r in roots if r["alias"] == alias]
        if not hit:
            return None, "这个来源不在允许列表里"
        cand = os.path.realpath(os.path.join(hit[0]["real"], parts[1] if len(parts) > 1 else ""))
    for r in roots:
        if cand == r["real"] or cand.startswith(r["real"] + os.sep):
            if not os.path.exists(cand):
                return None, "文件已不在原位置"
            return cand, ""
    return None, "越权路径（不在允许列表内）"


def _alias_of(abs_path):
    for r in _roots_ready():
        if abs_path == r["real"] or abs_path.startswith(r["real"] + os.sep):
            rel = os.path.relpath(abs_path, r["real"])
            return r["alias"] if rel == "." else (r["alias"] + "/" + rel.replace(os.sep, "/"))
    return abs_path


def _ref_sig(abs_path):
    st = os.stat(abs_path)
    return "%s|%d|%d" % (abs_path, int(st.st_size), int(st.st_mtime))


def _ref_info(abs_path):
    st = os.stat(abs_path)
    return {"path": _alias_of(abs_path), "size": int(st.st_size), "mtime": int(st.st_mtime),
            "mime": _mime_of(abs_path), "is_image": _is_img_name(abs_path)}


def _thumb_file(abs_path, w):
    key = hashlib.md5(("%s|%d|%d|%s|%d" % (abs_path, os.path.getsize(abs_path),
                                           int(os.path.getmtime(abs_path)), "calnote-thumb", w)).encode()).hexdigest()
    return os.path.join(THUMB_DIR, key + ".jpg")


def _make_thumb(abs_path, w=160):
    """生成/复用缩略图缓存；没装 Pillow 或不是图片就返回 ''（前端退回原图）。"""
    if not _is_img_name(abs_path):
        return ""
    try:
        os.makedirs(THUMB_DIR, exist_ok=True)
    except OSError:
        return ""
    out = _thumb_file(abs_path, w)
    if os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(abs_path):
        return out
    try:
        from PIL import Image
        im = Image.open(abs_path)
        im.draft("RGB", (w * 2, w * 2))
        im = im.convert("RGB") if im.mode not in ("RGB", "L") else im.convert("RGB")
        im.thumbnail((w, w), Image.LANCZOS)
        tmp = out + ".tmp"
        im.save(tmp, "JPEG", quality=82, optimize=True)
        os.replace(tmp, out)
        _thumb_sweep()
        return out
    except Exception:
        return ""


_thumb_lock = threading.Lock()
_thumb_state = {"since": 0, "last": 0.0}
THUMB_SWEEP_SEC = float(os.environ.get("THUMB_SWEEP_SEC", "120"))
THUMB_SWEEP_EVERY = max(24, int(os.environ.get("THUMB_SWEEP_EVERY", str(max(1, THUMB_MAX_FILES // 10)))))


def _thumb_sweep(force=False):
    """缩略图缓存封顶：超数量或超体积就按最旧删，永远可重生，删了不心疼。

    原来是「每生成一张就 listdir 整目录 + 对每个文件 stat 两次」：上限 8000 张时
    一张图要多花约 100ms，照片墙一屏几十张直接拖成几秒。现在攒够一批（或超过
    THUMB_SWEEP_SEC 秒）才真扫一次，淘汰只排一次序、按需删。
    """
    with _thumb_lock:
        _thumb_state["since"] += 1
        now = time.monotonic()
        if not force and (_thumb_state["since"] < THUMB_SWEEP_EVERY
                          and now - _thumb_state["last"] < THUMB_SWEEP_SEC):
            return
        _thumb_state["since"] = 0
        _thumb_state["last"] = now
    entries = []
    try:
        with os.scandir(THUMB_DIR) as it:
            for e in it:
                if not e.name.endswith(".jpg"):
                    continue
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, e.path))
    except OSError:
        return
    need_n = max(0, len(entries) - THUMB_MAX_FILES)
    need_b = max(0, sum(x[1] for x in entries) - THUMB_MAX_MB * 1048576)
    if not need_n and not need_b:
        return
    entries.sort()
    gone_n = gone_b = 0
    for _, sz, path in entries:
        if gone_n >= need_n and gone_b >= need_b:
            break
        gone_n += 1
        try:
            os.remove(path)
            gone_b += sz
        except OSError:
            pass


def _att_dict(a, conn, bundle=None):
    """attachmets 行 → 前端字典（区分上传/引用，带存在性与缩略图地址）"""
    kind = a["kind"] or "upload"
    if kind == "link":
        abs_p, err = _resolve_ref(a["path"])
        exists = bool(abs_p)
        info = _ref_info(abs_p) if exists else {}
        missing = 0 if exists else 1
        if not exists and not a["missing"]:
            try:
                conn.execute("UPDATE main.attachments SET missing=1 WHERE id=?", (a["id"],))
            except sqlite3.Error:
                pass
        refc = 0
        if exists:
            if bundle is not None:
                # 批量模式：整页只查一次各路径的引用数，不再每条附件扫一遍附件表
                refc = max(0, int(bundle.refcounts.get(a["path"], 0)) - 1)
            else:
                try:
                    refc = conn.execute("SELECT COUNT(*) c FROM attachments WHERE path=? AND id<>?",
                                        (a["path"], a["id"])).fetchone()["c"]
                except sqlite3.Error:
                    refc = 0
        return {"id": a["id"], "kind": "link", "filename": a["filename"],
                "mime": a["mime"] or info.get("mime", ""), "size": a["size"] or info.get("size", 0),
                "path": a["path"], "url": "/api/attachments/%d" % a["id"],
                "thumb": ("/api/attachments/%d/thumb" % a["id"]) if (a["mime"] or info.get("mime", "")).startswith("image/") else "",
                "exists": exists, "missing": int(a["missing"] or 0) or missing, "refcount": refc}
    return {"id": a["id"], "kind": "upload", "filename": a["filename"], "mime": a["mime"],
            "size": a["size"], "url": "/api/attachments/%d" % a["id"],
            "thumb": "/api/attachments/%d/thumb" % a["id"], "exists": True, "missing": 0, "refcount": 0}


def item_to_dict(row, conn=None, with_attach=True, bundle=None):
    """数据库一行 → 前端用的字典（补上中文类型名、重复标记、附件列表）

    with_attach=False：只给列表用的轻量装配（提醒条之类不需要附件的场合）。
    bundle：一次请求共享的批量查询结果，用来消灭逐条查询。"""
    d = dict(row)
    d["type_name"] = TYPE_NAMES.get(d["type"], d["type"])
    # 待办子项清单：JSON 字符串 → 列表
    try:
        d["todo_items"] = json.loads(d.get("todo_items") or "[]")
    except (ValueError, TypeError):
        d["todo_items"] = []
    # 每年重复的生日/纪念日，自动算出"今年"的日期；按农历的那个月日要换算
    if d.get("yearly"):
        lunar_flag = 1 if d.get("is_lunar") else 0
        d["this_year_date"] = yearly_this_year(d["date"], lunar_flag)
        if lunar_flag:
            stored = parse_date(d["date"])
            if stored:
                d["lunar_label"] = "农历%s月%s" % (
                    lunar_month_cn(stored.month), lunar_day_cn(stored.day))
    # v5.12：文件夹（与类型并存）+ 生日/纪念日年龄
    d["folder_id"] = int(d.get("folder_id") or 0)
    d["folder_name"] = ""
    d["birth_year"] = int(d.get("birth_year") or 0)
    d["age_text"] = ""
    if d["folder_id"]:
        if bundle is not None:
            d["folder_name"] = bundle.folders.get(d["folder_id"], "")
        elif conn is not None:
            fr = conn.execute("SELECT name FROM folders WHERE id=?", (d["folder_id"],)).fetchone()
            if fr:
                d["folder_name"] = fr["name"]
    if d["type"] in ("birthday", "anniversary"):
        sd = parse_date(d["date"])
        by = d["birth_year"] or (sd.year if sd else 0)
        occ = parse_date(d.get("this_year_date") or d["date"])
        if by and occ:
            age = occ.year - by
            if d.get("yearly"):
                nxt = parse_date(yearly_this_year(d["date"], 1 if d.get("is_lunar") else 0))
                if nxt and nxt > date.today():
                    age -= 1
            if age >= 0:
                d["birth_year"] = by
                d["age_text"] = ("%d岁" % age) if d["type"] == "birthday" else ("第%d年" % (age + 1))
    # 附件列表（上传的 + NAS 引用的）
    d["attachments"] = []
    if with_attach and conn is not None:
        rows_a = bundle.attaches.get(d["id"], []) if bundle is not None else \
            conn.execute("SELECT * FROM attachments WHERE item_id=?", (d["id"],)).fetchall()
        for a in rows_a:
            d["attachments"].append(_att_dict(a, conn, bundle))
    return d


def _chunks(seq, size):
    seq = list(seq)
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


class _Bundle(object):
    """一次请求内共享的批量查询结果（文件夹名 / 各条目的附件 / NAS 引用计数）。"""
    __slots__ = ("folders", "attaches", "refcounts")

    def __init__(self, conn, rows, with_attach=True):
        self.folders, self.attaches, self.refcounts = {}, {}, {}
        ids = [r["id"] for r in rows]
        if not ids:
            return
        try:
            for f in conn.execute("SELECT id, name FROM folders").fetchall():
                self.folders[f["id"]] = f["name"]
        except sqlite3.Error:
            pass
        if not with_attach:
            return
        for chunk in _chunks(ids, 400):
            ph = ",".join("?" * len(chunk))
            try:
                for a in conn.execute("SELECT * FROM attachments WHERE item_id IN (%s) ORDER BY id" % ph, chunk):
                    self.attaches.setdefault(a["item_id"], []).append(a)
            except sqlite3.Error:
                continue
        paths = {a["path"] for lst in self.attaches.values() for a in lst
                 if (a["kind"] or "upload") == "link" and a["path"]}
        for chunk in _chunks(paths, 200):
            ph = ",".join("?" * len(chunk))
            try:
                for r in conn.execute(
                        "SELECT path, COUNT(*) c FROM attachments WHERE kind='link' AND path IN (%s) GROUP BY path" % ph,
                        chunk).fetchall():
                    self.refcounts[r["path"]] = int(r["c"])
            except sqlite3.Error:
                continue


def items_to_dicts(rows, conn, with_attach=True):
    """批量装配：把「每条一次文件夹查询 + 每条一次附件查询 + 每个引用一次全表计数」
    压成整页三次查询。条目一多（大月、提醒列表、搜索翻页）差距是数量级的。"""
    if not rows:
        return []
    if conn is None:
        return [item_to_dict(r, None, with_attach=False) for r in rows]
    bundle = _Bundle(conn, rows, with_attach)
    return [item_to_dict(r, conn, with_attach=with_attach, bundle=bundle) for r in rows]


# 农历↔公历换算是本应用最贵的一步（实测单次约 1.4ms），而每年重复的生日/纪念日
    # 每次列表、每个提醒都要算两三遍，所以结果按 (年,月,日) 记住，算一次用到底。
_LUNAR_NIL = "__nil__"
_lunar_cache = {}
_LUNAR_CACHE_MAX = 30000


def solar_of_lunar(lunar_year, lunar_month, lunar_day):
    """农历(年,月,日) → 公历 date。月/日按普通农历月算（不处理闰月）；
    该年这个月没有三十日就退到廿九。"""
    key = (int(lunar_year), int(lunar_month), int(lunar_day))
    hit = _lunar_cache.get(key)
    if hit is not None:
        if hit == _LUNAR_NIL:
            raise ValueError("没有这个农历月")
        return hit
    m = LunarMonth.fromYm(key[0], key[1])
    if m is None:
        if len(_lunar_cache) > _LUNAR_CACHE_MAX:
            _lunar_cache.clear()
        _lunar_cache[key] = _LUNAR_NIL
        raise ValueError("没有这个农历月")
    solar = Lunar.fromYmd(key[0], key[1], min(key[2], m.getDayCount())).getSolar()
    out = date(solar.getYear(), solar.getMonth(), solar.getDay())
    if len(_lunar_cache) > _LUNAR_CACHE_MAX:
        _lunar_cache.clear()
    _lunar_cache[key] = out
    return out


def yearly_occurrence(date_str, is_lunar, year):
    """每年重复（生日/纪念日）在某一年的公历落点。
    农历的那条：库里存的月/日按农历数字理解（年份只是占位）。"""
    d = parse_date(date_str)
    if d is None:
        return None
    if is_lunar:
        try:
            return solar_of_lunar(int(year), d.month, d.day)
        except (ValueError, IndexError):
            return None
    try:
        return d.replace(year=int(year))
    except ValueError:
        return date(int(year), d.month, min(d.day, 28))


def yearly_hits(conn, day=None, start=None, end=None):
    """生日/纪念日存的是出生那年的日期，按 date 查月份永远查不到；
    这里把它们换算到当年的落点，挑出落在某天 / 某段区间内的。"""
    rows = conn.execute(
        "SELECT * FROM items WHERE yearly=1 AND type IN ('birthday','anniversary')").fetchall()
    out = []
    for r in rows:
        years = sorted({int((day or start)[:4])} | ({int(end[:4])} if end and start and end[:4] != start[:4] else set())) \
            if (day or start) else []
        for y in years:
            occ = yearly_occurrence(r["date"], r["is_lunar"], y)
            if occ is None:
                continue
            iso = occ.isoformat()
            if (day and iso == day) or (start and start <= iso < end):
                out.append((r, iso))
                break
    return out


def yearly_this_year(date_str, is_lunar=0):
    """每年重复的条目：算出“今年”落在哪天。
    is_lunar=1 时，把库里存的月/日当作农历月日（中华万年历的农历生日就是这么存的，
    年份是占位的 2000），换算成今年的公历日期。"""
    d = parse_date(date_str)
    if d is None:
        return date_str
    today = date.today()
    if is_lunar:
        for year in (today.year,):
            try:
                return solar_of_lunar(year, d.month, d.day).isoformat()
            except (ValueError, IndexError):
                pass
    try:
        return d.replace(year=today.year).isoformat()
    except ValueError:
        return d.replace(year=today.year, day=28).isoformat()


def next_occurrence(date_str, base=None, is_lunar=0):
    """最近一次（含今天）的出现日期：每年重复的条目按今年→明年找，普通条目就是它本身"""
    d = parse_date(date_str)
    if d is None:
        return None
    base = base or date.today()
    if is_lunar:
        for year in (base.year, base.year + 1):
            try:
                cand = solar_of_lunar(year, d.month, d.day)
            except (ValueError, IndexError):
                continue
            if cand >= base:
                return cand
        return None
    try:
        cand = d.replace(year=base.year)
    except ValueError:
        cand = d.replace(year=base.year, day=28)
    if cand < base:
        try:
            cand = d.replace(year=base.year + 1)
        except ValueError:
            cand = d.replace(year=base.year + 1, day=28)
    return cand


# ------------------------- 页面路由 -------------------------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/<path:filename>")
def static_files(filename):
    """让 static 目录下的文件（style.css、app.js、图标等）直接通过 /文件名 访问"""
    return send_from_directory(app.static_folder, filename)


# ------------------------- 内容增删改查 API -------------------------

@app.route("/api/health")
def health():
    """健康检查（NAS 部署、反代探活用）"""
    conn = get_db()
    n = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    return jsonify({"ok": True, "items": n, "time": datetime.now().isoformat(timespec="seconds"),
                    "weather_days": len(weather_cache())})


@app.route("/api/items", methods=["GET"])
def list_items():
    """查询条目
    GET /api/items?month=2026-10        当月（日历渲染用，最多 600 条）
    GET /api/items?date=2026-10-17      某天
    GET /api/items?q=辊压机&type=note&limit=50&offset=0   搜索/按类浏览（带总数，可翻页）
    """
    conn = get_db()
    month = request.args.get("month")
    day = request.args.get("date")
    q = (request.args.get("q") or "").strip()
    type_raw = request.args.get("type") or ""
    folder_f = request.args.get("folder")          # v5.13：按文件夹筛（抽屉点文件夹用）
    type_key = TYPE_ALIAS.get(type_raw) or (type_raw if type_raw in TYPE_NAMES else "")
    try:
        limit = max(1, min(int(request.args.get("limit") or 0), 500))
    except ValueError:
        limit = 0
    try:
        offset = max(0, int(request.args.get("offset") or 0))
    except ValueError:
        offset = 0

    if q or type_key:
        where, params = [], []
        if q:
            like = f"%{q}%"
            where.append("(title LIKE ? OR content LIKE ? OR tags LIKE ? OR location LIKE ? OR weather LIKE ?)")
            params += [like] * 5
        if type_key:
            where.append("type=?")
            params.append(type_key)
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        total = conn.execute("SELECT COUNT(*) FROM items" + sql_where, params).fetchone()[0]
        cap = limit or 100
        rows = conn.execute(
            "SELECT * FROM items" + sql_where + " ORDER BY date DESC, time DESC, id DESC LIMIT ? OFFSET ?",
            params + [cap, offset]).fetchall()
        result = items_to_dicts(rows, conn)
        conn.close()
        return jsonify({"items": result, "total": total, "returned": len(result),
                        "offset": offset, "limit": cap})

    if folder_f:
        try:
            fidq = int(folder_f)
        except (TypeError, ValueError):
            fidq = 0
        fl = conn.execute("SELECT name FROM folders WHERE id=?", (fidq,)).fetchone()
        if fidq == 0:
            rows = conn.execute("SELECT * FROM items WHERE folder_id=0 ORDER BY date DESC, id DESC LIMIT 500").fetchall()
        elif fl is None:
            conn.close()
            return jsonify({"error": "文件夹不存在"}), 404
        else:
            rows = conn.execute("SELECT * FROM items WHERE folder_id=? ORDER BY date DESC, id DESC LIMIT 500", (fidq,)).fetchall()
        out = items_to_dicts(rows, conn)
        conn.close()
        return jsonify({"items": out, "total": len(out), "folder": (fl["name"] if fl else "未归类")})

    if month:
        try:
            y, m = month.split("-")
            start = f"{int(y):04d}-{int(m):02d}-01"
            # 下个月的第一天
            ny, nm = (int(y), int(m) + 1) if int(m) < 12 else (int(y) + 1, 1)
            end = f"{ny:04d}-{nm:02d}-01"
        except (ValueError, AttributeError):
            conn.close()
            return jsonify({"error": "月份格式应为 YYYY-MM"}), 400
        rows = conn.execute(
            "SELECT * FROM items WHERE date >= ? AND date < ? ORDER BY date, time LIMIT 2000",
            (start, end),
        ).fetchall()
        result = items_to_dicts(rows, conn)
        # 每年重复的生日/纪念日换算到今年这个月再放进来
        extra = []
        for r, iso in yearly_hits(conn, start=start, end=end):
            it = dict(item_to_dict(r, conn))
            it["this_year_date"] = iso
            it["date_grid"] = iso
            extra.append(it)
        if extra:
            seen = {it["id"] for it in result}
            result.extend([e for e in extra if e["id"] not in seen])
        result.sort(key=lambda x: (x.get("date_grid") or x["date"], x.get("time") or ""))
        conn.close()          # 这条 return 原先没关连接，请求一多就是句柄泄漏
        led_daily = LEDGER["month_ledger"](start[:7]) if LEDGER.get("month_ledger") else {}
        return jsonify({"items": result, "ledger_daily": led_daily})
    elif day:
        rows = conn.execute(
            "SELECT * FROM items WHERE date = ? ORDER BY time, id", (day,)
        ).fetchall()
        result = items_to_dicts(rows, conn)
        ids = {it["id"] for it in result}
        for r, iso in yearly_hits(conn, day=day):
            if r["id"] not in ids:
                it = dict(item_to_dict(r, conn))
                it["this_year_date"] = iso
                result.append(it)
        conn.close()
        return jsonify({"items": result})
    else:
        conn.close()
        return jsonify({"error": "需要 month / date / q / type 参数"}) , 400


@app.route("/api/stats")
def stats():
    """各类型条数 + 年份分布（抽屉分类、年月跳转用）"""
    conn = get_db()
    counts = {t: 0 for t in TYPE_NAMES}
    for r in conn.execute("SELECT type, COUNT(*) c FROM items GROUP BY type").fetchall():
        counts[r["type"]] = r["c"]
    years = [{"year": r["y"], "count": r["c"]} for r in conn.execute(
        "SELECT substr(date,1,4) y, COUNT(*) c FROM items GROUP BY y ORDER BY y DESC").fetchall()]
    total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    conn.close()
    try:
        c2 = get_db()
        links = c2.execute("SELECT COUNT(*) c FROM attachments WHERE kind='link'").fetchone()["c"]
        missing = c2.execute("SELECT COUNT(*) c FROM attachments WHERE kind='link' AND missing=1").fetchone()["c"]
        c2.close()
    except sqlite3.Error:
        links, missing = 0, 0
    return jsonify({"counts": counts, "years": years, "total": total,
                    "links": links, "links_missing": missing})


@app.route("/api/day")
def day_info():
    """某天的完整信息：日历内容 + 农历黄历 + 当天天气（给「点日期只看当天」用）
    GET /api/day?date=2026-10-17
    """
    day = request.args.get("date") or date.today().isoformat()
    d = parse_date(day)
    if d is None:
        return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM items WHERE date = ? ORDER BY time, id", (day,)
    ).fetchall()
    items = items_to_dicts(rows, conn)
    ids = {it["id"] for it in items}
    for r, iso in yearly_hits(conn, day=day):          # 农历生日按农历落在今年
        if r["id"] not in ids:
            it = dict(item_to_dict(r, conn))
            it["this_year_date"] = iso
            items.append(it)
    items.sort(key=lambda x: (x.get("time") or "99:99", x["id"]))
    # 计数（只取需要的字段，页面显示共多少条）
    counts = {}
    for it in items:
        counts[it["type"]] = counts.get(it["type"], 0) + 1
    # 当天天气：① 条目里已有的 → ② 历史天气缓存 → ③ 联网现拉（NAS 能上外网时）
    weather = ""
    for it in items:
        if it.get("weather"):
            weather = it["weather"]
            break
    source = "条目" if weather else ""
    if not weather:
        weather = cached_weather(day)
        source = "天气档案" if weather else ""
    weather_updating = 0
    if d == date.today() and day not in _LIVE_DONE and not request.args.get("live"):
        # 联网最慢能拖到数秒，不能压着首屏：本次先回档案值，实测在后台刷进库
        _LIVE_DONE.add(day)
        weather_updating = 1
        run_in_bg("live-wx:" + day, refresh_today_weather, day, current_uid())
    if request.args.get("live") == "1":               # 点「取天气」：强制按实测重新校准
        live_w = fetch_weather_online(d)
        if live_w:
            if weather and live_w.split(" ", 1)[-1] != weather.split(" ", 1)[-1]:
                conn.execute("UPDATE main.items SET weather=? WHERE date=? AND weather<>''" +
                             VIS.format(u=current_uid()), (live_w, day))   # 只改自己可见范围内的
                conn.commit()
            weather, source = live_w, "实时校准"
    _wc = weather_cache().get(day) or {}   # v6.1：把"凭什么这么写"告诉前端
    weather_src = ("open-meteo(实测校准)" if (_wc.get("src") or "").startswith("open-meteo") else source)
    weather_mm = _wc.get("mm", "")
    conn.close()
    cache = weather_cache()
    days = sorted(k for k in cache if len(k) == 10)
    fetchable = (d >= date(1940, 1, 1) and (d - date.today()).days <= 15
                 and day not in _weather_miss)
    led_day = LEDGER["day_ledger"](day) if LEDGER.get("day_ledger") else {}
    return jsonify({"date": day, "ledger": led_day, "almanac": almanac_info(d), "weather": weather,
                    "weather_src": weather_src, "weather_mm": weather_mm,
                    "weather_updating": weather_updating,
                    "weather_source": source, "fetchable": 1 if fetchable else 0,
                    "cache_start": days[0] if days else "", "cache_end": days[-1] if days else "",
                    "items": items, "counts": counts})


@app.route("/api/daywx")
def api_daywx():
    """按日期取天气（编辑框用）。live=1 强制按实测重取。"""
    day = (request.args.get("date") or "").strip()
    live = request.args.get("live") == "1"
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return jsonify({"error": "日期不对"}), 400
    w = "" if live else cached_weather(day)
    src = "天气档案" if w else ""
    if not w or live:
        live_w = fetch_weather_online(d)
        if live_w:
            w, src = live_w, "实时校准"
    return jsonify({"date": day, "weather": w, "src": src})



@app.route("/api/ui")
def api_ui_get():
    """界面偏好（列宽）。换浏览器/换设备都从这里读，所以手机上拖一次，公司电脑也一样。"""
    ukey = "cols:%d" % current_uid()
    conn = get_db()
    try:
        row = conn.execute("SELECT val FROM ui_state WHERE key=?", (ukey,)).fetchone()
    except Exception:
        row = None
    conn.close()
    try:
        cols = json.loads(row[0]) if row and row[0] else {}
    except (ValueError, TypeError):
        cols = {}
    return jsonify({"ok": 1, "cols": cols})


@app.route("/api/ui/save", methods=["POST", "PUT"])
def api_ui_put():
    """只认白名单键，值必须是范围内的整数，其它一律忽略。"""
    data = request.get_json(silent=True) or {}
    cols = data.get("cols") or {}
    clean = {}
    for k, lo, hi in (("cal", 240, 1600), ("mid", 240, 1600), ("fsHead", 11, 32)):
        try:
            v = int(cols.get(k))
        except (TypeError, ValueError):
            continue
        if lo <= v <= hi:
            clean[k] = v
    if not clean:
        return jsonify({"ok": 0, "error": "no valid value"}), 400
    ukey = "cols:%d" % current_uid()
    conn = get_db()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS ui_state (key TEXT PRIMARY KEY, val TEXT DEFAULT '')")
        old = conn.execute("SELECT val FROM ui_state WHERE key=?", (ukey,)).fetchone()
        try:
            merged = json.loads(old[0]) if old and old[0] else {}
        except (ValueError, TypeError):
            merged = {}
        merged.update(clean)
        payload = json.dumps(merged)
        if old:
            conn.execute("UPDATE ui_state SET val=? WHERE key=?", (payload, ukey))
        else:
            conn.execute("INSERT INTO ui_state(key,val) VALUES(?,?)", (ukey, payload))
        conn.commit()
    finally:
        conn.close()
    return jsonify({"ok": 1, "cols": merged})


@app.route("/api/day/weather", methods=["POST"])
def save_day_weather():
    """把某天的天气写进那天的内容（只填空白的，不覆盖已手动填过的）
    body: {"date":"2023-06-01","weather":"兴安县 多云 31°/23°"}
    """
    data = request.get_json(silent=True) or {}
    day = str(data.get("date") or "")
    weather = str(data.get("weather") or "").strip()
    if parse_date(day) is None or not weather:
        return jsonify({"error": "需要 date 和 weather"}), 400
    conn = get_db()
    scope = VIS.format(u=current_uid())
    cur = conn.execute("UPDATE main.items SET weather=?, updated_at=datetime('now','localtime') "
                       "WHERE date=? AND (weather IS NULL OR weather='')" + scope, (weather, day))
    n = cur.rowcount
    if n == 0:
        conn.execute("UPDATE main.items SET weather=?, updated_at=datetime('now','localtime') "
                     "WHERE date=?" + scope, (weather, day))
        n = conn.execute("SELECT changes()").fetchone()[0]
    conn.commit()
    conn.close()
    return jsonify({"updated": n, "weather": weather})


@app.route("/api/search")
def search_items():
    """搜索（/api/items?q= 的别名，前端两个入口都能用）"""
    return list_items()


@app.route("/api/items/<int:item_id>", methods=["GET"])
def get_item(item_id):
    """读取单条内容（搜索结果里点开时用）"""
    conn = get_db()
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    result = item_to_dict(row, conn) if row is not None else None
    conn.close()
    if result is None:
        return jsonify({"error": "内容不存在"}), 404
    return jsonify({"item": result})


@app.route("/api/items", methods=["POST"])
def create_item():
    """新建一条内容"""
    data = request.get_json(silent=True) or {}
    type_key = TYPE_ALIAS.get(str(data.get("type", "")).strip())
    title = str(data.get("title", "")).strip()
    if not type_key:
        return jsonify({"error": "类型不正确，可选：日程/生日/纪念日/倒数日/笔记/待办"}), 400
    day = data.get("date") or date.today().isoformat()
    if parse_date(day) is None:
        return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400
    # 笔记/待办不写标题时，默认用当天日期当标题（如 2026.10.17）
    if not title:
        if type_key in ("note", "todo"):
            title = day.replace("-", ".")
        else:
            return jsonify({"error": "标题不能为空"}), 400

    conn = get_db()
    cur = conn.execute(
        """INSERT INTO main.items
           (type, title, content, date, time, end_time, all_day, location,
            remind_minutes, is_lunar, yearly, done, tags, weather, todo_items,
            folder_id, birth_year, owner_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            type_key,
            title,
            str(data.get("content", "")),
            day,
            str(data.get("time", "")).strip(),
            str(data.get("end_time", "")).strip(),
            1 if data.get("all_day") else 0,
            str(data.get("location", "")).strip(),
            int(data.get("remind_minutes", 0) or 0),
            1 if data.get("is_lunar") else 0,
            1 if data.get("yearly") else 0,
            1 if data.get("done") else 0,
            str(data.get("tags", "")).strip(),
            str(data.get("weather", "")).strip(),
            json.dumps(data.get("todo_items") or [], ensure_ascii=False),
            int(data.get("folder_id") or 0),
            int(data.get("birth_year") or 0),
            current_uid(),
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM items WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    if row is None:
        return jsonify({"error": "写入失败"}), 500
    result = item_to_dict(row, None)
    return jsonify({"item": result}), 201


@app.route("/api/items/<int:item_id>", methods=["PUT"])
def update_item(item_id):
    """修改一条内容"""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "内容不存在"}), 404

    title = str(data.get("title", row["title"])).strip() or row["title"]
    day = data.get("date", row["date"])
    if parse_date(day) is None:
        conn.close()
        return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400
    type_key = TYPE_ALIAS.get(str(data.get("type", row["type"])).strip()) or row["type"]

    conn.execute(
        """UPDATE main.items SET type=?, title=?, content=?, date=?, time=?, end_time=?, all_day=?,
           location=?, remind_minutes=?, is_lunar=?, yearly=?, done=?, tags=?, weather=?,
           todo_items=?, folder_id=?, birth_year=?, updated_at=datetime('now','localtime')
           WHERE id=?""",
        (
            type_key,
            title,
            str(data.get("content", row["content"])),
            day,
            str(data.get("time", row["time"])).strip(),
            str(data.get("end_time", row["end_time"] if "end_time" in row.keys() else "")).strip(),
            1 if data.get("all_day", row["all_day"] if "all_day" in row.keys() else 0) else 0,
            str(data.get("location", row["location"] if "location" in row.keys() else "")).strip(),
            int(data.get("remind_minutes", row["remind_minutes"]) or 0),
            1 if data.get("is_lunar", row["is_lunar"]) else 0,
            1 if data.get("yearly", row["yearly"]) else 0,
            1 if data.get("done", row["done"]) else 0,
            str(data.get("tags", row["tags"])).strip(),
            str(data.get("weather", row["weather"] if "weather" in row.keys() else "")).strip(),
            json.dumps(data.get("todo_items", row["todo_items"] if "todo_items" in row.keys() else []), ensure_ascii=False),
            int(data.get("folder_id", row["folder_id"] if "folder_id" in row.keys() else 0) or 0),
            int(data.get("birth_year", row["birth_year"] if "birth_year" in row.keys() else 0) or 0),
            item_id,
        ),
    )
    conn.commit()
    row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
    result = item_to_dict(row, conn)
    conn.close()
    return jsonify({"item": result})


@app.route("/api/items/<int:item_id>", methods=["DELETE"])
def delete_item(item_id):
    """删除一条内容 = 放进垃圾桶：只打时间戳，附件一个都不动，恢复时原样回来。"""
    conn = get_db()
    if conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone() is None:
        conn.close()
        return jsonify({"error": "内容不存在"}), 404
    conn.execute("UPDATE main.items SET deleted_at=?, updated_at=? "
                 "WHERE id=? AND COALESCE(deleted_at,'')=''", (_now(), _now(), item_id))
    left = conn.execute("SELECT COUNT(*) c FROM main.items WHERE COALESCE(deleted_at,'')<>''").fetchone()["c"]
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "trashed": True, "kept_days": TRASH_DAYS, "trash_total": left})


# ------------------------- 附件 API -------------------------

@app.route("/api/items/<int:item_id>/attachments", methods=["POST"])
def upload_attachment(item_id):
    """给某条内容上传附件（图片等）
    用法：表单字段名 file，一次传一个文件
    """
    f = request.files.get("file")
    if f is None or not f.filename:
        return jsonify({"error": "没有选择文件"}), 400
    conn = get_db()
    row = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "内容不存在"}), 404
    # 限制单文件 20MB，防撑爆 NAS
    f.stream.seek(0, os.SEEK_END)
    size = f.stream.tell()
    f.stream.seek(0)
    if size > 20 * 1024 * 1024:
        conn.close()
        return jsonify({"error": "文件超过 20MB 限制"}), 400

    os.makedirs(ATTACH_DIR, exist_ok=True)
    ext = os.path.splitext(f.filename)[1][:10].lower()
    stored = uuid.uuid4().hex + ext
    f.save(os.path.join(ATTACH_DIR, stored))
    cur = conn.execute(
        "INSERT INTO main.attachments (item_id, filename, stored_name, mime, size, kind) VALUES (?,?,?,?,?,'upload')",
        (item_id, f.filename, stored, f.mimetype or "", size),
    )
    conn.commit()
    a = conn.execute("SELECT * FROM attachments WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify({"attachment": {
        "id": a["id"], "filename": a["filename"], "mime": a["mime"], "size": a["size"],
        "url": f"/api/attachments/{a['id']}",
    }}), 201


@app.route("/api/nas/preview")
def nas_preview():
    """按路径直接预览/下载（还没保存的引用也能先看）"""
    stored = (request.args.get("path") or "").strip()
    abs_p, err = _resolve_ref(stored)
    if not abs_p:
        return jsonify({"error": err or "读不到这个文件", "missing": 1}), 404
    dl = (request.args.get("dl") or "").lower() in ("1", "true", "yes")
    resp = send_file(abs_p, mimetype=_mime_of(abs_p), download_name=os.path.basename(abs_p),
                     as_attachment=dl, conditional=True)
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/api/attachments/<int:att_id>")
def get_attachment(att_id):
    """读取附件（网页里直接当图片地址用）"""
    conn = get_db()
    a = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    if a is None:
        conn.close()
        return jsonify({"error": "附件不存在"}), 404
    if conn.execute("SELECT id FROM items WHERE id=?", (a["item_id"],)).fetchone() is None:
        conn.close()
        return jsonify({"error": "附件不存在"}), 404
    if (a["kind"] or "upload") == "link":
        abs_p, err = _resolve_ref(a["path"])
        if not abs_p:
            conn.execute("UPDATE main.attachments SET missing=1 WHERE id=?", (att_id,))
            conn.commit()
            conn.close()
            return jsonify({"error": err or "原文件读不到", "missing": 1}), 410
        now_size = int(os.path.getsize(abs_p))
        if a["missing"] or int(a["size"] or 0) != now_size:   # 只有真变了才写库，GET 不再是写操作
            conn.execute("UPDATE main.attachments SET missing=0, size=? WHERE id=?", (now_size, att_id))
            conn.commit()
        conn.close()
        if NAS_PREVIEW_MAX_MB and a["mime"] and not a["mime"].startswith("image/") and now_size > NAS_PREVIEW_MAX_MB * 1048576:
            return jsonify({"error": "这个文件太大，直接下载或去飞牛里打开",
                            "too_big": 1, "size": now_size}), 413
        dl = (request.args.get("dl") or "").lower() in ("1", "true", "yes")
        resp = send_file(abs_p, mimetype=a["mime"] or _mime_of(abs_p),
                         download_name=a["filename"], as_attachment=dl, conditional=True)
        resp.headers["Cache-Control"] = "no-cache"   # 引用可能原地更新，别长期缓存
        return resp
    conn.close()
    path = os.path.join(ATTACH_DIR, a["stored_name"])
    if not os.path.exists(path):
        return jsonify({"error": "文件已丢失"}), 404
    dl = (request.args.get("dl") or "").lower() in ("1", "true", "yes")
    resp = send_file(path, mimetype=a["mime"] or "application/octet-stream",
                     download_name=a["filename"], as_attachment=dl, conditional=True)
    # 附件存的是随机文件名、内容不会变 → 让手机/浏览器永久缓存：
    # 同一条第二次点开不再下载、不再重新解码（实测第二次 0 请求 0 字节）
    resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
    return resp


@app.route("/api/attachments/<int:att_id>/thumb")
def attachment_thumb(att_id):
    """缩略图（NAS 引用与上传的文件都走这里），缓存到 data/thumbs，可随时整目录删。"""
    conn = get_db()
    a = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    conn.close()
    if a is None:
        return jsonify({"error": "附件不存在"}), 404
    _c = get_db()
    _ok = _c.execute("SELECT id FROM items WHERE id=?", (a["item_id"],)).fetchone() is not None
    _c.close()
    if not _ok:
        return jsonify({"error": "附件不存在"}), 404
    if (a["kind"] or "upload") == "link":
        abs_p, _err = _resolve_ref(a["path"])
    else:
        abs_p = os.path.join(ATTACH_DIR, a["stored_name"])
        if not os.path.exists(abs_p):
            abs_p = None
    if not abs_p:
        return jsonify({"error": "读不到文件"}), 404
    try:
        w = max(48, min(640, int(request.args.get("w") or 160)))
    except (TypeError, ValueError):
        w = 160
    out = _make_thumb(abs_p, w)
    if not out:
        # 出不了图就明确说清楚，别偷偷跳到别的地址（之前缩略图"叠在一起"就是这个引起的错觉）
        return jsonify({"error": "这个文件生成不了缩略图", "path": _alias_of(abs_p)}), 406
    return send_file(out, mimetype="image/jpeg", conditional=True)


@app.route("/api/attachments/<int:att_id>/solidify", methods=["POST"])
def attachment_solidify(att_id):
    """「存一份」：把 NAS 原文件复制进应用存储，从此不受原文件被删/移动影响。"""
    conn = get_db()
    a = conn.execute("SELECT * FROM attachments WHERE id=?", (att_id,)).fetchone()
    if a is None or (a["kind"] or "upload") != "link":
        conn.close()
        return jsonify({"error": "只有 NAS 引用才能存一份"}), 400
    if conn.execute("SELECT id FROM items WHERE id=?", (a["item_id"],)).fetchone() is None:
        conn.close()
        return jsonify({"error": "无权操作这个引用"}), 403
    abs_p, err = _resolve_ref(a["path"])
    if not abs_p:
        conn.close()
        return jsonify({"error": err or "原文件读不到"}), 410
    size = os.path.getsize(abs_p)
    if size > 20 * 1048576:
        conn.close()
        return jsonify({"error": "超过 20MB，先压缩或去飞牛里备份"}), 400
    os.makedirs(ATTACH_DIR, exist_ok=True)
    ext = os.path.splitext(abs_p)[1][:10].lower()
    stored = uuid.uuid4().hex + ext
    try:
        with open(abs_p, "rb") as fsrc, open(os.path.join(ATTACH_DIR, stored), "wb") as fdst:
            shutil.copyfileobj(fsrc, fdst)
    except OSError as e:
        conn.close()
        return jsonify({"error": "复制失败：" + str(e)[:60]}), 500
    conn.execute("UPDATE main.attachments SET kind='upload', stored_name=?, size=?, missing=0 WHERE id=?",
                 (stored, size, att_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": 1, "stored": stored}), 201


@app.route("/api/items/<int:item_id>/refs", methods=["POST"])
def add_refs(item_id):
    """只建引用，不上传：body {"paths":["相片/2026/10/IMG.jpg", ...]}"""
    data = request.get_json(silent=True) or {}
    paths = data.get("paths") or []
    if isinstance(paths, str):
        paths = [paths]
    conn = get_db()
    if conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone() is None:
        conn.close()
        return jsonify({"error": "内容不存在"}), 404
    added, skipped = [], []
    for raw in paths[:100]:
        abs_p, err = _resolve_ref(raw)
        if not abs_p:
            skipped.append({"path": raw, "error": err or "读不到"})
            continue
        if os.path.isdir(abs_p):
            skipped.append({"path": raw, "error": "那是个文件夹"})
            continue
        sig = _ref_sig(abs_p)
        dup = conn.execute("SELECT id FROM attachments WHERE item_id=? AND sig=? AND sig<>''", (item_id, sig)).fetchone()
        if dup:
            skipped.append({"path": raw, "error": "这条内容里已经有了"})
            continue
        info = _ref_info(abs_p)
        cur = conn.execute("""INSERT INTO main.attachments (item_id, filename, stored_name, mime, size, kind, path, sig)
                              VALUES (?,?,?,?,?,'link',?,?)""",
                           (item_id, os.path.basename(abs_p), "", info["mime"], info["size"],
                            info["path"], sig))
        added.append(_att_dict(conn.execute("SELECT * FROM attachments WHERE id=?", (cur.lastrowid,)).fetchone(), conn))
    conn.commit()
    conn.close()
    # 就算全被跳过也返回 200：这是"引用"不是"上传"，前端按明细提示，绝不让笔记保存失败
    return jsonify({"added": added, "skipped": skipped}), 200


@app.route("/api/items/<int:item_id>/refs", methods=["DELETE"])
def drop_all_refs(item_id):
    """清掉某条内容上的所有 NAS 引用（原始文件一点不动）"""
    conn = get_db()
    conn.execute("DELETE FROM main.attachments WHERE item_id=? AND kind='link'", (item_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": 1})


@app.route("/api/nas/ls")
def nas_ls():
    """浏览被授权的 NAS 目录：列子目录 + 文件（一次最多 NAS_MAX_FILES 条）"""
    target = (request.args.get("dir") or "").strip()
    ext_only = (request.args.get("only") or "").strip()
    if not target:
        roots = []
        for r in NAS_ROOTS:
            okd = os.path.isdir(r["real"])
            roots.append({"name": r["alias"], "path": r["alias"], "is_dir": True, "mounted": okd,
                          "n": 0, "n_more": 0, "size": 0, "mtime": 0, "mime": "", "is_image": False})
        return jsonify({"dir": "", "parent": None, "roots": True, "entries": roots})
    abs_dir, err = _resolve_ref(target)
    if not abs_dir:
        return jsonify({"error": err, "entries": []}), 403
    if not os.path.isdir(abs_dir):
        return jsonify({"error": "不是文件夹", "entries": []}), 400
    sort_by = (request.args.get("sort") or "name").lower()      # name | time | size
    desc = (request.args.get("desc") or "") in ("1", "true", "yes")
    count_cap = int(os.environ.get("NAS_DIR_COUNT", "120"))      # 目录子项只数到这儿，超出标 n_more
    dirs, files = [], []
    roots = _roots_ready()
    try:
        with os.scandir(abs_dir) as it:
            raw = list(it)
    except OSError as e:
        return jsonify({"error": "打不开这个文件夹：" + str(e)[:60], "entries": []}), 403
    for e in raw:
        name = e.name
        if name.startswith("."):
            continue
        full = e.path
        try:
            real = os.path.realpath(full)
        except OSError:
            continue
        okdir = False
        for r in roots:
            if real == r["real"] or real.startswith(r["real"] + os.sep):
                okdir = True
                break
        if not okdir:
            continue
        try:
            isd = e.is_dir(follow_symlinks=False)
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        # 原来只带 size，排序键要的 mtime 压根没塞进去 → 「按时间」排序其实是失效的
        item = {"name": name, "path": _alias_of(full), "is_dir": isd, "mounted": True,
                "n": 0, "n_more": 0, "size": 0 if isd else int(st.st_size),
                "mtime": int(st.st_mtime),
                "mime": "" if isd else _mime_of(name), "is_image": (not isd) and _is_img_name(name),
                "is_video": (not isd) and str(name).lower().endswith(VIDEO_EXT)}
        if isd:
            if count_cap > 0:
                try:
                    cnt = 0
                    with os.scandir(full) as sub:
                        for _x in sub:
                            cnt += 1
                            if cnt >= count_cap:
                                item["n_more"] = 1
                                break
                    item["n"] = cnt
                except OSError:
                    pass
            dirs.append(item)
        else:
            if ext_only == "img" and not item["is_image"]:
                continue
            if ext_only == "doc" and item["is_image"]:
                continue
            files.append(item)
    if sort_by == "time":
        files.sort(key=lambda x: (x["mtime"], x["name"]), reverse=desc)
    elif sort_by == "size":
        files.sort(key=lambda x: (x["size"], x["name"]), reverse=desc)
    else:
        files.sort(key=lambda x: x["name"].lower(), reverse=desc)
    dirs = sorted(dirs, key=lambda x: x["name"].lower(), reverse=bool(desc) and sort_by == "name")
    allv = dirs + files
    cut = (request.args.get("q") or "").strip().lower()
    if cut:
        allv = [x for x in allv if cut in x["name"].lower()]
    parts = target.split("/", 1)
    parent = parts[0] if len(parts) > 1 else (target.rsplit("/", 1)[0] if "/" in target else "")
    total = len(allv)
    try:
        off = max(0, int(request.args.get("off") or 0))
    except (TypeError, ValueError):
        off = 0
    return jsonify({"dir": target, "parent": parent, "sort": sort_by, "desc": 1 if desc else 0,
                    "entries": allv[off:off + NAS_MAX_FILES],
                    "total": total, "dirs": len(dirs), "files": total - len(dirs),
                    "roots": [r["alias"] for r in NAS_ROOTS]})


@app.route("/api/nas/pick", methods=["POST"])
def nas_can_read():
    """给前端做「能不能读」的探活（挂载没生效时能说人话）"""
    data = request.get_json(silent=True) or {}
    ok_, err = _resolve_ref(data.get("path") or "")
    # 不回传容器内的绝对路径，前端只用得到「能不能读」
    return jsonify({"ok": bool(ok_), "error": err})




VIDEO_EXT = (".mp4", ".mov", ".m4v", ".avi", ".mkv", ".webm", ".wmv", ".flv", ".3gp")


def _ffmpeg_bin():
    import shutil as _sh
    return _sh.which("ffmpeg")


@app.route("/api/nas/thumb")
def nas_thumb():
    """给浏览器直接出缩略图：图片走 Pillow，视频用 ffmpeg 抽一帧。都落到 data/thumbs 缓存。"""
    stored = (request.args.get("path") or "").strip()
    abs_p, err = _resolve_ref(stored)
    if not abs_p:
        return jsonify({"error": err or "读不到这个文件"}), 403
    try:
        w = max(48, min(640, int(request.args.get("w") or 220)))
    except (TypeError, ValueError):
        w = 220
    st = os.stat(abs_p)
    key = hashlib.md5(("%s|%d|%d|nas-thumb|%d" % (abs_p, int(st.st_size), int(st.st_mtime), w)).encode()).hexdigest()
    out = os.path.join(THUMB_DIR, key + ".jpg")
    if not (os.path.exists(out) and os.path.getmtime(out) >= os.path.getmtime(abs_p)):
        made = False
        if _is_img_name(abs_p):
            try:
                from PIL import Image
                im = Image.open(abs_p)
                im.draft("RGB", (w * 2, w * 2))
                im = im.convert("RGB")
                im.thumbnail((w, w), Image.LANCZOS)
                os.makedirs(THUMB_DIR, exist_ok=True)
                tmp = out + ".tmp"
                im.save(tmp, "JPEG", quality=80, optimize=True)
                os.replace(tmp, out)
                made = True
            except Exception:
                made = False
        if not made and str(abs_p).lower().endswith(VIDEO_EXT):
            ff = _ffmpeg_bin()
            if ff:
                import subprocess
                try:
                    os.makedirs(THUMB_DIR, exist_ok=True)
                    tmp = out + ".tmp.jpg"
                    subprocess.run([ff, "-y", "-ss", "1", "-i", abs_p, "-frames:v", "1",
                                    "-vf", "scale=%d:-2" % w, tmp],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=20)
                    if os.path.exists(tmp) and os.path.getsize(tmp) > 500:
                        os.replace(tmp, out)
                        made = True
                    elif os.path.exists(tmp):
                        os.remove(tmp)
                except Exception:
                    made = False
        _thumb_sweep()
    if os.path.exists(out):
        return send_file(out, mimetype="image/jpeg", conditional=True)
    return jsonify({"error": "这个类型出不了缩略图"}), 404


@app.route("/api/refs/broken")
def refs_broken():
    """列一下失效的 NAS 引用（原文件被删/改名）"""
    conn = get_db()
    rows = conn.execute("""SELECT a.id AS att_id, a.item_id, a.filename, a.path, i.title
                           FROM attachments a JOIN items i ON i.id=a.item_id
                           WHERE a.kind='link' ORDER BY i.date DESC LIMIT 300""").fetchall()
    out = []
    for r in rows:
        abs_p, err = _resolve_ref(r["path"])
        if abs_p:
            if r["att_id"]:
                conn.execute("UPDATE main.attachments SET missing=0 WHERE id=?", (r["att_id"],))
            continue
        conn.execute("UPDATE main.attachments SET missing=1 WHERE id=?", (r["att_id"],))
        out.append({"att_id": r["att_id"], "item_id": r["item_id"], "title": r["title"], "path": r["path"], "error": err})
    conn.commit()
    conn.close()
    return jsonify({"broken": out, "count": len(out)})


@app.route("/api/attachments/<int:att_id>", methods=["DELETE"])
def delete_attachment(att_id):
    """从记录里移出一个附件 = 进垃圾桶（文件留在原地）；桶里再点彻底删除才真删。"""
    conn = get_db()
    a = conn.execute("SELECT * FROM main.attachments WHERE id=?", (att_id,)).fetchone()
    if a is None:
        conn.close()
        return jsonify({"error": "附件不存在"}), 404
    if (a["deleted_at"] or ""):                       # 已经在桶里 → 这次是彻底删除
        _drop_files(conn, [a])
        conn.execute("DELETE FROM main.attachments WHERE id=?", (att_id,))
        conn.commit()
        conn.close()
        return jsonify({"ok": True, "purged": True})
    conn.execute("UPDATE main.attachments SET deleted_at=? WHERE id=?", (_now(), att_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "trashed": True, "kept_days": TRASH_DAYS})


# ───────────────────── 垃圾桶（v9.5：删除先进桶，可恢复）─────────────────────
TRASH_DAYS = int(os.environ.get("TRASH_KEEP_DAYS") or 30)      # 桶里最多留多少天


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _then(s2):
    try:
        return datetime.strptime(s2, "%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return datetime.min


def _owntrash(conn, iid, uid, admin):
    """桶里的这条记录能不能由当前用户处置：自己的（管理员全部）。"""
    w = "" if admin else " AND (owner_id=? OR COALESCE(private,0)=0)"
    p = [] if admin else [uid]
    return conn.execute("SELECT id FROM main.items WHERE id=?" + w + " AND COALESCE(deleted_at,\'\')<>\'\'",
                        [iid] + p).fetchone() is not None


def _drop_files(conn, rows):
    """真删附件时连上传文件一起删；NAS 引用没有本应用的文件，只删行。"""
    for a in rows:
        if a["kind"] != "link":
            try:
                os.remove(os.path.join(ATTACH_DIR, a["stored_name"]))
            except OSError:
                pass


def purge_trash(conn):
    """清掉桶里超过 TRASH_DAYS 天的记录与附件（打开桶、启动、后台巡检都会调）。"""
    cut = (datetime.now() - timedelta(days=TRASH_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM main.items WHERE COALESCE(deleted_at,\'\')<>\'\' AND deleted_at<?", (cut,))]
    for iid in ids:
        _drop_files(conn, conn.execute("SELECT * FROM main.attachments WHERE item_id=?", (iid,)))
        conn.execute("DELETE FROM main.attachments WHERE item_id=?", (iid,))
        conn.execute("DELETE FROM main.items WHERE id=?", (iid,))
    aids = [r["id"] for r in conn.execute(
        "SELECT id FROM main.attachments WHERE COALESCE(deleted_at,\'\')<>\'\' AND deleted_at<?", (cut,))]
    for aid in aids:
        row = conn.execute("SELECT * FROM main.attachments WHERE id=?", (aid,)).fetchone()
        if row is not None:
            _drop_files(conn, [row])
        conn.execute("DELETE FROM main.attachments WHERE id=?", (aid,))
    conn.commit()
    return len(ids), len(aids)


@app.route("/api/trash")
def api_trash():
    """垃圾桶清单：被删的记录（附件都还在）+ 单独移出来的附件。"""
    conn = get_db()
    uid, admin = current_uid(), is_admin()
    purge_trash(conn)
    w = "" if admin else " AND (owner_id=? OR COALESCE(private,0)=0)"
    p = [] if admin else [uid]
    out = []
    for r in conn.execute("SELECT * FROM main.items WHERE COALESCE(deleted_at,\'\')<>\'\'" + w +
                          " ORDER BY deleted_at DESC LIMIT 500", p):
        used = (datetime.now() - _then(r["deleted_at"])).days
        out.append({"id": r["id"], "type": r["type"], "title": r["title"] or "（无标题）",
                    "date": r["date"], "time": r["time"] or "", "content": (r["content"] or "")[:120],
                    "deleted_at": r["deleted_at"], "left_days": max(0, TRASH_DAYS - used),
                    "att_n": len(conn.execute("SELECT id FROM main.attachments WHERE item_id=?", (r["id"],)).fetchall())})
    loose = []
    for a in conn.execute("SELECT * FROM main.attachments WHERE COALESCE(deleted_at,\'\')<>\'\'"
                          " AND item_id NOT IN (SELECT id FROM main.items WHERE COALESCE(deleted_at,\'\')<>\'\')"
                          " ORDER BY deleted_at DESC LIMIT 300"):
        used = (datetime.now() - _then(a["deleted_at"])).days
        loose.append({"id": a["id"], "item_id": a["item_id"], "filename": a["filename"], "kind": a["kind"],
                      "size": a["size"], "left_days": max(0, TRASH_DAYS - used)})
    conn.close()
    return jsonify({"items": out, "loose": loose, "count": len(out) + len(loose), "kept_days": TRASH_DAYS})


@app.route("/api/trash/restore", methods=["POST"])
def api_trash_restore():
    """恢复：ids=记录（连同它挂的附件一起回来），att_ids=单独移出的附件。"""
    d = request.get_json(silent=True) or {}
    ids = [int(x) for x in (d.get("ids") or []) if str(x).strip().isdigit()]
    aids = [int(x) for x in (d.get("att_ids") or []) if str(x).strip().isdigit()]
    conn = get_db()
    uid, admin = current_uid(), is_admin()
    done = refused = 0
    for iid in ids[:200]:
        if not _owntrash(conn, iid, uid, admin):
            refused += 1
            continue
        conn.execute("UPDATE main.items SET deleted_at=\'\', updated_at=? WHERE id=?", (_now(), iid))
        conn.execute("UPDATE main.attachments SET deleted_at=\'\' WHERE item_id=?", (iid,))
        done += 1
    for aid in aids[:200]:
        row = conn.execute("SELECT item_id FROM main.attachments WHERE id=?", (aid,)).fetchone()
        if row is None or conn.execute("SELECT id FROM main.items WHERE id=? AND COALESCE(deleted_at,\'\')<>\'\'",
                                       (row["item_id"],)).fetchone() is not None:
            refused += 1            # 父记录还在桶里：先恢复记录，附件跟着一起回来
            continue
        conn.execute("UPDATE main.attachments SET deleted_at=\'\' WHERE id=?", (aid,))
        done += 1
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "restored": done, "refused": refused})


@app.route("/api/trash/purge", methods=["POST"])
def api_trash_purge():
    """彻底删除（不可恢复）：删行 + 删还留在磁盘上的上传文件。"""
    d = request.get_json(silent=True) or {}
    ids = [int(x) for x in (d.get("ids") or []) if str(x).strip().isdigit()]
    aids = [int(x) for x in (d.get("att_ids") or []) if str(x).strip().isdigit()]
    conn = get_db()
    uid, admin = current_uid(), is_admin()
    gone = refused = 0
    for iid in ids[:200]:
        if not _owntrash(conn, iid, uid, admin):
            refused += 1
            continue
        _drop_files(conn, conn.execute("SELECT * FROM main.attachments WHERE item_id=?", (iid,)))
        conn.execute("DELETE FROM main.attachments WHERE item_id=?", (iid,))
        conn.execute("DELETE FROM main.items WHERE id=?", (iid,))
        gone += 1
    for aid in aids[:200]:
        row = conn.execute("SELECT * FROM main.attachments WHERE id=?", (aid,)).fetchone()
        if row is None:
            continue
        _drop_files(conn, [row])
        conn.execute("DELETE FROM main.attachments WHERE id=?", (aid,))
        gone += 1
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "purged": gone, "refused": refused})


@app.route("/api/trash/clear", methods=["POST"])
def api_trash_clear():
    """清空垃圾桶（当前用户能看到的这些）。"""
    conn = get_db()
    uid, admin = current_uid(), is_admin()
    w = "" if admin else " AND (owner_id=? OR COALESCE(private,0)=0)"
    p = [] if admin else [uid]
    ids = [r["id"] for r in conn.execute(
        "SELECT id FROM main.items WHERE COALESCE(deleted_at,\'\')<>\'\'" + w, p)]
    for iid in ids:
        _drop_files(conn, conn.execute("SELECT * FROM main.attachments WHERE item_id=?", (iid,)))
        conn.execute("DELETE FROM main.attachments WHERE item_id=?", (iid,))
        conn.execute("DELETE FROM main.items WHERE id=?", (iid,))
    aids = [r["id"] for r in conn.execute(
        "SELECT id FROM main.attachments WHERE COALESCE(deleted_at,\'\')<>\'\'")]
    for aid in aids:
        row = conn.execute("SELECT * FROM main.attachments WHERE id=?", (aid,)).fetchone()
        if row is not None:
            _drop_files(conn, [row])
        conn.execute("DELETE FROM main.attachments WHERE id=?", (aid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "purged": len(ids) + len(aids)})


# ------------------------- 农历 / 提醒 / 导入 API -------------------------

@app.route("/api/calendar-info")
def calendar_info():
    """返回某个月的农历/节日/节气信息
    GET /api/calendar-info?month=2026-10
    """
    month = request.args.get("month")
    if not month:
        return jsonify({"error": "缺少 month 参数"}), 400
    try:
        y, m = month.split("-")
        info = month_calendar_info(int(y), int(m))
    except (ValueError, IndexError):
        return jsonify({"error": "月份格式应为 YYYY-MM"}), 400
    return jsonify({"calendar": info})


@app.route("/api/reminders")
def reminders():
    """返回需要提醒的内容：
    今天到期的日程/生日/纪念日（可设置提前提醒分钟数），以及今天到期未完成的待办
    """
    today = date.today()
    t_iso = today.isoformat()
    t1 = (today + timedelta(days=1)).isoformat()
    far = (today + timedelta(days=800)).isoformat()
    conn = get_db()
    # 原来把「除笔记外的全部条目」都捞出来逐条加工（每条还查一遍附件），
    # 五千条库里这一发就是几百毫秒；现在先用 SQL 按日期圈定候选。
    rows = []
    for sql, args in (
            ("SELECT * FROM items WHERE yearly=1 AND type IN ('birthday','anniversary')", ()),
            ("SELECT * FROM items WHERE type='schedule' AND date BETWEEN ? AND ?", (t_iso, t1)),
            ("SELECT * FROM items WHERE type='countdown' AND date BETWEEN ? AND ?"
             " AND (remind_minutes>0 OR date=?)", (t_iso, far, t_iso)),
            ("SELECT * FROM items WHERE type='todo' AND done=0 AND date<=?", (t_iso,))):
        rows.extend(conn.execute(sql, args).fetchall())
    rows.sort(key=lambda r: r["id"])   # 分类型查回来后按 id 排，保持和原来单条 SQL 一样的顺序
    # 提醒弹层只要标题/类型/剩余天数，不碰附件；folders 表整批查一次就够
    bundle = _Bundle(conn, rows, with_attach=False)
    result, now = [], datetime.now()
    for r in rows:
        d = dict(item_to_dict(r, conn, with_attach=False, bundle=bundle))
        # 每年重复的生日/纪念日：算"今天或未来最近一次"
        if d["yearly"] and d["type"] in ("birthday", "anniversary"):
            occur = next_occurrence(d["date"], today, d["is_lunar"])
            if occur is None:
                continue
            diff = (occur - today).days
            remind_minutes = d["remind_minutes"] or 0
            # 提前提醒：发生在今天，或发生在未来 remind_minutes 分钟内
            if diff == 0:
                result.append({**d, "days_left": 0})
            elif diff == 1 and remind_minutes >= 60 * 24:
                result.append({**d, "days_left": 1})
        elif d["type"] == "schedule":
            dday = parse_date(d["date"])
            if dday is None:
                continue
            diff = (dday - today).days
            remind_minutes = d["remind_minutes"] or 0
            if diff == 0:
                result.append({**d, "days_left": 0})
            elif diff == 1 and remind_minutes >= 60 * 24:
                result.append({**d, "days_left": 1})
        elif d["type"] == "countdown":
            dday = parse_date(d["date"])
            if dday is None:
                continue
            diff = (dday - today).days
            if diff >= 0 and diff <= (d["remind_minutes"] or 0) // (60 * 24):
                result.append({**d, "days_left": diff})
        elif d["type"] == "todo":
            dday = parse_date(d["date"])
            if dday is None:
                continue
            if not d["done"] and dday <= today:
                result.append({**d, "days_left": (dday - today).days})
    conn.close()
    return jsonify({"reminders": result})


def tiptap_plain_text(node, out=None):
    """tiptap JSON 文档 → 纯文本（段落之间空行）"""
    if out is None:
        out = []
    if isinstance(node, list):
        for x in node:
            tiptap_plain_text(x, out)
        return out
    if not isinstance(node, dict):
        return out
    t = node.get("type")
    if t == "text":
        out.append(node.get("text", ""))
    elif t == "hardBreak":
        out.append("\n")
    tiptap_plain_text(node.get("content") or [], out)
    if t in ("paragraph", "heading", "listItem", "codeBlock", "blockquote"):
        out.append("\n")
    return out


def clean_nowen_markdown(text):
    """弄文日记正文（markdown / tiptap 文本）→ 可读纯文本：去掉 ^blk_ 块锚点、
    `---` 分隔线，把 `## 15:48` 这样的时间标题变成行内时间，保留分段。"""
    t = str(text or "")
    t = re.sub(r"[ \t]*\^blk_[0-9a-zA-Z_-]+", "", t)
    lines = []
    for line in t.splitlines():
        s = line.strip()
        if not s or re.fullmatch(r"([-*_])(?:\s*\1){2,}", s):
            continue
        m = re.match(r"#{1,6}\s*(.*)$", s)
        if m:
            s = m.group(1).strip()
            if not s:
                continue
        s = re.sub(r"\*\*|__|~~|`", "", s)
        s = re.sub(r"\[([^\]]*)\]\(([^)]*)\)", lambda m2: m2.group(1).strip() or "", s)
        s = re.sub(r"<[^>]+>", "", s)
        if s.strip():
            lines.append(s.strip())
    return "\n".join(lines)


def nowen_jsonl_to_item(d):
    """弄文笔记自带导出（exports/*.jsonl，字段名跟它的 SQLite 表一致）→ calnote 一条"""
    raw = str(d.get("type") or d.get("note_type") or d.get("noteType") or "note").strip().lower()
    # 弄文的类型：note / task / calendar / attachment（attachment 当笔记存）
    type_key = {"task": "todo", "calendar": "schedule", "attachment": "note", "note": "note"}.get(raw)
    if not type_key:
        type_key = TYPE_ALIAS.get(raw, "note")
    day = str(d.get("date") or d.get("journal_date") or d.get("journalDate") or "").strip()[:10]
    if parse_date(day) is None:
        stamp = str(d.get("createdAt") or d.get("created_at") or "").strip()
        day = stamp[:10]
    if parse_date(day) is None:
        return None
    content = ""
    body = d.get("content")
    if isinstance(body, str) and body.strip().startswith("{"):
        try:
            content = "".join(tiptap_plain_text(json.loads(body)))
        except ValueError:
            content = body
    elif isinstance(body, (dict, list)):
        content = "".join(tiptap_plain_text(body))
    elif isinstance(body, str):
        content = body
    content = content or str(d.get("contentText") or d.get("content_text") or "")
    content = clean_nowen_markdown(content)
    title = str(d.get("title") or "").strip()
    if not title or title == "无标题笔记":
        title = day.replace("-", ".")
    time_str = str(d.get("time") or "").strip()
    if not time_str and type_key in ("schedule", "todo"):
        time_str = str(d.get("startTime") or d.get("start_time") or "")[:5]
    remind = str(d.get("remindAt") or d.get("remind_at") or "").strip()
    if remind and type_key in ("todo", "schedule"):
        rd = remind[:10]
        if parse_date(rd) is not None:
            day = rd
            rt = remind[11:16]
            if re.fullmatch(r"\d{2}:\d{2}", rt):
                time_str = rt
    todos = d.get("todo_items") or d.get("subtasks") or []
    if isinstance(todos, str):
        todos = _json_obj(todos).get("items") or []
    return {
        "type": type_key, "title": title, "content": content, "date": day,
        "time": time_str if re.fullmatch(r"\d{1,2}:\d{2}", time_str) else "",
        "end_time": "", "all_day": 0,
        "location": str(d.get("location") or ""),
        "remind_minutes": int(d.get("remind_minutes") or 0),
        "is_lunar": 1 if str(d.get("calendarType") or "").lower() == "lunar" else 0,
        "yearly": 1 if d.get("repeat") not in (None, "", "none") else 0,
        "done": 1 if d.get("done") or d.get("status") == "done" else 0,
        "tags": str(d.get("tags") or d.get("notebook") or ""),
        "weather": str(d.get("weather") or ""),
        "todo_items": json.dumps(todos, ensure_ascii=False) if todos else "",
    }


def calnote_jsonl_to_item(d):
    """本应用自己的 JSONL 导出（字段名同 items 表）"""
    rec = {k: d.get(k, "") for k in
           ("type", "title", "content", "date", "time", "end_time", "all_day", "location",
            "remind_minutes", "is_lunar", "yearly", "done", "tags", "weather", "todo_items")}
    rec["type"] = TYPE_ALIAS.get(str(rec["type"]).strip(), str(rec["type"]).strip())
    if not rec.get("content"):
        rec["content"] = str(d.get("contentText") or d.get("content_text") or "")
    for b in ("all_day", "is_lunar", "yearly", "done"):
        rec[b] = 1 if str(rec[b]).strip() in ("1", "True", "true", "是") else 0
    rec["remind_minutes"] = int(rec.get("remind_minutes") or 0)
    if not isinstance(rec["todo_items"], str):
        rec["todo_items"] = json.dumps(rec["todo_items"] or [], ensure_ascii=False)
    if rec["type"] not in TYPE_NAMES:
        return None
    if parse_date(str(rec["date"])[:10]) is None:
        return None
    rec["date"] = str(rec["date"])[:10]
    for k in ("title", "content", "time", "end_time", "location", "tags", "weather"):
        rec[k] = str(rec[k] or "")
    return rec


def jsonl_to_item(d):
    """一行 JSON → calnote 一条：自动分辨万年历 / 弄文导出 / 本应用导出"""
    if not isinstance(d, dict):
        return None
    if "lineType" in d:
        return wannianli_to_item(d)
    keys = set(d)
    t = str(d.get("type") or "").strip()
    # 带 items 表那几个布尔字段的（本项目 / tools 生成的 JSONL），按 calnote 结构读，
    # 否则会被当成弄文导出而丢掉 yearly / is_lunar
    if t in TYPE_ALIAS and {"yearly", "is_lunar", "all_day", "end_time"} & keys:
        return calnote_jsonl_to_item(d)
    if "date" in keys and t in TYPE_ALIAS:
        return calnote_jsonl_to_item(d)
    if ({"contentText", "content_text", "journal_date", "journalDate", "createdAt", "created_at"} & keys
            or t in ("note", "task", "calendar", "attachment")):
        return nowen_jsonl_to_item(d)
    return calnote_jsonl_to_item(d)


def _insert_item(conn, rec):
    """写一条 items（rec 为 wannianli_to_item / CSV / JSONL 解析出的字段字典）"""
    if not isinstance(rec.get("todo_items"), str):
        rec = dict(rec, todo_items=json.dumps(rec.get("todo_items") or [], ensure_ascii=False))
    conn.execute(
        """INSERT INTO main.items
           (type, title, content, date, time, end_time, all_day, location,
            remind_minutes, is_lunar, yearly, done, tags, weather, todo_items, owner_id)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec["type"], rec["title"], rec.get("content", ""), rec["date"],
         rec.get("time", ""), rec.get("end_time", ""), rec.get("all_day", 0),
         rec.get("location", ""), int(rec.get("remind_minutes") or 0),
         rec.get("is_lunar", 0), rec.get("yearly", 0), rec.get("done", 0),
         rec.get("tags", ""), rec.get("weather", ""), rec.get("todo_items", ""), current_uid()),
    )


def _dup_exists(conn, rec):
    """同日期 + 同类型 + 同正文首行 视为重复（导入前防手滑重复导入）"""
    sig = (rec.get("content") or rec.get("title") or "").strip().splitlines()[:1]
    row = conn.execute(
        "SELECT id FROM items WHERE date=? AND type=? AND (content LIKE ? OR title=?) LIMIT 1",
        (rec["date"], rec["type"], f"{sig[0] if sig else ''}%", rec.get("title", "")),
    ).fetchone()
    return row is not None


_DUP_PREFIX = 512


class _DupIndex(object):
    """导入查重索引。

    原来每导一行都要跑一次 content LIKE '首行%'，前置通配符用不上索引，
    等于每行扫一遍全库（五千行导入 ≈ 上千万次行比较，越导越慢）。这里一次
    把 (日期,类型) → 已有正文前缀 / 标题 装进内存，判重变成字典查找。

    两点与旧 SQL 的差异（都是往对了的方向改）：正文比对不再被 content 里的
    % _ 当通配符误伤；ASCII 大小写不再被视为同一条（中文正文不受影响）。
    首行超过 _DUP_PREFIX 字符的极少见，这种退回原来的 SQL 判断。
    """
    __slots__ = ("groups",)

    def __init__(self, conn):
        self.groups = {}
        try:
            rows = conn.execute("SELECT date, type, content, title FROM items").fetchall()
        except sqlite3.Error:
            return
        for r in rows:
            self._add(r["date"], r["type"], r["content"] or "", r["title"] or "")

    def _add(self, day, type_, content, title):
        g = self.groups.setdefault((day, type_), (set(), set()))
        g[0].add(content[:_DUP_PREFIX])
        g[1].add(title or "")

    def add_rec(self, rec):
        self._add(rec["date"], rec["type"], rec.get("content") or "", rec.get("title") or "")

    def exists(self, rec, conn):
        sig_lines = (rec.get("content") or rec.get("title") or "").strip().splitlines()[:1]
        sig = sig_lines[0] if sig_lines else ""
        g = self.groups.get((rec["date"], rec["type"]))
        if g is None:
            return False
        if not sig:
            return True                      # 旧 SQL 这时是 LIKE '%'，同日期同类型即算重复
        if len(sig) > _DUP_PREFIX:
            return _dup_exists(conn, rec)
        if rec.get("title") and rec["title"] in g[1]:
            return True
        for pref in g[0]:
            if pref.startswith(sig):
                return True
        return False


@app.route("/api/import", methods=["POST"])
def import_csv():
    """批量导入：自动识别三种文件（都能直接用）
      ① CSV            —— 本应用「导出」的格式，15 列
      ② 弄文笔记 JSONL  —— 表头 type,title,date,time,... 之外的每行 {"type":..}
      ③ 中华万年历 txt  —— 每行一条 {"lineType":..,"title":"<div ...>"}
    重复（同日期同类型同正文）会被跳过，可反复导入。
    """
    f = request.files.get("file")
    if f is not None and f.filename:
        text = f.read().decode("utf-8-sig", errors="replace")
        since = (request.form.get("since") or "").strip()
        until = (request.form.get("until") or "").strip()
        do_dedupe = (request.form.get("dedupe") or "1") != "0"
    else:
        data = request.get_json(silent=True) or {}
        text = data.get("csv", "") or data.get("text", "")
        since = str(data.get("since") or "").strip()
        until = str(data.get("until") or "").strip()
        do_dedupe = data.get("dedupe", True) is not False
    if not text.strip():
        return jsonify({"error": "文件内容为空"}), 400
    for bound in (since, until):
        if bound and parse_date(bound) is None:
            return jsonify({"error": "起止日期格式应为 YYYY-MM-DD"}), 400

    def in_range(rec):
        if since and rec["date"] < since:
            return False
        if until and rec["date"] > until:
            return False
        return True

    lines = [l for l in text.splitlines() if l.strip()]
    is_jsonl = lines[0].lstrip().startswith("{")
    conn = get_db()
    dup_index = _DupIndex(conn) if do_dedupe else None
    ok, fail, skip = 0, 0, 0
    errors = []
    if is_jsonl:
        for i, line in enumerate(lines, start=1):
            try:
                d = json.loads(line)
            except ValueError as e:
                fail += 1
                errors.append(f"第{i}行不是 JSON：{e}")
                continue
            try:
                rec = jsonl_to_item(d)               # 自动分辨三种 JSON 行格式
                if rec is None or not in_range(rec):
                    skip += 1
                    continue
                if do_dedupe and dup_index.exists(rec, conn):
                    skip += 1
                    continue
                _insert_item(conn, rec)
                dup_index.add_rec(rec)
                ok += 1
            except Exception as e:
                fail += 1
                errors.append(f"第{i}行：{e}")
    else:
        try:
            reader = csv.reader(io.StringIO(text))
            header = next(reader, None)
            if header is None:
                return jsonify({"error": "CSV 内容为空"}), 400

            def col(name):
                try:
                    return header.index(name)
                except ValueError:
                    return None

            idx = {k: col(k) for k in
                   ["type", "title", "date", "time", "end_time", "all_day", "content", "tags",
                    "location", "remind_minutes", "is_lunar", "yearly", "done", "weather",
                    "todo_items", "content_text"]}

            def flag(v):
                return 1 if v in ("1", "是", "true", "True") else 0

            for i, row in enumerate(reader, start=2):
                if not row or not any(c.strip() for c in row):
                    continue
                try:
                    def gv(k, default=""):
                        j = idx.get(k)
                        return (row[j] if j is not None and len(row) > j else default).strip()

                    type_raw = gv("type") or "note"
                    type_key = TYPE_ALIAS.get(type_raw)
                    if type_raw in TYPE_NAMES.values():
                        type_key = [k for k, v in TYPE_NAMES.items() if v == type_raw][0]
                    if not type_key:
                        raise ValueError(f"未知类型：{type_raw}")
                    title = gv("title")
                    if not title:
                        raise ValueError("标题为空")
                    day = gv("date")
                    if parse_date(day) is None:
                        raise ValueError(f"日期格式错误：{day}")
                    content = gv("content") or gv("content_text")
                    rec = {"type": type_key, "title": title, "content": content, "date": day,
                           "time": gv("time"), "end_time": gv("end_time"),
                           "all_day": flag(gv("all_day")), "location": gv("location"),
                           "remind_minutes": int(gv("remind_minutes", "0") or 0),
                           "is_lunar": flag(gv("is_lunar")), "yearly": flag(gv("yearly")),
                           "done": flag(gv("done")), "tags": gv("tags"),
                           "weather": gv("weather"), "todo_items": gv("todo_items")}
                    if not in_range(rec):
                        skip += 1
                        continue
                    if do_dedupe and dup_index.exists(rec, conn):
                        skip += 1
                        continue
                    _insert_item(conn, rec)
                    dup_index.add_rec(rec)
                    ok += 1
                except Exception as e:
                    fail += 1
                    errors.append(f"第{i}行：{e}")
        except UnicodeError as e:
            return jsonify({"error": f"文件编码有问题：{e}"}), 400
    conn.commit()
    conn.close()
    return jsonify({"ok": ok, "fail": fail, "skip": skip, "since": since, "until": until,
                    "errors": errors[:20]})


def _json_obj(s):
    """稳妥地把 JSON 字符串转字典（坏数据返回空字典）"""
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except (ValueError, TypeError):
        return {}


def fmt_temp(v):
    """31 / 31℃ / 31° → 31°；空值 → 空串"""
    s = str(v if v is not None else "").strip()
    if not s:
        return ""
    num = re.sub(r"[^\d.\-]", "", s)
    if num in ("", "-", "."):
        return ""
    return num + "°"


def join_weather(city="", weather="", hi="", lo=""):
    """统一天气串：兴安县 多云 11°/6°"""
    parts = [str(x).strip() for x in (city, weather) if str(x or "").strip()]
    temps = [t for t in (fmt_temp(hi), fmt_temp(lo)) if t]
    if temps:
        parts.append("/".join(temps))
    return " ".join(parts)


def wannianli_to_item(d):
    """一条中华万年历 JSON → calnote 字段字典
    lineType：1/8=记事（日记内容 → 笔记）  2=生日  3/5=提醒（日程）  4=清单（待办）
    笔记的标题用日历时间代替，正文放在 content 里。
    """
    lt = int(d.get("lineType", 1))
    sy, sm, sd = int(d.get("syear") or 0), int(d.get("smonth") or 0), int(d.get("sdate") or 0)
    sh, smi = int(d.get("shour") or 0), int(d.get("sminute") or 0)
    if lt == 2:
        type_key = "birthday"
    elif lt == 4:
        type_key = "todo"
    elif lt in (1, 8):
        type_key = "note"
    else:
        type_key = "schedule"
    # 记事正文是富文本：<div suishen-format='limited'><p>一行</p>\n<p>又一行</p></div>
    body = re.sub(r"<\s*/?\s*p\b[^>]*>", "\n", d.get("title") or "", flags=re.I)
    body = re.sub(r"<br\s*/?>", "\n", body, flags=re.I)
    body = re.sub(r"<[^>]+>", "", body)
    body = (body.replace("&nbsp;", " ").replace("&lt;", "<").replace("&gt;", ">")
                .replace("&quot;", '"').replace("&#39;", "'").replace("&amp;", "&"))
    body = "\n".join(l.strip() for l in body.splitlines() if l.strip())
    note = (d.get("note") or "").strip()
    if type_key == "note":
        title = f"{sy:04d}.{sm:02d}.{sd:02d}"
        if sh or smi:
            title += f" {sh:02d}:{smi:02d}"
        content = body
        if note and note != body:
            content = (content + "\n" + note).strip()
        remind, tags = 0, "万年历记事"
    else:
        title = body or note or {"birthday": "生日", "todo": "待办"}.get(type_key, "（无标题）")
        content = note if (note and note != body) else ""
        adv = int(d.get("advance") or 0)              # 提前多少秒提醒（-1=不提醒）
        remind = adv // 60 if adv > 0 else 0
        tags = ""
    if not title.strip():
        title = "（无标题）"
    # 日期：syear=0 的生日按农历处理（用 2000 年代替，标记农历）
    is_lunar = 0
    if sy > 0 and sm > 0 and sd > 0:
        day = f"{sy:04d}-{sm:02d}-{sd:02d}"
    elif lt == 2 and sm > 0 and sd > 0:
        day = f"2000-{sm:02d}-{sd:02d}"
        is_lunar = 1
    else:
        return None  # 无有效日期，跳过
    time_str = f"{sh:02d}:{smi:02d}" if (sh or smi) else ""
    data = _json_obj(d.get("data"))
    weather = join_weather(data.get("city"), data.get("weather"), data.get("temph"), data.get("templ"))
    place = data.get("place") or {}
    location = str(place["address"]) if isinstance(place, dict) and place.get("address") else ""
    all_day = 1 if (data.get("isAllDayTask") or data.get("is_allday")) else 0
    if all_day:
        time_str = ""
    yearly = 1 if type_key == "birthday" and int(d.get("cycle") or 0) == 1 else 0
    done = 1 if data.get("isDone") else 0
    todo_items = json.dumps(data.get("list") or [], ensure_ascii=False) if type_key == "todo" else ""
    return {
        "type": type_key, "title": title, "content": content,
        "date": day, "time": time_str, "end_time": "", "all_day": all_day,
        "location": location, "remind_minutes": remind, "is_lunar": is_lunar,
        "yearly": yearly, "done": done, "tags": tags, "weather": weather,
        "todo_items": todo_items,
    }


@app.route("/api/import_wannianli", methods=["POST"])
def import_wannianli():
    """旧接口保留：/api/import 已能自动识别中华万年历 txt / 弄文 JSONL / CSV"""
    return import_csv()


@app.route("/api/export")
def export_csv():
    """导出全部内容为 CSV（Excel 可直接打开，带模板表头）
    导出的文件也能直接用于导入（格式相同）
    """
    conn = get_db()
    rows = conn.execute("SELECT * FROM items ORDER BY date, time, id").fetchall()
    conn.close()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["type", "title", "date", "time", "end_time", "all_day",
                     "content", "tags", "location", "remind_minutes",
                     "is_lunar", "yearly", "done", "weather", "todo_items"])
    for r in rows:
        writer.writerow([
            TYPE_NAMES.get(r["type"], r["type"]),
            r["title"], r["date"], r["time"], r["end_time"], r["all_day"],
            r["content"], r["tags"], r["location"], r["remind_minutes"],
            r["is_lunar"], r["yearly"], r["done"],
            r["weather"] if "weather" in r.keys() else "",
            r["todo_items"] if "todo_items" in r.keys() else "",
        ])
    # utf-8-sig：带 BOM，Excel 打开中文不乱码
    data = buf.getvalue().encode("utf-8-sig")
    return send_file(
        io.BytesIO(data),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"calnote-导出-{date.today().isoformat()}.csv",
    )


# ------------------------- 启动 -------------------------


# ------------------------- 文件夹（v5.12） -------------------------

@app.route("/api/folders")
def list_folders():
    """所有自建文件夹 + 各自条目数"""
    conn = get_db()
    rows = conn.execute("""SELECT f.id, f.name, f.color,
            (SELECT COUNT(*) FROM items i WHERE i.folder_id=f.id) n
           FROM folders f ORDER BY f.name""").fetchall()
    conn.close()
    return jsonify({"folders": [{"id": r["id"], "name": r["name"], "color": r["color"], "count": r["n"]} for r in rows]})


@app.route("/api/folders", methods=["POST"])
def create_folder():
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:20]
    if not name:
        return jsonify({"error": "文件夹名字不能为空"}), 400
    conn = get_db()
    if conn.execute("SELECT id FROM folders WHERE name=?", (name,)).fetchone():
        conn.close()
        return jsonify({"error": "已经有这个文件夹了"}), 400
    cur = conn.execute("INSERT INTO folders (name, color, owner_id) VALUES (?,?,?)",
                       (name, str(data.get("color", ""))[:20], current_uid()))
    conn.commit()
    row = conn.execute("SELECT * FROM folders WHERE id=?", (cur.lastrowid,)).fetchone()
    conn.close()
    return jsonify({"folder": {"id": row["id"], "name": row["name"], "color": row["color"], "count": 0}}), 201


@app.route("/api/folders/<int:fid>", methods=["PUT"])
def rename_folder(fid):
    data = request.get_json(silent=True) or {}
    name = str(data.get("name", "")).strip()[:20]
    if not name:
        return jsonify({"error": "名字不能为空"}), 400
    conn = get_db()
    row = conn.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "文件夹不存在"}), 404
    if row["owner_id"] != current_uid() and not is_admin():
        conn.close()
        return jsonify({"error": "只有建夹的人或管理员能改名"}), 403
    conn.execute("UPDATE folders SET name=? WHERE id=?", (name, fid))
    conn.commit()
    conn.close()
    return jsonify({"ok": 1})


@app.route("/api/folders/<int:fid>", methods=["DELETE"])
def delete_folder(fid):
    """删文件夹：里面的条目不删，只是变成未归类"""
    conn = get_db()
    row = conn.execute("SELECT * FROM folders WHERE id=?", (fid,)).fetchone()
    if row is not None and row["owner_id"] != current_uid() and not is_admin():
        conn.close()
        return jsonify({"error": "只有建夹的人或管理员能删"}), 403
    # 只把本人可见的条目移出该夹，别人放到这个夹里的内容不动
    conn.execute("UPDATE main.items SET folder_id=0 WHERE folder_id=?" +
                 VIS.format(u=current_uid()), (fid,))
    conn.execute("DELETE FROM folders WHERE id=?", (fid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": 1})


@app.route("/api/items/<int:item_id>/meta", methods=["PUT"])
def set_item_meta(item_id):
    """单独更新文件夹归属 / 出生年（编辑时改这两样不用整条提交）"""
    data = request.get_json(silent=True) or {}
    conn = get_db()
    row = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
    if row is None:
        conn.close()
        return jsonify({"error": "内容不存在"}), 404
    if "folder_id" in data:
        conn.execute("UPDATE main.items SET folder_id=? WHERE id=?" + VIS.format(u=current_uid()),
                     (int(data.get("folder_id") or 0), item_id))
    if "birth_year" in data:
        conn.execute("UPDATE main.items SET birth_year=? WHERE id=?" + VIS.format(u=current_uid()),
                     (int(data.get("birth_year") or 0), item_id))
    conn.execute("UPDATE main.items SET updated_at=datetime('now','localtime') WHERE id=?" +
                 VIS.format(u=current_uid()), (item_id,))
    conn.commit()
    conn.close()
    return jsonify({"ok": 1})


# ------------------------- 记账模块挂载（v9 第 2 期） -------------------------
_CTX = {"get_db": get_db, "current_uid": current_uid, "is_admin": is_admin, "auth_on": auth_on,
        "err": lambda msg, code=400: (jsonify({"error": msg}), code)}
LEDGER = ledger.register(app, _CTX)
VAULT = vault.register(app, _CTX)


# 启动时顺手清桶：超过 TRASH_KEEP_DAYS 天的删除内容才真删（含上传文件）
try:
    _c0 = get_db()
    purge_trash(_c0)
    _c0.close()
except Exception:
    pass


if __name__ == "__main__":  # 启动时顺手清桶
    init_db()
    print(f"日历便签已启动：http://0.0.0.0:{PORT}  （数据文件：{DB_PATH}）")
    app.run(host="0.0.0.0", port=PORT, debug=False)
