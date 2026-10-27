#!/bin/sh
# ============================================================
# 日历便签 · 一键更新脚本（飞牛 NAS / 通用 docker compose）
#
# 用法（把本文件和 calnote 代码目录放在一起，SSH 里执行）：
#     sh update.sh                      # 用当前目录的代码重建并重启
#     sh update.sh calnote-v6.6.zip     # 先解压这个新包覆盖代码，再重建重启
#     sh update.sh --repo               # 顺便拉一次新镜像基础（--pull，慢但干净）
#
# 它做四件事：① 备份数据库 → ② （可选）解压新包覆盖代码 →
#            ③ 重新构建镜像并起容器 → ④ 健康检查，失败就回滚
# 全程不会动 data/attachments、data/import 里的任何东西。
# ============================================================
set -e
cd "$(dirname "$0")"

HERE=$(pwd)
DATA="$HERE/data"
TS=$(date +%Y%m%d-%H%M)
KEEP=10                       # 数据库备份最多留几份
ZIP=""
PULL=""

for arg in "$@"; do
  case "$arg" in
    *.zip) ZIP="$arg" ;;
    --repo|--pull) PULL="--pull" ;;
  esac
done

say() { printf '\033[1;36m==\033[0m %s\n' "$1"; }
bad() { printf '\033[1;31m!!\033[0m %s\n' "$1"; }

# ---------- ① 备份数据库 ----------
[ -d "$DATA" ] || mkdir -p "$DATA"
if [ -f "$DATA/calnote.db" ]; then
  # v9：数据库开了 WAL，最新那批事务可能还在 -wal 里没并回主文件。
  # 直接 cp 主文件会得到一份「少了最近几分钟」的备份，真回滚就会丢笔记。
  # 优先让 SQLite 自己出在线快照；都不行才退回拷贝，并把 -wal 一起备份。
  BK="$DATA/calnote.db.bak-$TS"
  if docker compose version >/dev/null 2>&1; then DCX="docker compose"; else DCX="docker-compose"; fi
  if command -v sqlite3 >/dev/null 2>&1 && sqlite3 "$DATA/calnote.db" "VACUUM INTO '$BK'" >/dev/null 2>&1; then
    say "已备份数据库（SQLite 在线快照，含未合并事务）：data/calnote.db.bak-$TS"
  elif $DCX exec -T calnote python -c "import sqlite3;c=sqlite3.connect('/app/data/calnote.db');c.execute('VACUUM INTO \"/app/data/calnote.db.bak-$TS\"');c.close()" >/dev/null 2>&1; then
    say "已备份数据库（容器内在线快照）：data/calnote.db.bak-$TS"
  else
    cp "$DATA/calnote.db" "$BK"
    if [ -f "$DATA/calnote.db-wal" ]; then
      cp "$DATA/calnote.db-wal" "$BK-wal"
      say "⚠ 本机没有 sqlite3、容器也没在跑，已连带 -wal 一起备份；回滚时两份要一起放回"
    fi
    say "已备份数据库：data/calnote.db.bak-$TS"
  fi
  ls -1t "$DATA"/calnote.db.bak-* 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r f; do rm -f "$f"; done
fi

# ---------- ② 解压新包（只覆盖代码，绝不碰 data/）----------
if [ -n "$ZIP" ]; then
  [ -f "$ZIP" ] || { bad "找不到安装包 $ZIP"; exit 1; }
  tmp="$HERE/.upd-$TS"
  mkdir -p "$tmp"
  if command -v unzip >/dev/null 2>&1; then
    unzip -oq "$ZIP" -d "$tmp"
  else
    python3 -c "import zipfile,sys; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$ZIP" "$tmp"
  fi
  src="$tmp/calnote"
  [ -d "$src" ] || src=$(find "$tmp" -maxdepth 2 -name "app.py" -exec dirname {} \; | head -1)
  [ -d "$src" ] || { bad "包里没找到 calnote 目录"; rm -rf "$tmp"; exit 1; }
  say "覆盖代码：app.py / static / tools / Dockerfile / docker-compose.yml / *.md"
  cp -f "$src/app.py" "$HERE/" 2>/dev/null || true
  cp -rf "$src/static" "$HERE/" 2>/dev/null || true
  cp -rf "$src/tools" "$HERE/" 2>/dev/null || true
  for f in Dockerfile docker-compose.yml requirements.txt; do
    [ -f "$src/$f" ] && cp -f "$src/$f" "$HERE/$f"
  done
  for f in "$src"/*.md; do [ -f "$f" ] && cp -f "$f" "$HERE/"; done
  rm -rf "$tmp"
fi

# ---------- ③ 重建 + 起容器 ----------
if docker compose version >/dev/null 2>&1; then DC="docker compose"; else DC="docker-compose"; fi
OLD_IMG=$(docker images --format '{{.Repository}}:{{.Tag}}' | grep -m1 '^calnote:' || true)
say "构建镜像（第一次有 Pillow 会慢一点，之后走缓存很快）"
$DC build $PULL
say "重启容器"
$DC up -d

# ---------- ④ 健康检查（最多等 40 秒），不行就回滚 ----------
LOCAL=$(grep -oE '[0-9]{2,5}:[0-9]{2,5}' docker-compose.yml | head -1 | cut -d: -f1)
[ -n "$LOCAL" ] || LOCAL=8000
say "检查 http://127.0.0.1:$LOCAL/api/health"
i=0
while [ $i -lt 20 ]; do
  if command -v curl >/dev/null 2>&1; then
    HOVER=$(curl -s -o /dev/null -w '%{http_code}' --max-time 4 "http://127.0.0.1:$LOCAL/api/health" || echo 000)
  else
    HOVER=$(python3 -c "import urllib.request,sys
try:
    print(urllib.request.urlopen(sys.argv[1],timeout=4).status)
except Exception:
    print('000')" "http://127.0.0.1:$LOCAL/api/health")
  fi
  [ "$HOVER" = "200" ] && break
  i=$((i + 1)); sleep 2
done

if [ "$HOVER" = "200" ]; then
  say "更新完成 ✔  现在打开 http://你NAS的IP:$LOCAL 就行（手机请重开一次便签，PWA 会自动换新样式）"
  exit 0
fi

bad "健康检查没过（HTTP $HOVER）。最近 25 行日志："
$DC logs --tail=25 || true
if [ -n "$OLD_IMG" ]; then
  say "回滚到旧镜像 $OLD_IMG"
  docker tag "$OLD_IMG" calnote:latest
  $DC up -d --force-recreate || true
fi
bad "如果还在报错，把上面的日志拍给我。数据库在 data/calnote.db.bak-$TS，没被动过。"
exit 1
