#!/usr/bin/env bash
# 安装 workbuddy-checkin 到用户级 skills 目录
#
# 用法:
#   bash install.sh                                  # 默认装 full（完整版 1.4.1，开箱即用）
#   bash install.sh ~/.workbuddy/skills split        # 装拆分版：主包 + bridge
#   EDITION=split bash install.sh                    # 也可用环境变量
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_ROOT="${1:-$HOME/.workbuddy/skills}"
EDITION="${2:-${EDITION:-full}}"
BACKUP_ROOT="$(dirname "$DEST_ROOT")/skill-backups"

if [[ "$EDITION" != "full" && "$EDITION" != "split" ]]; then
  echo "错误：Edition 只能是 full 或 split（当前：$EDITION）" >&2
  exit 1
fi

mkdir -p "$DEST_ROOT"

install_pack() {
  local src="$SRC_DIR/$1" name="$2" dest="$DEST_ROOT/$2"
  if [ ! -f "$src/SKILL.md" ]; then
    echo "错误：找不到 $src/SKILL.md，请在仓库根目录运行。" >&2
    exit 1
  fi
  if [ -d "$dest" ]; then
    # 备份到 skills 目录之外，避免残留目录被当成损坏的 skill 加载
    mkdir -p "$BACKUP_ROOT"
    local backup="$BACKUP_ROOT/$name.$(date +%Y%m%d%H%M%S)"
    echo "已存在旧版本，备份到：$backup"
    mv "$dest" "$backup"
  fi
  cp -r "$src" "$dest"
  echo "已安装：$name -> $dest"
}

PY=""
for c in python3 python py; do
  if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done

# Git Bash / MSYS 下 $HOME 形如 /c/Users/x，直接传给原生 python.exe 会被
# 拼成 C:\c\Users\x 而找不到文件，需先转成 Windows 路径
winpath() {
  if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi
}

if [ "$EDITION" = "full" ]; then
  install_pack "legacy/workbuddy-checkin-full" "workbuddy-checkin"
  echo
  echo "已安装【完整版 1.4.1】：自动读取本机客户端凭据，开箱即用。"
  if [ -n "$PY" ]; then
    echo "--- 自检（只查状态，不领取）---"
    if ! "$PY" "$(winpath "$DEST_ROOT/workbuddy-checkin/scripts/wb-checkin.py")" --status; then
      echo "（自检未通过，请确认已登录过 WorkBuddy 客户端）"
    fi
  else
    echo "未找到 python，跳过自检。"
  fi
else
  install_pack "workbuddy-checkin" "workbuddy-checkin"
  install_pack "workbuddy-checkin-bridge" "workbuddy-checkin-bridge"
  echo
  echo "已安装【拆分版】：主包只接受显式 token，bridge 需显式同意才会运行。"
  echo "  1) 生成凭据： python \"$DEST_ROOT/workbuddy-checkin-bridge/scripts/bridge.py\" --confirm --out /secure/token.json"
  echo "  2) 执行签到： WB_TOKEN_FILE=/secure/token.json python \"$DEST_ROOT/workbuddy-checkin/scripts/wb-checkin.py\""
  echo "（主包需要显式凭据，跳过签到自检）"
  if [ -n "$PY" ]; then
    echo "--- 语法自检 ---"
    if "$PY" -m py_compile \
        "$(winpath "$DEST_ROOT/workbuddy-checkin/scripts/wb-checkin.py")" \
        "$(winpath "$DEST_ROOT/workbuddy-checkin/scripts/wb-checkin-guard.py")" \
        "$(winpath "$DEST_ROOT/workbuddy-checkin/scripts/token_provider.py")"; then
      echo "语法校验通过"
    fi
  fi
fi

echo
echo "完成。"
