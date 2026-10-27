#!/usr/bin/env bash
# 第一次把项目推到你的 GitHub 仓库。用法（在项目目录里）：
#   bash scripts/push-to-github.sh            # 用默认地址
#   REPO=https://github.com/你的用户名/仓库.git bash scripts/push-to-github.sh
set -e
REPO="${REPO:-https://github.com/liangyz123/calnote.git}"
cd "$(dirname "$0")/.."

if [ -d .git ]; then
  echo "已有 .git，继续"
else
  git init -b main
fi
git config user.name  "${GIT_NAME:-liangyz123}"
git config user.email "${GIT_EMAIL:-liangyz123@users.noreply.github.com}"

# 先把可能被误加的垃圾清出暂存区（.gitignore 之后新生成的也不会有）
git rm -r --cached --ignore-unmatch data '__pycache__' '*.pyc' '*.db' '*.log' nohup.out >/dev/null 2>&1 || true

git add -A
if ! git diff --cached --quiet; then
  git commit -m "日历便签 v10.5：仓库规范化（.gitignore / docs / 自检 / CI / fpk 工程）"
fi
git remote remove origin 2>/dev/null || true
git remote add origin "$REPO"
echo
echo "接下来会推送。如果弹出要密码：GitHub 已不用账号密码，需要用「访问令牌 PAT」当密码。"
echo "  · 用网页上传（不需要本地 git）："
echo "      打开 https://github.com/liangyz123/calnote  →  点 “uploading an existing file”  →  把本目录里的文件拖进去"
echo "  · 或用令牌推送：GitHub → Settings → Developer settings → Personal access tokens → Fine-grained"
echo "      勾选 Contents: Read and Write，生成后当密码用（用户名填你的 GitHub 账号）"
echo
git push -u origin main --force
echo "推送完成。"
