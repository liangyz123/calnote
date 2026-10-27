#!/usr/bin/env bash
# 重新打包飞牛桌面图标用的 .fpk（改端口/图标后用这个）
# 用法：bash scripts/make-fpk.sh      →  产出 dist/calnote.fpk
set -e
cd "$(dirname "$0")/.."
mkdir -p .tool dist
if [ ! -x .tool/fnpack ]; then
  echo "下载官方打包工具 fnpack（首次需要联网）…"
  curl -fsSL -o .tool/fnpack https://static2.fnnas.com/fnpack/fnpack-1.2.3-linux-amd64
  chmod +x .tool/fnpack
fi
find fpk -name ".DS_Store" -delete 2>/dev/null || true
find fpk -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
.tool/fnpack build -d fpk/calnote >/dev/null
mv -f calnote.fpk dist/calnote.fpk 2>/dev/null || mv -f fpk/calnote/../calnote.fpk dist/calnote.fpk
echo "已生成 dist/calnote.fpk —— 飞牛「应用中心 →（右上角）设置 → 手动安装」选它。"
echo "安装前先把旧的 calnote 应用卸载掉，否则桌面图标还缓存着老地址。"
