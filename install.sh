#!/usr/bin/env bash
# 安装 workbuddy-checkin Skill 到用户级 skills 目录
# 用法: bash install.sh [目标 skills 目录]
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILL_NAME="workbuddy-checkin"
DEST_ROOT="${1:-$HOME/.workbuddy/skills}"
DEST="$DEST_ROOT/$SKILL_NAME"

if [ ! -f "$SRC_DIR/$SKILL_NAME/SKILL.md" ]; then
  echo "错误：在 $SRC_DIR/$SKILL_NAME 下找不到 SKILL.md，请在仓库根目录运行。" >&2
  exit 1
fi

mkdir -p "$DEST_ROOT"

if [ -d "$DEST" ]; then
  # 备份到 skills 目录之外，避免残留目录被当成损坏的 skill 加载
  BACKUP_ROOT="$(dirname "$DEST_ROOT")/skill-backups"
  mkdir -p "$BACKUP_ROOT"
  BACKUP="$BACKUP_ROOT/$SKILL_NAME.$(date +%Y%m%d%H%M%S)"
  echo "已存在旧版本，备份到：$BACKUP"
  mv "$DEST" "$BACKUP"
fi

cp -r "$SRC_DIR/$SKILL_NAME" "$DEST_ROOT/"
echo "已安装到：$DEST"

# 找一个可用的 python
PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

if [ -n "$PY" ]; then
  SCRIPT="$DEST/scripts/wb-checkin.py"
  # Git Bash / MSYS 下 $HOME 形如 /c/Users/x，直接传给原生 python.exe 会被
  # 拼成 C:\c\Users\x 而找不到文件，需先转成 Windows 路径
  if command -v cygpath >/dev/null 2>&1; then
    SCRIPT="$(cygpath -w "$SCRIPT")"
  fi
  echo
  echo "--- 自检（查询签到状态，不领取）---"
  if ! "$PY" "$SCRIPT" --status; then
    echo "（自检未通过，请确认已登录 WorkBuddy 客户端）"
  fi
else
  echo "未找到 python，跳过自检。请确认已安装 Python 3.8+。"
fi

echo
echo "完成。现在可以对 WorkBuddy 说「帮我领积分」。"
