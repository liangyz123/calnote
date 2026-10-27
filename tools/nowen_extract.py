#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""nowen_extract —— 读取「弄文笔记」SQLite，把日记拆成一条条记录。

弄文日记一天一条笔记，正文是 markdown：
    ## 15:48 ^blk_xxx
    早上参加党建活动…… ^blk_xxx
    --- ^blk_xxx
    ## 18:16 ^blk_xxx
    腻子粉3.5吨电叉到货卸货……

每个 `## HH:MM` 段落 = 一条记录（date=笔记日期，hour/minute=标题时间）。
没有时间标题的段落：先从正文里找 HH:MM，再找不到用兜底时间。
"""
import json
import re
import sqlite3
from datetime import datetime

BLK_RE = re.compile(r"[ \t]*\^blk_[0-9a-zA-Z_-]+")
HDR_RE = re.compile(r"(?m)^(#{1,6})[ \t]*(.*?)[ \t]*$")
HR_RE = re.compile(r"(?m)^[ \t]*([-*_])(?:[ \t]*\1){2,}[ \t]*$")
TIME_HDR_RE = re.compile(r"\A(\d{1,2})[:：](\d{2})\s*\Z")
TIME_IN_TEXT_RE = re.compile(r"(?:凌晨|早上|上午|中午|下午|傍晚|晚上|夜里)?\s*"
                            r"([01]?\d|2[0-3])[:：]([0-5]\d)(?![-–]\d)")
MD_LINK_RE = re.compile(r"!?\[([^\]]*)\]\(([^)]*)\)")
ATTACH_LINE_RE = re.compile(r"^\s*(?:📎|🖼|📷|附件)\s*$|^\s*📎\s")


# ---------------- 读取 ----------------
def load_notes(db_path, include_normal=False):
    """返回 [ {id,title,journal_date,content,contentText,createdAt,contentFormat,...} ]"""
    con = sqlite3.connect("file:%s?mode=ro&immutable=1" % db_path, uri=True)
    con.row_factory = sqlite3.Row
    sql = ("select id, title, journal_date, content, contentText, createdAt, note_type, "
           "contentFormat, notebookId, "
           "(select name from notebooks b where b.id=n.notebookId) as nbName "
           "from notes n where isTrashed=0 and (isArchived is null or isArchived=0)")
    if not include_normal:
        sql += " and note_type='journal'"
    rows = [dict(r) for r in con.execute(sql)]
    con.close()
    return rows


# ---------------- 正文 → 纯文本 ----------------
def strip_anchors(text):
    return BLK_RE.sub("", text or "")


def markdown_to_plain(text):
    out = []
    for raw in (text or "").splitlines():
        line = raw.replace("\r", " ")
        if ATTACH_LINE_RE.match(line):
            continue
        line = strip_anchors(line)
        line = re.sub(r"^\s*#{1,6}\s+", "", line)
        line = re.sub(r"^\s*>\s?", "", line)
        line = re.sub(r"^\s*[-*+·•]\s+\[[ xX]\]\s*", "", line)
        line = re.sub(r"^\s*[-*+·•]\s+", "", line)
        line = re.sub(r"^\s*\d+[.)]\s+", "", line)
        line = re.sub(r"^\s*\|?\s*:?-{2,}.*$", "", line)
        line = MD_LINK_RE.sub(lambda m: m.group(1).strip(), line)
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"\*\*|__|~~|`|\*|_", "", line)
        line = re.sub(r"\s*\|\s*", "  ", line).strip(" |")
        line = re.sub(r"[ \t]{2,}", " ", line).strip()
        if line:
            out.append(line)
    return "\n".join(out)


def tiptap_to_text(node):
    parts = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
            return
        if not isinstance(n, dict):
            return
        t = n.get("type")
        if t == "text":
            parts.append(n.get("text", ""))
            return
        if t == "hardBreak":
            parts.append("\n")
            return
        if t == "image":
            return
        walk(n.get("content") or [])
        if t in ("paragraph", "heading", "listItem", "blockquote", "codeBlock"):
            parts.append("\n")

    walk(node)
    lines = [re.sub(r"\s+", " ", l).strip() for l in "".join(parts).splitlines()]
    return "\n".join(l for l in lines if l)


def render_content(fmt, content, content_text):
    body = (content or "").strip()
    if fmt == "tiptap-json":
        try:
            text = tiptap_to_text(json.loads(body))
        except (ValueError, TypeError):
            text = ""
        return text.strip() or (content_text or "").strip()
    if fmt and fmt != "markdown":
        return (content_text or "").strip() or body
    return body


# ---------------- 拆条 ----------------
def split_entries(content):
    blocks, cur = [], [None, []]
    for line in (content or "").splitlines():
        m = HDR_RE.match(line)
        hm = None
        if m:
            t = TIME_HDR_RE.match(m.group(2).strip())
            if t:
                hm = (min(int(t.group(1)), 23), min(int(t.group(2)), 59))
        if hm is not None:
            if "\n".join(cur[1]).strip():
                blocks.append((cur[0], "\n".join(cur[1])))
            cur = [hm, []]
            continue
        if HR_RE.match(line):
            continue
        cur[1].append(line)
    if "\n".join(cur[1]).strip():
        blocks.append((cur[0], "\n".join(cur[1])))
    return blocks


def time_from_text(text):
    for line in text.splitlines():
        m = TIME_IN_TEXT_RE.search(line)
        if m:
            return int(m.group(1)), int(m.group(2))
    return None


def parse_dt(value):
    try:
        return datetime.fromisoformat(str(value).strip().rstrip("Z"))
    except (ValueError, TypeError):
        return None


def entries_from_note(note, default_time=None):
    """一条弄文笔记 → [{date:(y,m,d), hour, minute, text, note_id}]"""
    day = (note.get("journal_date") or "").strip()[:10]
    created = parse_dt(note.get("createdAt"))
    try:
        d = datetime.strptime(day, "%Y-%m-%d").date()
    except ValueError:
        d = created.date() if created else None
    if d is None:
        return []
    fallback = default_time or ((created.hour, created.minute) if created else (12, 0))

    body = render_content(note.get("contentFormat"), note.get("content"), note.get("contentText"))
    prepared = strip_anchors(body)
    out = []
    for hm, chunk in split_entries(prepared):
        plain = markdown_to_plain(HDR_RE.sub("", chunk))
        if not plain:
            continue
        if hm is not None:
            hh, mi, src = hm[0], hm[1], "标题"
        else:
            inline = time_from_text(plain)
            if inline:
                hh, mi, src = inline[0], inline[1], "正文"
            else:
                hh, mi, src = fallback[0], fallback[1], "补"
        out.append({"date": (d.year, d.month, d.day), "hour": hh, "minute": mi,
                    "time_src": src, "text": plain, "note_id": note.get("id")})
    return out
