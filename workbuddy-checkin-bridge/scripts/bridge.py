#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""workbuddy-checkin-bridge：把本机 WorkBuddy 登录态转成标准 JSON 凭据。

这个组件**不会被自动调用**。用户必须显式运行它（并显式同意），
主签到包才会拿到 token。它和签到包刻意分开：签到包因此不需要具备
「读取/解密客户端凭据」的能力。

用法:
  python bridge.py --confirm --out C:/path/to/token.json
  python bridge.py --confirm                      # 直接把一行 JSON 打到 stdout
  WB_BRIDGE_CONSENT=1 python bridge.py --out ...  # 无人值守时用环境变量同意

输出形状（stdout 最后一行，或 --out 文件内容）:
  {"accessToken":"...","refreshToken":"...","uid":"...","domain":"..."}

安全约定:
  * 只读凭据文件，绝不写回、不修改客户端登录态；
  * 解密用的 build key 优先来自环境变量 / 用户已有的缓存文件；
    --allow-live-extract 走进程提取时 secret **只留在内存**，不落盘；
  * 绝不把 token 或 secret 打印到终端、写进日志；
  * --out 由用户显式指定；写完后尽量收紧为仅当前用户可读。
"""
import argparse
import glob
import json
import os
import platform
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import at_rest  # noqa: E402

DATA_DIR_NAME = "CodeBuddyExtension"
AUTH_SUBPATH = os.path.join(DATA_DIR_NAME, "Data", "Public", "auth")
BACKUP_RE = re.compile(r"\.\d{4}-\d{2}-\d{2}T[\d-]+Z\.info$", re.IGNORECASE)
JWT_RE = re.compile(r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")


class BridgeError(Exception):
    pass


def auth_dirs():
    override = os.environ.get("WB_AUTH_DIR")
    if override:
        return [override]
    home = os.path.expanduser("~")
    if platform.system() == "Windows":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        return [os.path.join(local, AUTH_SUBPATH)]
    if platform.system() == "Darwin":
        return [os.path.join(home, "Library", "Application Support", AUTH_SUBPATH)]
    xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return [os.path.join(xdg, AUTH_SUBPATH)]


def candidate_files():
    """只列出用户自己机器上的会话文件；当前会话优先于历史备份。"""
    out = []
    for d in auth_dirs():
        if not os.path.isdir(d):
            continue
        for f in glob.glob(os.path.join(d, "*.info")):
            out.append(f)
    out.sort(key=lambda p: (
        1 if BACKUP_RE.search(os.path.basename(p)) else 0,
        -os.path.getmtime(p),
    ))
    return out


def resolve_secret(allow_live_extract):
    """返回 (secret, 来源说明)。绝不打印 secret 本身。"""
    env = os.environ.get("WB_AT_REST_SECRET_KEY")
    if env:
        return env, "WB_AT_REST_SECRET_KEY"
    try:
        return at_rest.load_build_key(), "本地已有缓存文件"
    except Exception:
        pass
    if not allow_live_extract:
        raise BridgeError(
            "缺少 build key。请设置 WB_AT_REST_SECRET_KEY，或加 --allow-live-extract "
            "让本组件从运行中的客户端取到内存 secret（不会写盘）。")
    try:
        import buildkey_bootstrap
    except Exception as exc:
        raise BridgeError("实时提取模块不可用：%s" % exc)
    ok, info = buildkey_bootstrap.bootstrap()
    if not ok:
        raise BridgeError("实时提取失败：%s（%s）"
                          % (info.get("reason"), info.get("hint") or ""))
    return info["secret"], "运行中的客户端（仅内存，未落盘）"


def build_cred(allow_live_extract):
    files = candidate_files()
    if not files:
        raise BridgeError("未找到会话文件。已查找：%s" % ", ".join(auth_dirs()))

    secret = None
    secret_src = None
    errors = []
    for path in files:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        auth = data.get("auth") or {}
        acct = data.get("account") or {}
        if not auth.get("accessToken"):
            continue
        if secret is None:
            try:
                secret, secret_src = resolve_secret(allow_live_extract)
            except BridgeError as exc:
                raise exc
        try:
            token = at_rest.unwrap(auth["accessToken"], secret)
            refresh = None
            if auth.get("refreshToken"):
                try:
                    refresh = at_rest.unwrap(auth["refreshToken"], secret)
                except Exception:
                    refresh = None
        except Exception as exc:
            errors.append("%s: %s" % (os.path.basename(path), exc))
            continue
        if not isinstance(token, str) or not JWT_RE.match(token):
            errors.append("%s: 解出的不是 JWT" % os.path.basename(path))
            continue
        cred = {"accessToken": token}
        if refresh:
            cred["refreshToken"] = refresh
        uid = acct.get("uid") or auth.get("uid")
        if uid:
            cred["uid"] = str(uid)
        if auth.get("domain"):
            cred["domain"] = auth["domain"]
        return cred, secret_src

    raise BridgeError("没有可用的会话凭据。%s" % (
        ("跳过原因：" + "; ".join(errors[:5])) if errors else ""))


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 登录态桥接（需显式同意，输出 JSON 凭据文件）")
    ap.add_argument("--confirm", action="store_true", help="确认本组件可以读取本机登录态")
    ap.add_argument("--out", help="把 JSON 写到这个文件（不指定则输出到 stdout）")
    ap.add_argument("--allow-live-extract", action="store_true",
                     help="允许从运行中的客户端进程取 build key（secret 只在内存，不落盘）")
    args = ap.parse_args()

    if not (args.confirm or os.environ.get("WB_BRIDGE_CONSENT") == "1"):
        print("[REFUSED] 本组件会读取并解密本机 WorkBuddy 登录态。", file=sys.stderr)
        print("          如确认使用，请加 --confirm 或设置 WB_BRIDGE_CONSENT=1。", file=sys.stderr)
        return 2

    try:
        cred, secret_src = build_cred(args.allow_live_extract)
    except BridgeError as exc:
        print("[FAIL] %s" % exc, file=sys.stderr)
        return 1

    payload = json.dumps(cred, ensure_ascii=False, separators=(",", ":"))
    if args.out:
        try:
            with open(args.out, "w", encoding="utf-8") as fh:
                fh.write(payload + "\n")
            try:
                os.chmod(args.out, 0o600)
            except Exception:
                pass
        except Exception as exc:
            print("[FAIL] 写出失败：%s" % exc, file=sys.stderr)
            return 1
        print("[OK] 凭据已写出：%s（字段：%s；build key 来源：%s）"
              % (args.out, ", ".join(sorted(cred.keys())), secret_src))
    else:
        # 只在用户没指定 --out 时把 JSON 打到 stdout（供管道消费）
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
