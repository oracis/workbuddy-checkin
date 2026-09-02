#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日签到领积分 —— 幂等、零依赖（仅标准库）

直接调用 WorkBuddy(CodeBuddy) 后端签到接口，无需打开客户端 UI。
凭据从客户端本地的明文会话文件中读取，不做任何网络登录。

用法:
  python wb-checkin.py            正常签到（幂等：今日已领则跳过）
  python wb-checkin.py --status   只查询状态，不领取
  python wb-checkin.py --json     以 JSON 输出状态（便于脚本消费）
  python wb-checkin.py --force    强制调用领取接口（调试用）

退出码:
  0  成功领取 / 今日已领 / 活动未开启
  1  失败（鉴权失效、凭据缺失、接口异常）

环境变量:
  WB_AUTH_DIR   覆盖凭据目录（自动探测失败时使用）
  WB_ENDPOINT   覆盖后端地址（默认 https://copilot.tencent.com）
"""
import os
import re
import sys
import json
import glob
import platform
import argparse
import urllib.request
import urllib.error

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"

# 客户端共享数据目录名（源码常量 EXTENSION_DATA_DIR_NAME）
# 注意：是 CodeBuddyExtension，不是 WorkBuddyExtension
DATA_DIR_NAME = "CodeBuddyExtension"
AUTH_SUBPATH = os.path.join(DATA_DIR_NAME, "Data", "Public", "auth")

# 历史备份文件名形如 workbuddy-desktop-dev.2026-07-07T06-34-08-857Z.info
# 用正则匹配日期戳，避免把年份写死
BACKUP_RE = re.compile(r"\.\d{4}-\d{2}-\d{2}T[\d-]+Z\.info$", re.IGNORECASE)

SENSITIVE_HINTS = ("token", "secret", "password", "credential")


def log(*a):
    print(*a, flush=True)


def auth_dir_candidates():
    """返回可能的凭据目录列表，按优先级排序。"""
    override = os.environ.get("WB_AUTH_DIR")
    if override:
        return [override]

    out = []
    system = platform.system()
    home = os.path.expanduser("~")

    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
        out.append(os.path.join(local, AUTH_SUBPATH))
    elif system == "Darwin":
        # 未在 macOS 上实测，属候选路径
        out.append(os.path.join(home, "Library", "Application Support", AUTH_SUBPATH))
        out.append(os.path.join(home, ".local", "share", AUTH_SUBPATH))
    else:
        # 未在 Linux 上实测，属候选路径
        xdg = os.environ.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
        out.append(os.path.join(xdg, AUTH_SUBPATH))

    # 兜底：部分版本可能放在用户主目录
    out.append(os.path.join(home, AUTH_SUBPATH))
    return out


def load_cred():
    """从最新修改的会话文件中读取凭据。"""
    tried = []
    for d in auth_dir_candidates():
        tried.append(d)
        if not os.path.isdir(d):
            continue
        files = glob.glob(os.path.join(d, "*.info"))
        # 优先用当前会话文件（排除带日期戳的历史备份）
        current = [f for f in files if not BACKUP_RE.search(os.path.basename(f))]
        pool = current or files
        if not pool:
            continue
        pool.sort(key=lambda f: os.path.getmtime(f), reverse=True)
        for f in pool:
            try:
                with open(f, encoding="utf-8") as fh:
                    data = json.load(fh)
            except Exception:
                continue
            auth = data.get("auth") or {}
            acct = data.get("account") or {}
            token = auth.get("accessToken")
            if not token:
                continue
            return {
                "file": f,
                "token": token,
                "uid": acct.get("uid") or auth.get("uid"),
                "domain": auth.get("domain"),
            }

    raise FileNotFoundError(
        "未找到可用的凭据文件。已尝试以下目录：\n  "
        + "\n  ".join(tried)
        + "\n请确认已登录 WorkBuddy 客户端，或用环境变量 WB_AUTH_DIR 指定凭据目录。"
    )


def call(path, cred, endpoint):
    url = endpoint.rstrip("/") + path
    body = json.dumps({}).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", "Bearer " + cred["token"])
    if cred.get("uid"):
        req.add_header("X-User-Id", str(cred["uid"]))
    if cred.get("domain"):
        req.add_header("X-Domain", str(cred["domain"]))
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": raw[:500]}
    except urllib.error.URLError as e:
        return 0, {"raw": "网络不可达: %s" % e.reason}


def ok(status, payload):
    return status == 200 and (not isinstance(payload, dict) or payload.get("code") in (0, None))


def fmt_progress(wp):
    if not isinstance(wp, list):
        return ""
    return "".join("\u25cf" if x else "\u25cb" for x in wp)


def disp_width(s):
    """East Asian 宽字符按 2 列计算，用于终端对齐。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in str(s))


def pad(s, width):
    return str(s) + " " * max(0, width - disp_width(s))


def render(d):
    name = d.get("activity_name") or d.get("theme_name") or "未知活动"
    theme = d.get("theme_name")
    title = name if not theme or theme == name else "%s（%s）" % (name, theme)
    rows = [
        ("活动", title),
        ("期数", "第 %s 期" % d.get("season") if d.get("season") is not None else None),
        ("时间范围", "%s ~ %s" % (d.get("start_time"), d.get("end_time"))
            if d.get("start_time") else None),
        ("已签到天数", "%s（本周 %s 天）" % (d.get("streak_days"), d.get("week_checkin_days"))),
        ("累计积分", d.get("total_credits")),
        ("今日积分", d.get("today_credit")),
        ("本周进度", fmt_progress(d.get("week_progress")) or None),
    ]
    for k, v in rows:
        if v not in (None, ""):
            log("%s: %s" % (pad(k, 12), v))


def auth_failed(status):
    return status in (401, 403)


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 每日签到领积分")
    ap.add_argument("--status", action="store_true", help="只查询状态，不领取")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出状态")
    ap.add_argument("--force", action="store_true", help="强制调用领取接口（调试）")
    args = ap.parse_args()

    endpoint = os.environ.get("WB_ENDPOINT") or DEFAULT_ENDPOINT

    try:
        cred = load_cred()
    except Exception as e:
        log("[FAIL] 凭据加载失败：%s" % e)
        return 1

    status, payload = call(STATUS_PATH, cred, endpoint)
    if not ok(status, payload):
        if auth_failed(status):
            log("[FAIL] 鉴权失败（HTTP %s），accessToken 可能已过期。" % status)
            log("       请打开一次 WorkBuddy 客户端让它刷新登录态，之后会自动恢复。")
            return 1
        log("[FAIL] 查询状态失败 HTTP %s：%s" % (status, str(payload)[:300]))
        return 1

    data = (payload or {}).get("data") or {}

    if args.json:
        log(json.dumps(data, ensure_ascii=False, indent=2))
        return 0

    active = data.get("active", True)
    checked = data.get("today_checked_in", False)

    if args.status:
        render(data)
        log("[INFO] 仅查询模式，未执行领取。")
        return 0

    if checked and not args.force:
        render(data)
        log("[SKIP] 今天已经领过了，无需重复领取（今日 +%s 积分）" % data.get("today_credit"))
        return 0

    if not active and not args.force:
        render(data)
        log("[SKIP] 当前没有进行中的签到活动。")
        return 0

    status2, payload2 = call(CLAIM_PATH, cred, endpoint)
    if not ok(status2, payload2):
        if auth_failed(status2):
            log("[FAIL] 领取失败，鉴权失效（HTTP %s）。" % status2)
            log("       请打开一次 WorkBuddy 客户端刷新登录态。")
            return 1
        log("[FAIL] 领取接口异常 HTTP %s：%s" % (status2, str(payload2)[:300]))
        return 1

    # 复查一次，用服务端权威状态汇报
    status3, payload3 = call(STATUS_PATH, cred, endpoint)
    if ok(status3, payload3):
        data = (payload3 or {}).get("data") or data

    render(data)
    if data.get("today_checked_in"):
        log("[OK] 领取成功！本次 +%s 积分，累计 %s。"
            % (data.get("today_credit"), data.get("total_credits")))
        return 0

    log("[FAIL] 接口返回成功但状态仍为未签到，请检查活动规则或稍后重试。")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
