#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""WorkBuddy 每日签到领积分 —— 幂等、零依赖（仅 Python 标准库）。

能力边界（刻意保持最小）：
  * 只做一件事：用「用户显式提供的 accessToken」调用 WorkBuddy 官方签到接口；
  * 不扫描/不解密本机客户端凭据文件，不读取客户端进程内存；
  * 不写入任何凭据文件，不修改客户端登录态；
  * 只向固定白名单域名发 HTTPS 请求，且禁止重定向；
  * token 续期后的新凭据只在本次进程内使用，不落盘。

登录态输入见同目录 token_provider.py（两种显式入口：环境变量 / 指定 JSON 文件）。
本包不含任何凭据解密组件；需要读取本机客户端会话的用户请另行安装
workbuddy-checkin-bridge（独立包，由用户自行决定是否运行）。

用法:
  python wb-checkin.py            正常签到（幂等：今日已领则跳过）
  python wb-checkin.py --status   只查询状态，不领取
  python wb-checkin.py --json     以 JSON 输出状态
  python wb-checkin.py --force    强制调领取接口（调试用）

退出码:
  0  成功领取 / 今日已领 / 活动未开启
  1  失败（凭据缺失、鉴权失效、接口异常）
"""
import os
import re
import sys
import json
import time
import base64
import argparse
import urllib.parse
import urllib.request
import urllib.error

_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import token_provider  # noqa: E402  （只读的显式凭据输入层）

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"
REFRESH_PATH = "/v2/plugin/auth/token/refresh"

# 仅允许 WorkBuddy 官方域名。token 绝不会被发到其它主机。
ALLOWED_HOSTS = frozenset({"copilot.tencent.com", "www.workbuddy.cn"})

_JWT_IN_TEXT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


def log(*a):
    print(*a, flush=True)


def redact(text):
    """日志/错误信息脱敏：绝不把 JWT 打到终端。"""
    return _JWT_IN_TEXT.sub("***REDACTED***", str(text))


# ---------------------------------------------------------------- 出口安全
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """禁止一切自动重定向。

    原因：请求头里带 Bearer token，若服务端 302 到别的域名，
    urllib 默认会把请求（含 header）跟着发过去，等于把 token 交给第三方。
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(
            req.full_url, code,
            "redirect blocked: token must not follow cross-origin redirects",
            headers, fp,
        )


_OPENER = urllib.request.build_opener(_NoRedirect)


def resolve_endpoint():
    """把 endpoint 收敛成 https://白名单主机，拒绝一切可疑形态。"""
    raw = (os.environ.get("WB_ENDPOINT") or DEFAULT_ENDPOINT).strip()
    if "://" not in raw:
        raw = "https://" + raw
    p = urllib.parse.urlparse(raw)
    if p.scheme != "https":
        raise ValueError("endpoint 必须是 https，拒绝明文传输 token：%s" % redact(raw))
    if p.username or p.password:
        raise ValueError("endpoint 不允许内嵌用户名/密码")
    if p.path not in ("", "/") or p.query or p.fragment:
        raise ValueError("endpoint 只允许纯主机，不允许路径/查询/片段")
    # 白名单是硬约束，没有任何环境变量可以放行到其它主机。
    # WB_ENDPOINT 只允许在两个官方域名之间切换，不是「自定义后端」开关。
    # 同时拒绝非标准端口，避免凭据被发到本机代理或别的中转。
    host = (p.hostname or "").lower()
    if host not in ALLOWED_HOSTS:
        raise ValueError(
            "endpoint 主机 %r 不在白名单 %s 内；白名单为硬约束，不可通过环境变量放行"
            % (host, sorted(ALLOWED_HOSTS)))
    if p.port not in (None, 443):
        raise ValueError("endpoint 只允许默认的 443 端口")
    return "https://" + host


def _post(url, headers, expected_host):
    """发送一个 POST：固定 https + 白名单 host + 禁重定向 + 校验最终 URL。"""
    if urllib.parse.urlparse(url).scheme != "https":
        return 0, {"raw": "refused: non-https url"}
    req = urllib.request.Request(url, data=b"{}", method="POST")
    for key, val in headers.items():
        req.add_header(key, str(val))
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    try:
        with _OPENER.open(req, timeout=20) as r:
            final_host = (urllib.parse.urlparse(r.geturl()).hostname or "").lower()
            if final_host != expected_host:
                return 0, {"raw": "refused: response came from unexpected host %s" % final_host}
            return r.status, json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(raw)
        except Exception:
            return e.code, {"raw": redact(raw[:400])}
    except urllib.error.URLError as e:
        return 0, {"raw": "网络不可达: %s" % redact(e.reason)}


def call(path, cred, endpoint):
    headers = {"Authorization": "Bearer " + cred["token"]}
    if cred.get("uid"):
        headers["X-User-Id"] = cred["uid"]
    if cred.get("domain"):
        headers["X-Domain"] = cred["domain"]
    host = urllib.parse.urlparse(endpoint).hostname
    return _post(endpoint + path, headers, host)


# ---------------------------------------------------------------- 登录态续期
def refresh_session(cred, endpoint):
    """用 refreshToken 换新 accessToken。新 token 只在本次进程内使用，不落盘。

    返回 (ok, info)；info 里要么是新 auth 字段，要么是失败原因。
    """
    if not cred.get("refresh"):
        return False, {"reason": "no_refresh_token"}
    headers = {
        "X-Refresh-Token": cred["refresh"],
        "X-Auth-Refresh-Source": "plugin",
    }
    if cred.get("uid"):
        headers["X-User-Id"] = cred["uid"]
    if cred.get("domain"):
        headers["X-Domain"] = cred["domain"]
    host = urllib.parse.urlparse(endpoint).hostname
    status_code, payload = _post(endpoint + REFRESH_PATH, headers, host)
    if status_code != 200:
        return False, {"reason": "http_%s" % status_code}
    if not isinstance(payload, dict) or payload.get("code") not in (0, None):
        return False, {"reason": "api_code_%s" % (payload or {}).get("code")}
    data = (payload.get("data") or {})
    if not isinstance(data, dict) or not data.get("accessToken"):
        return False, {"reason": "empty_token"}
    return True, {"data": data}


def ensure_fresh_cred(cred, endpoint, allow_refresh=True):
    """token 邻近/已过期时续期一次；新凭据仅本次运行生效，绝不写盘。"""
    exp = 0
    try:
        payload_b64 = cred["token"].split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload_b64)).get("exp") or 0
    except Exception:
        exp = 0
    remaining = exp - time.time() if exp else None
    if remaining is None or remaining > 1800:
        return cred, None
    if not allow_refresh:
        return cred, "token 已过期但已禁用续期"
    if not cred.get("refresh"):
        return cred, "token 已过期且未提供 refreshToken，请重新提供凭据"
    ok, info = refresh_session(cred, endpoint)
    if not ok:
        return cred, "自动续期失败（%s）" % info.get("reason")
    new_auth = info["data"]
    cred2 = dict(cred)
    cred2["token"] = new_auth["accessToken"]
    if new_auth.get("refreshToken"):
        cred2["refresh"] = new_auth["refreshToken"]
    # 本技能不写任何文件；如需持久化新 token，由用户提供凭据的那一侧负责。
    return cred2, "已自动续期（新 token 仅本次运行生效，未写入任何文件）"


def ok(status, payload):
    return status == 200 and (not isinstance(payload, dict) or payload.get("code") in (0, None))


def auth_failed(status):
    return status in (401, 403)


def call_with_refresh(path, cred, endpoint, allow_refresh=True):
    """调用接口；若鉴权失效则续期一次并重试。返回 (cred, status, payload, note)。"""
    status, payload = call(path, cred, endpoint)
    if not auth_failed(status):
        return cred, status, payload, None
    cred2, note = ensure_fresh_cred(cred, endpoint, allow_refresh=allow_refresh)
    if cred2.get("token") == cred.get("token"):
        return cred, status, payload, note
    status2, payload2 = call(path, cred2, endpoint)
    return cred2, status2, payload2, note


# ---------------------------------------------------------------- 输出
def fmt_progress(wp):
    if not isinstance(wp, list):
        return ""
    return "".join("\u25cf" if x else "\u25cb" for x in wp)


def disp_width(s):
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


# ---------------------------------------------------------------- 主流程
def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 每日签到领积分")
    ap.add_argument("--status", action="store_true", help="只查询状态，不领取")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出状态")
    ap.add_argument("--force", action="store_true", help="强制调领取接口（调试用）")
    ap.add_argument("--no-refresh", action="store_true", help="禁止自动续期（排障用）")
    args = ap.parse_args()

    try:
        endpoint = resolve_endpoint()
    except ValueError as e:
        log("[FAIL] endpoint 校验失败：%s" % redact(e))
        return 1

    try:
        cred = token_provider.resolve()
    except token_provider.CredentialError as e:
        log("[FAIL] 凭据加载失败：%s" % redact(e))
        return 1
    except Exception as e:
        log("[FAIL] 凭据加载失败：%s" % redact(e))
        return 1

    allow_refresh = not args.no_refresh
    cred, status, payload, note = call_with_refresh(
        STATUS_PATH, cred, endpoint, allow_refresh=allow_refresh)
    if note:
        log("[INFO] %s" % note)

    if not ok(status, payload):
        if auth_failed(status):
            log("[FAIL] 鉴权失败（HTTP %s），accessToken 已过期且无法自动续期。" % status)
            log("       请重新提供有效凭据，或打开一次 WorkBuddy 客户端刷新登录态。")
            return 1
        log("[FAIL] 查询状态失败 HTTP %s：%s" % (status, redact(str(payload)[:300])))
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

    cred, status2, payload2, note2 = call_with_refresh(
        CLAIM_PATH, cred, endpoint, allow_refresh=allow_refresh)
    if note2 and note2 != note:
        log("[INFO] %s" % note2)
    if not ok(status2, payload2):
        if auth_failed(status2):
            log("[FAIL] 领取失败，鉴权失效（HTTP %s）。请重新提供有效凭据。" % status2)
            return 1
        log("[FAIL] 领取接口异常 HTTP %s：%s" % (status2, redact(str(payload2)[:300])))
        return 1

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
