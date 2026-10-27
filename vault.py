# -*- coding: utf-8 -*-
"""
日历便签 v9 · 第 3 期：密码本（保险柜）
==========================================
每条内容的每个字段都单独用 AES-256-GCM 加密，密钥由「主口令 + 每用户随机盐」经
scrypt 派生。主口令不入库、不进 compose、不进代码，服务器上没有任何人能凭库文件解出内容。

一条硬约束：库文件 + 主口令才是完整备份。
    - 主口令忘了 = 永久打不开，没有任何找回通道（服务器确实没存）
    - 只拷走 calnote.db 没有主口令，等于拷走一把锁没钥匙
    - 只记得主口令但 data/ 丢了，密码本全没

为什么在主口令之外还留一把「共享密钥」：家庭场景里 WiFi 密码、视频会员这类要全家共用，
而密文是用各自主口令派生的密钥加的，别人没有你的主口令就解不开。所以共享条目改用服务器
上的 vault_share.key 加密 —— 代价是共享条目的机密性只依赖服务器文件安全，不依赖主口令。
因此默认所有条目都是私有，共享必须显式勾选。非共享条目：管理员也解不开。
"""

import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import time

# scrypt 参数：N=2^15 时 OpenSSL 默认内存上限会拒绝，必须显式传 maxmem（实测 32MB + 余量）
KDF_N = int(os.environ.get("VAULT_KDF_N", "32768"))
KDF_R = int(os.environ.get("VAULT_KDF_R", "8"))
KDF_P = int(os.environ.get("VAULT_KDF_P", "1"))
KDF_MAXMEM = 128 * KDF_N * KDF_R + (32 << 20)
VAULT_SESSION_MIN = max(1, int(os.environ.get("VAULT_SESSION_MIN", "30")))   # 解锁后保持多久
VAULT_FAIL_LIMIT = int(os.environ.get("VAULT_FAIL_LIMIT", "8"))              # 连续错几次就锁
VAULT_LOCK_SEC = int(os.environ.get("VAULT_LOCK_SEC", "300"))
SHARE_FILE = os.path.join(os.path.dirname(os.environ.get("DB_PATH", "data/calnote.db")), "vault_share.key")

try:
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    _HAS_CRYPTO = True
except Exception:                                       # 没装 cryptography 时整个模块拒绝工作
    _HAS_CRYPTO = False

_key_cache = {}                                          # uid -> (key_bytes, 过期时间)
_cache_lock = threading.Lock()
_fail_state = {}                                         # uid -> (错误次数, 锁定到期)
FIELDS = ("title", "url", "user", "pass", "note")


def register(app, ctx):
    get_db = ctx["get_db"]
    current_uid = ctx["current_uid"]
    is_admin = ctx.get("is_admin", lambda: False)
    _err = ctx.get("err")

    def err(msg, code=400):
        from flask import jsonify
        if _err:
            return _err(msg, code)
        return jsonify({"error": msg}), code

    if not _HAS_CRYPTO:
        app.logger.warning("未安装 cryptography，密码本功能关闭（pip 装 cryptography 后重建镜像）")

    # ------------------------- 建表 -------------------------
    DDL = """
    CREATE TABLE IF NOT EXISTS vault_kdf (
        user_id    INTEGER PRIMARY KEY,
        salt       TEXT NOT NULL,
        verify     TEXT NOT NULL,
        kdf_n      INTEGER DEFAULT 32768,
        kdf_r      INTEGER DEFAULT 8,
        kdf_p      INTEGER DEFAULT 1,
        updated_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS vault_item (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        ref        TEXT NOT NULL,
        user_id    INTEGER NOT NULL,
        title_enc  TEXT DEFAULT '',
        url_enc    TEXT DEFAULT '',
        user_enc   TEXT DEFAULT '',
        pass_enc   TEXT DEFAULT '',
        note_enc   TEXT DEFAULT '',
        shared     INTEGER DEFAULT 0,
        favorite   INTEGER DEFAULT 0,
        created_at TEXT DEFAULT (datetime('now','localtime')),
        updated_at TEXT DEFAULT (datetime('now','localtime')));
    CREATE TABLE IF NOT EXISTS vault_view_log (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        item_id   INTEGER NOT NULL,
        owner_id  INTEGER NOT NULL,
        viewer_id INTEGER NOT NULL,
        at        TEXT DEFAULT (datetime('now','localtime')));
    """
    INDEXES = (
        "CREATE INDEX IF NOT EXISTS idx_vault_item_user ON vault_item(user_id, shared)",
        "CREATE INDEX IF NOT EXISTS idx_vault_ref ON vault_item(ref)",
        "CREATE INDEX IF NOT EXISTS idx_vault_log_item ON vault_view_log(item_id)",
    )

    def init_vault_db(conn):
        if not _HAS_CRYPTO:
            return
        conn.executescript(DDL)
        for sql in INDEXES:
            try:
                conn.execute(sql)
            except sqlite3.Error:
                pass

    # ------------------------- 密码学 -------------------------
    def _b64e(b):
        return base64.b64encode(b).decode()

    def _b64d(s):
        return base64.b64decode(str(s or "")) if s else b""

    def derive_key(master, salt_b64):
        dk = hashlib.scrypt(str(master).encode("utf-8"), salt=_b64d(salt_b64),
                            n=KDF_N, r=KDF_R, p=KDF_P, dklen=32, maxmem=KDF_MAXMEM)
        return dk

    def key_verify(key):
        return _b64e(hmac.new(key, b"calnote-vault-verify-v1", hashlib.sha256).digest())

    def _aad(ref):
        return ("v1|%s" % ref).encode()

    def enc(key, plain, ref):
        """每字段独立加密：nonce(12B) + AES-GCM 密文，base64 存库。aad 绑定该行 ref，
        把某一行的密文复制到另一行会直接解不开。"""
        if plain is None:
            return ""
        nonce = secrets.token_bytes(12)
        ct = AESGCM(key).encrypt(nonce, str(plain).encode("utf-8"), _aad(ref))
        return _b64e(nonce + ct)

    def dec(key, blob, ref):
        if not blob:
            return ""
        raw = _b64d(blob)
        if len(raw) < 13:
            return ""
        try:
            return AESGCM(key).decrypt(raw[:12], raw[12:], _aad(ref)).decode("utf-8", "replace")
        except Exception:
            return None                                   # 解不开＝密钥不对或密文被动过

    def _share_key():
        """共享条目用的服务器密钥：随机生成一次存 data/vault_share.key（0600）。"""
        try:
            with open(SHARE_FILE, "rb") as f:
                v = f.read().strip()
            if len(v) >= 32:
                return v[:32]
        except OSError:
            pass
        v = secrets.token_bytes(32)
        try:
            os.makedirs(os.path.dirname(SHARE_FILE), exist_ok=True)
            fd = os.open(SHARE_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(v)
        except OSError:
            pass
        return v

    # ------------------------- 会话密钥缓存 -------------------------
    def _cached_key(uid, sid):
        if not sid:
            return None
        with _cache_lock:
            hit = _key_cache.get((uid, sid))
            if not hit:
                return None
            if hit[1] < time.time():
                _key_cache.pop(uid, None)
                return None
            return hit[0]

    def _remember_key(uid, sid, key):
        with _cache_lock:
            _key_cache[(uid, sid)] = (key, time.time() + VAULT_SESSION_MIN * 60)

    def _forget_key(uid, sid=None):
        with _cache_lock:
            if sid:
                _key_cache.pop((uid, sid), None)
            else:                                   # 改口令、锁定：这个账号的所有会话一起上锁
                for k in [x for x in _key_cache if x[0] == uid]:
                    _key_cache.pop(k, None)

    def _sid(req):
        """解锁状态按「账号 + 浏览器会话」记，不是只按账号。
        否则手机解锁一次，电脑上的同一账号也就跟着通了。"""
        v = str(req.headers.get("X-Vault-Sid") or req.args.get("sid") or "")
        return v if re.fullmatch(r"[0-9a-f]{16,64}", v) else ""

    def _locked_out(uid):
        st = _fail_state.get(uid)
        return bool(st and st[1] > time.time())

    def _note_fail(uid):
        n, _ = _fail_state.get(uid) or (0, 0)
        n += 1
        _fail_state[uid] = (n, time.time() + VAULT_LOCK_SEC if n >= VAULT_FAIL_LIMIT else 0)
        if n >= VAULT_FAIL_LIMIT:
            _fail_state[uid] = (n, time.time() + VAULT_LOCK_SEC)
            return "主口令连续错 %d 次，锁定 %d 分钟" % (VAULT_FAIL_LIMIT, VAULT_LOCK_SEC // 60)
        return "还剩 %d 次尝试" % (VAULT_FAIL_LIMIT - n)

    def _kdf_row(conn, uid):
        try:
            return conn.execute("SELECT * FROM vault_kdf WHERE user_id=?", (uid,)).fetchone()
        except sqlite3.Error:
            return None

    # ------------------------- 接口 -------------------------
    @app.route("/api/vault/status")
    def vault_status():
        from flask import jsonify, request
        if not _HAS_CRYPTO:
            return jsonify({"error": "没装 cryptography，密码本不可用", "available": 0}), 503
        conn = get_db()
        row = _kdf_row(conn, current_uid())
        conn.close()
        return jsonify({"ok": 1, "available": 1, "setup": bool(row),
                        "unlocked": bool(_cached_key(current_uid(), _sid(request))),
                        "keep_min": VAULT_SESSION_MIN})

    @app.route("/api/vault/setup", methods=["POST"])
    def vault_setup():
        """首次使用：设主口令。已有主口令时改走 /api/vault/master，避免误覆盖。"""
        from flask import jsonify, request
        if not _HAS_CRYPTO:
            return err("没装 cryptography 库，密码本不可用（重建镜像时会自动装）", 503)
        data = request.get_json(silent=True) or {}
        master = str(data.get("master") or "")
        if len(master) < 8:
            return err("主口令至少 8 位，这个是开所有密码的钥匙")
        conn = get_db()
        uid = current_uid()
        if _kdf_row(conn, uid) is not None:
            conn.close()
            return err("主口令已经设过了，要改请用「修改主口令」")
        salt = _b64e(secrets.token_bytes(16))
        key = derive_key(master, salt)
        conn.execute("INSERT INTO vault_kdf (user_id,salt,verify,kdf_n,kdf_r,kdf_p) VALUES (?,?,?,?,?,?)",
                    (uid, salt, key_verify(key), KDF_N, KDF_R, KDF_P))
        conn.commit()
        conn.close()
        sid = secrets.token_hex(16)
        _remember_key(uid, sid, key)
        return jsonify({"ok": 1, "unlocked": 1, "sid": sid}), 201

    @app.route("/api/vault/unlock", methods=["POST"])
    def vault_unlock():
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        master = str(data.get("master") or "")
        conn = get_db()
        uid = current_uid()
        row = _kdf_row(conn, uid)
        conn.close()
        if row is None:
            return err("还没设主口令", 400)
        if _locked_out(uid):
            return err("解锁被暂时锁定，等几分钟再来", 429)
        key = derive_key(master, row["salt"])
        if not hmac.compare_digest(key_verify(key), row["verify"]):
            tip = _note_fail(uid)
            return err("主口令不对（%s）" % tip, 403)
        _fail_state.pop(uid, None)
        sid = secrets.token_hex(16)
        _remember_key(uid, sid, key)
        return jsonify({"ok": 1, "unlocked": 1, "sid": sid, "keep_min": VAULT_SESSION_MIN})

    @app.route("/api/vault/lock", methods=["POST"])
    def vault_lock():
        from flask import jsonify, request
        _forget_key(current_uid(), _sid(request))
        return jsonify({"ok": 1, "unlocked": 0})

    @app.route("/api/vault/master", methods=["POST"])
    def vault_change_master():
        """换主口令：旧口令解出全部条目 → 用新口令的密钥重新加密 → 换盐和验证值。"""
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        old, new = str(data.get("old") or ""), str(data.get("new") or "")
        if len(new) < 8:
            return err("新主口令至少 8 位")
        conn = get_db()
        uid = current_uid()
        row = _kdf_row(conn, uid)
        if row is None:
            conn.close()
            return err("还没设主口令")
        if _locked_out(uid):
            conn.close()
            return err("操作被暂时锁定，等几分钟再来", 429)
        key_old = derive_key(old, row["salt"])
        if not hmac.compare_digest(key_verify(key_old), row["verify"]):
            tip = _note_fail(uid)
            conn.close()
            return err("当前主口令不对（%s）" % tip, 403)
        salt_new = _b64e(secrets.token_bytes(16))
        key_new = derive_key(new, salt_new)
        _forget_key(uid)                                  # 换口令＝这个账号所有会话都要重新解锁
        rows = conn.execute("SELECT * FROM vault_item WHERE user_id=?", (uid,)).fetchall()
        moved = 0
        for r in rows:
            # 共享条目用的是服务器共享密钥，和主口令无关：跳过不动。
            # 早先写成「全部用旧主口令解一遍再重加密」，一撞上共享条目就全体中止、口令永远改不掉。
            if r["shared"]:
                continue
            plain = {}
            for f in FIELDS:
                p = dec(key_old, r["%s_enc" % f], r["ref"])
                if p is None:
                    conn.rollback()
                    conn.close()
                    return err("有条目解不开，为防丢数据已中止，主口令没改", 500)
                plain[f] = p
            for f in FIELDS:
                conn.execute("UPDATE vault_item SET %s_enc=?, updated_at=datetime('now','localtime') WHERE id=?"
                             % f, (enc(key_new, plain[f], r["ref"]), r["id"]))
            moved += 1
        conn.execute("INSERT OR REPLACE INTO vault_kdf (user_id,salt,verify,kdf_n,kdf_r,kdf_p,updated_at)"
                     " VALUES (?,?,?,?,?,?,datetime('now','localtime'))",
                     (uid, salt_new, key_verify(key_new), KDF_N, KDF_R, KDF_P))
        conn.commit()
        conn.close()
        sid = secrets.token_hex(16)
        _remember_key(uid, sid, key_new)
        return jsonify({"ok": 1, "re_encrypted": moved, "sid": sid})

    def _reveal_allowed(conn, r, uid):
        """能否看这条：自己的 / 已共享的（共享条目走服务器密钥，任何已解锁成员可看）"""
        return (r["user_id"] == uid) or bool(r["shared"])

    def _key_for(conn, r, uid, sid):
        """先按「是否共享」选密钥，再谈归属：共享条目入库时用的就是服务器共享密钥，
        本人创建的那部分也一样 —— 早先按「本人优先用主口令密钥」写，结果自己建的
        共享条目反而解不开，列表里显示成损坏。"""
        if r["shared"]:
            if _cached_key(uid, sid) is None:
                return None, "not_unlocked"                 # 未解锁仍然一律不给看
            return _share_key(), "share"
        if r["user_id"] == uid:
            k = _cached_key(uid, sid)
            return (k, "own") if k else (None, "not_unlocked")
        return None, "forbidden"

    @app.route("/api/vault/items")
    def vault_list():
        from flask import jsonify
        from flask import request as _req
        uid = current_uid()
        if _cached_key(uid, _sid(_req)) is None:
            return jsonify({"locked": 1, "error": "先输入主口令解锁"}), 423
        conn = get_db()
        rows = conn.execute("SELECT * FROM vault_item WHERE user_id=? OR shared=1 ORDER BY favorite DESC, updated_at DESC",
                            (uid,)).fetchall()
        out = []
        for r in rows:
            key, mode = _key_for(conn, r, uid, _sid(_req))
            if key is None:
                continue                                 # 别人的私有条目直接不出现在列表里
            title = dec(key, r["title_enc"], r["ref"]) or ""
            url = dec(key, r["url_enc"], r["ref"]) or ""
            user = dec(key, r["user_enc"], r["ref"]) or ""
            if title is None or url is None:             # 解不开＝密文被动过，标出来而不是静默显示空
                out.append({"id": r["id"], "corrupt": 1, "title": "（解不开：密文或密钥有问题）",
                            "shared": bool(r["shared"]), "mine": r["user_id"] == uid})
                continue
            out.append({"id": r["id"], "title": title, "url": url, "user": user,
                        "has_pass": bool(r["pass_enc"]), "shared": bool(r["shared"]),
                        "favorite": bool(r["favorite"]), "mine": r["user_id"] == uid,
                        "via": mode, "updated_at": r["updated_at"]})
        conn.close()
        return jsonify({"items": out, "locked": 0})       # 注意：这里永远没有密码字段

    @app.route("/api/vault/items/<int:iid>/reveal", methods=["POST"])
    def vault_reveal(iid):
        """看密码要走这一条独立请求：列表里绝不携带密码，响应带 no-store 不进任何缓存。"""
        from flask import jsonify, request
        conn = get_db()
        uid = current_uid()
        r = conn.execute("SELECT * FROM vault_item WHERE id=?", (iid,)).fetchone()
        if r is None or not _reveal_allowed(conn, r, uid):
            conn.close()
            return err("条目不存在", 404)
        key, mode = _key_for(conn, r, uid, _sid(request))
        if key is None:
            conn.close()
            return err("这条是别人的私有密码，看不了" if mode == "forbidden" else "请先解锁", 403)
        pwd = dec(key, r["pass_enc"], r["ref"])
        if pwd is None:
            conn.close()
            return err("解不开这条（密文或密钥有问题）", 500)
        try:                                             # 共享条目留痕：谁在什么时候看过
            if r["user_id"] != uid:
                conn.execute("INSERT INTO vault_view_log (item_id,owner_id,viewer_id) VALUES (?,?,?)",
                             (iid, r["user_id"], uid))
                conn.commit()
        except sqlite3.Error:
            pass
        conn.close()
        resp = jsonify({"password": pwd, "via": mode})
        resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.route("/api/vault/items", methods=["POST"])
    def vault_add():
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        uid = current_uid()
        key = _cached_key(uid, _sid(request))
        if key is None:
            return err("先解锁", 423)
        title = str(data.get("title") or "").strip()
        if not title:
            return err("至少给个名字（网站或用途）")
        shared = 1 if data.get("shared") else 0
        ref = secrets.token_hex(8)
        ck = key if not shared else _share_key()          # 共享条目改由服务器密钥加密
        vals = {f: enc(ck, str(data.get(f) if data.get(f) is not None else ""), ref) for f in FIELDS}
        conn = get_db()
        conn.execute("INSERT INTO vault_item (ref,user_id,title_enc,url_enc,user_enc,pass_enc,note_enc,shared,favorite)"
                     " VALUES (?,?,?,?,?,?,?,?,?)",
                    (ref, uid, vals["title"], vals["url"], vals["user"], vals["pass"], vals["note"],
                     shared, 1 if data.get("favorite") else 0))
        conn.commit()
        iid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.close()
        return jsonify({"item": {"id": iid, "title": title, "shared": shared}}), 201

    @app.route("/api/vault/items/<int:iid>", methods=["PUT"])
    def vault_edit(iid):
        from flask import jsonify, request
        data = request.get_json(silent=True) or {}
        conn = get_db()
        uid = current_uid()
        r = conn.execute("SELECT * FROM vault_item WHERE id=?", (iid,)).fetchone()
        if r is None or r["user_id"] != uid:
            conn.close()
            return err("只能改自己的条目", 404)
        key = _cached_key(uid, _sid(request))
        if key is None:
            conn.close()
            return err("先解锁", 423)
        shared = bool(data["shared"]) if "shared" in data else bool(r["shared"])
        if shared != bool(r["shared"]):                   # 共享状态一变就要换密钥重加密全部字段
            old_ck = _share_key() if r["shared"] else key
            new_ck = _share_key() if shared else key
            for f in FIELDS:
                p = dec(old_ck, r["%s_enc" % f], r["ref"])
                if p is None:
                    conn.close()
                    return err("这条解不开，改共享会丢数据，先别动", 500)
                conn.execute("UPDATE vault_item SET %s_enc=? WHERE id=?" % f, (enc(new_ck, p, r["ref"]), iid))
            conn.execute("UPDATE vault_item SET shared=? WHERE id=?", (1 if shared else 0, iid))
        for f in FIELDS:
            if f in data:
                ck = _share_key() if shared else key
                conn.execute("UPDATE vault_item SET %s_enc=? WHERE id=?" % f,
                             (enc(ck, str(data.get(f) or ""), r["ref"]), iid))
        if "favorite" in data:
            conn.execute("UPDATE vault_item SET favorite=? WHERE id=?", (1 if data.get("favorite") else 0, iid))
        conn.execute("UPDATE vault_item SET updated_at=datetime('now','localtime') WHERE id=?", (iid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": 1})

    @app.route("/api/vault/items/<int:iid>", methods=["DELETE"])
    def vault_del(iid):
        from flask import jsonify
        conn = get_db()
        uid = current_uid()
        r = conn.execute("SELECT * FROM vault_item WHERE id=?", (iid,)).fetchone()
        if r is None or r["user_id"] != uid:
            conn.close()
            return err("只能删自己的条目", 404)
        conn.execute("DELETE FROM vault_item WHERE id=?", (iid,))
        conn.execute("DELETE FROM vault_view_log WHERE item_id=?", (iid,))
        conn.commit()
        conn.close()
        return jsonify({"ok": 1})

    @app.route("/api/vault/logs")
    def vault_logs():
        """管理员看全部留痕，普通成员只看到「谁看了我共享出去的条目」。"""
        from flask import jsonify
        conn = get_db()
        uid = current_uid()
        try:
            rows = conn.execute(
                "SELECT l.item_id, l.viewer_id, l.at, v.title_enc, v.ref, v.user_id AS owner,"
                " cu.display_name AS viewer_name, ou.display_name AS owner_name"
                " FROM vault_view_log l JOIN vault_item v ON v.id=l.item_id"
                " LEFT JOIN users cu ON cu.id=l.viewer_id LEFT JOIN users ou ON ou.id=v.user_id"
                " ORDER BY l.id DESC LIMIT 200").fetchall()
        except sqlite3.Error:
            rows = []
        out = []
        for r in rows:
            if r["owner"] != uid and not is_admin():     # 管理员能看全局留痕，成员只看谁看了自己的
                continue
            out.append({"item": r["item_id"], "viewer": r["viewer_name"] or "?",
                        "at": r["at"], "shared": True})
        conn.close()
        return jsonify({"logs": out})

    ctx["init_vault_db"] = init_vault_db if _HAS_CRYPTO else (lambda conn: None)
    ctx["vault_available"] = _HAS_CRYPTO
    return ctx
