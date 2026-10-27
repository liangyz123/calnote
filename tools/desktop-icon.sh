#!/usr/bin/env bash
# ============================================================
# 给 calnote（日历便签）在桌面做一个图标，点开直接进应用
# 适用：飞牛 fnOS 底层装的 Cinnamon（肉桂）/ Debian 桌面，或任何
#       带 XDG 桌面的 Linux；浏览器会优先用 chromium / google-chrome
#       的 --app 模式（没地址栏，看起来就像原生 App）。
#
# 用法：
#   bash tools/desktop-icon.sh                     # 用本机 8000 端口
#   bash tools/desktop-icon.sh http://192.168.1.10:8000
# ============================================================
set -euo pipefail

URL="${1:-http://127.0.0.1:8000}"
NAME="日历便签"
HOST=$(printf '%s' "$URL" | sed -E 's#^[a-z]+://##; s#[:/].*$##')
PORT=$(printf '%s' "$URL" | sed -nE 's#^[a-z]+://[^/:]+:([0-9]+).*#\1#p'); PORT="${PORT:-80}"

# 1) 找浏览器（优先支持 --app 模式的）
BROWSER=""
for b in google-chrome google-chrome-stable chromium chromium-browser microsoft-edge brave-browser vivaldi-stable firefox; do
  if command -v "$b" >/dev/null 2>&1; then BROWSER="$b"; break; fi
done
if [ -z "$BROWSER" ]; then
  echo "没找到浏览器，先装一个 chromium 或 firefox"; exit 1
fi

# 2) 图标：优先从应用里下载自带图标，失败就用系统默认
ICON_DIR="$HOME/.local/share/icons"
ICON="$ICON_DIR/calnote.png"
mkdir -p "$ICON_DIR"
if command -v curl >/dev/null 2>&1; then
  curl -fsSL --max-time 8 "$URL/icon-512.png" -o "$ICON" 2>/dev/null || \
  curl -fsSL --max-time 8 "$URL/icon-192.png" -o "$ICON" 2>/dev/null || \
  rm -f "$ICON"
fi
[ -f "$ICON" ] || ICON="application-x-executable"

# 3) 桌面目录（中文系统叫“桌面”）
DESK="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
[ -d "$DESK" ] || DESK="$HOME/Desktop"
[ -d "$DESK" ] || mkdir -p "$DESK"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"

# 4) 生成 .desktop
case "$BROWSER" in
  firefox) EXEC="firefox --kiosk $URL" ;;        # firefox 没有 --app，用 kiosk 近似
  *)       EXEC="$BROWSER --class=calnote --app=$URL" ;;
esac

DESKTOP_FILE="$DESK/$NAME.desktop"
echo "[Desktop Entry]
Version=1.0
Type=Application
Name=$NAME
Comment=日历便签（$HOST:$PORT）
Exec=$EXEC
Icon=$ICON
Terminal=false
Categories=Office;Utility;
StartupWMClass=calnote" | tee "$APPS/calnote.desktop" > "$DESKTOP_FILE"

chmod +x "$DESKTOP_FILE" 2>/dev/null || true
command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$APPS" || true

echo
echo "已生成图标："
echo "  桌面   $DESKTOP_FILE"
echo "  应用菜单 $APPS/calnote.desktop"
echo "浏览器：$BROWSER   地址：$URL"
case "$BROWSER" in
  firefox) echo "提示：Firefox 用 --kiosk 打开（无地址栏），关掉窗口即退出。" ;;
  *) echo "提示：用 --app 模式打开，没有地址栏和标签，像独立应用；可再右键任务栏固定。" ;;
esac
echo "若双击提示“不受信任”，在桌面上右键该图标 → 允许运行/允许启动即可。"
