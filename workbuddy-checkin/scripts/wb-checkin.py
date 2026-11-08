#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
WorkBuddy 每日签到领积分 —— 幂等、零依赖（仅标准库）

直接调用 WorkBuddy(CodeBuddy) 后端签到接口，无需打开客户端 UI。
凭据从客户端本地的会话文件中读取，不做任何网络登录。

自客户端 2026-09-19 起，本地 accessToken 由明文 JWT 改为 at-rest 加密信封
（{"$wbEncrypted":1,"envelope":"<b64>"}）。本脚本通过同目录的 at_rest.py 解密它；
若解密不可用（缺 build key / 客户端换了 keyId），自动回退到目录里的历史明文 token。

用法:
  python wb-checkin.py            正常签到（幂等：今日已领则跳过）
  python wb-checkin.py --status   只查询状态，不领取
  python wb-checkin.py --json     以 JSON 输出状态（便于脚本消费）
  python wb-checkin.py --force    强制调用领取接口（调试用）

退出码:
  0  成功领取 / 今日已领 / 活动未开启
  1  失败（鉴权失效、凭据缺失、接口异常）

环境变量:
  WB_AUTH_DIR            覆盖凭据目录（自动探测失败时使用）
  WB_ENDPOINT            覆盖后端地址（默认 https://copilot.tencent.com）
  WB_AT_REST_SECRET_KEY  直接给出 at-rest build key（覆盖缓存文件）
"""
import os
import re
import sys
import json
import glob
import time
import platform
import argparse
import urllib.request
import urllib.error

# at_rest.py 与本脚本同目录；为兼容从其它目录调用，把脚本目录也加进搜索路径
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

DEFAULT_ENDPOINT = "https://copilot.tencent.com"
STATUS_PATH = "/v2/billing/meter/checkin-activity-status"
CLAIM_PATH = "/v2/billing/meter/daily-checkin"
# 续期端点：来自客户端 app.asar 的 external-link auth（prefixPath=/plugin）
# 注意 /v2/auth/token/refresh 是 404，前缀 /plugin 不可省
REFRESH_PATH = "/v2/plugin/auth/token/refresh"

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


def _load_at_rest():
    """导入 at_rest 解密模块；不可用返回 None（此时只能吃明文 token）。"""
    try:
        import at_rest
        return at_rest
    except Exception:
        return None


def _read_info(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def _scan_cred():
    """扫一遍凭据目录，返回 dict(cred, rejected, tried, envelope_blocked)。

    envelope_blocked=True 表示「遇到加密信封但解不开」——这才是值得触发自愈的情形
    （区别于「压根没登录过」）。
    """
    at_rest = _load_at_rest()
    tried, rejected = [], []
    envelope_blocked = False

    for d in auth_dir_candidates():
        tried.append(d)
        if not os.path.isdir(d):
            continue
        files = glob.glob(os.path.join(d, "*.info"))
        if not files:
            continue

        parsed = []
        for f in files:
            data = _read_info(f)
            if data:
                parsed.append((f, data))
        if not parsed:
            continue

        # 目标账号 = 最新文件里的 uid（uid 是明文，即使 token 加密也能读到）
        parsed.sort(key=lambda x: os.path.getmtime(x[0]), reverse=True)
        target_uid = None
        for _f, data in parsed:
            uid = (data.get("account") or {}).get("uid") or (data.get("auth") or {}).get("uid")
            if uid:
                target_uid = uid
                break

        def rank(item):
            f, data = item
            auth = data.get("auth") or {}
            acct = data.get("account") or {}
            uid = acct.get("uid") or auth.get("uid")
            exp = (auth.get("expiresAt") or 0) / 1000
            return (
                0 if uid == target_uid else 1,                       # 同账号优先
                0 if exp > time.time() else 1,                       # 未过期优先
                0 if not BACKUP_RE.search(os.path.basename(f)) else 1,  # 当前会话文件优先
                -os.path.getmtime(f),                                # 再按修改时间倒序
            )

        for f, data in sorted(parsed, key=rank):
            auth = data.get("auth") or {}
            acct = data.get("account") or {}
            raw = auth.get("accessToken")
            if not raw:
                continue
            try:
                if isinstance(raw, str):
                    token = raw
                else:
                    envelope_blocked = True
                    if at_rest is None:
                        raise RuntimeError("at_rest 模块不可用，无法解密信封")
                    token = at_rest.unwrap(raw)
            except Exception as e:
                rejected.append("%s: %s" % (os.path.basename(f)[:48], e))
                continue
            if not isinstance(token, str) or token.count(".") < 2:
                rejected.append("%s: 解出的不是 JWT" % os.path.basename(f)[:48])
                continue

            # refreshToken 只用于续期，解不开不算致命（老版本可能没有）
            refresh = None
            raw_refresh = auth.get("refreshToken")
            if raw_refresh:
                try:
                    refresh = at_rest.unwrap(raw_refresh) if at_rest else (
                        raw_refresh if isinstance(raw_refresh, str) else None)
                except Exception:
                    refresh = None

            return {
                "cred": {
                    "file": f,
                    "token": token,
                    "refresh": refresh,
                    "uid": acct.get("uid") or auth.get("uid"),
                    "domain": auth.get("domain"),
                    "data": data,          # 原始文件内容，写回时保留其它字段
                },
                "rejected": rejected,
                "tried": tried,
                "envelope_blocked": envelope_blocked,
            }

    return {"cred": None, "rejected": rejected, "tried": tried,
            "envelope_blocked": envelope_blocked}


def _bootstrap_build_key():
    """自愈：向正在运行的客户端要 build key。返回 (ok, info)。"""
    try:
        import buildkey_bootstrap
    except Exception as e:
        return False, {"reason": "bootstrap_module_unavailable: %s" % e}
    try:
        return buildkey_bootstrap.bootstrap(verbose=False)
    except Exception as e:
        return False, {"reason": "bootstrap_error: %s" % e}


def load_cred(allow_bootstrap=True):
    """挑一个能用的凭据：优先当前会话文件，其次历史备份；加密信封先解密。

    若遇到「加密信封但无可用 build key」，自动从运行中的客户端提取后重试一次。
    """
    res = _scan_cred()
    if res["cred"]:
        return res["cred"]

    if allow_bootstrap and res["envelope_blocked"]:
        log("[INFO] 本地凭据是加密信封，但没有可用的 build key；尝试从运行中的客户端自动提取…")
        ok, info = _bootstrap_build_key()
        if ok:
            log("[INFO] 已提取 build key（pid=%s），重试解密…" % info.get("pid"))
            res2 = _scan_cred()
            if res2["cred"]:
                return res2["cred"]
            res["rejected"] = res2["rejected"] or res["rejected"]
        else:
            reason = info.get("reason")
            log("[WARN] 自动提取 build key 失败：%s" % reason)
            if info.get("hint"):
                log("       %s" % info["hint"])

    msg = ("未找到可用的凭据文件。已尝试以下目录：\n  " + "\n  ".join(res["tried"])
           + "\n请确认已登录 WorkBuddy 客户端，或用环境变量 WB_AUTH_DIR 指定凭据目录。")
    if res["rejected"]:
        msg += "\n\n已跳过（解密/格式不合格）：\n  " + "\n  ".join(res["rejected"][:8])
    raise FileNotFoundError(msg)


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


# ---------------------------------------------------------------- 登录态续期
def client_running():
    """客户端主进程是否在跑。用于判断「外部续期是否会与客户端抢 token」。"""
    try:
        import buildkey_bootstrap
        pids = buildkey_bootstrap.candidate_pids()
        return bool(pids)
    except Exception:
        return False


def _claim_lock(lock_path, stale_seconds=120):
    """取一个基于 O_EXCL 的排他锁，避免与定时任务/手动并发写凭据。"""
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        return True
    except FileExistsError:
        # 陈旧锁（进程已死或超时）直接接管
        try:
            if time.time() - os.path.getmtime(lock_path) > stale_seconds:
                os.unlink(lock_path)
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
        except Exception:
            pass
        return False


def _release_lock(lock_path):
    try:
        os.unlink(lock_path)
    except Exception:
        pass


def _now_ms():
    return int(time.time() * 1000)


def refresh_session(cred, endpoint):
    """用 refreshToken 换新的 accessToken/refreshToken。

    返回 (ok, info)。info 里要么是新 auth 字段，要么是失败原因。
    """
    if not cred.get("refresh"):
        return False, {"reason": "no_refresh_token"}

    url = endpoint.rstrip("/") + REFRESH_PATH
    req = urllib.request.Request(url, data=b"{}", method="POST")
    req.add_header("X-Refresh-Token", cred["refresh"])
    req.add_header("X-Auth-Refresh-Source", "plugin")
    if cred.get("uid"):
        req.add_header("X-User-Id", str(cred["uid"]))
    if cred.get("domain"):
        req.add_header("X-Domain", str(cred["domain"]))
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "replace")
        try:
            payload = json.loads(raw)
        except Exception:
            payload = {"raw": raw[:300]}
        return False, {"reason": "http_%s" % e.code, "payload": payload}
    except urllib.error.URLError as e:
        return False, {"reason": "network: %s" % e.reason}

    if payload.get("code") not in (0, None):
        return False, {"reason": "api_code_%s" % payload.get("code"), "payload": payload}

    data = (payload.get("data") or {})
    if not data.get("accessToken"):
        return False, {"reason": "empty_token", "payload": payload}
    return True, {"data": data}


def _seal_like(original_value, plaintext):
    """按原字段的形态写回：原来是明文 str 就写明文，原来是信封就加密。"""
    if isinstance(original_value, str):
        return plaintext
    import at_rest
    return at_rest.seal(plaintext)


def persist_auth(cred, new_auth):
    """把新 token 加密写回凭据文件（原子替换 + 备份 + 保留其它字段）。

    失败不抛，返回 (ok, msg)，让签到流程能降级继续。
    """
    path = cred.get("file")
    data = cred.get("data")
    if not path or not isinstance(data, dict):
        return False, "缺少原始凭据上下文"

    auth = data.get("auth")
    if not isinstance(auth, dict):
        return False, "凭据文件缺少 auth 段"

    try:
        import at_rest
    except Exception as e:
        return False, "at_rest 不可用: %s" % e

    lock_path = path + ".wb-lock"
    if not _claim_lock(lock_path):
        return False, "凭据文件被占用（另一实例正在写），本次跳过写回"

    tmp_path = path + ".wb-tmp"
    bak_path = path + ".wb-bak"
    try:
        # 写前再确认一次：若文件已被客户端更新，放弃写回（避免覆盖更新的 token）
        try:
            with open(path, encoding="utf-8") as fh:
                latest = json.load(fh)
            if (latest.get("auth") or {}).get("accessToken") != auth.get("accessToken"):
                return False, "凭据已被客户端更新，跳过写回"
        except Exception:
            pass

        # 先备份原始文件（只保留一份，避免堆积）
        try:
            with open(path, "rb") as src, open(bak_path, "wb") as dst:
                dst.write(src.read())
        except Exception:
            pass

        auth["accessToken"] = _seal_like(auth.get("accessToken"), new_auth["accessToken"])
        if new_auth.get("refreshToken"):
            auth["refreshToken"] = _seal_like(auth.get("refreshToken"), new_auth["refreshToken"])
        for k in ("expiresIn", "refreshExpiresIn", "tokenType", "domain", "sessionState"):
            if new_auth.get(k) is not None:
                auth[k] = new_auth[k]

        # 时间戳按客户端 calculateExpiresAt 的语义重算
        now = _now_ms()
        auth["lastRefreshTime"] = now
        if new_auth.get("expiresAt"):
            auth["expiresAt"] = int(new_auth["expiresAt"])
        elif new_auth.get("expiresIn"):
            auth["expiresAt"] = now + int(new_auth["expiresIn"]) * 1000
        if new_auth.get("refreshExpiresAt"):
            auth["refreshExpiresAt"] = int(new_auth["refreshExpiresAt"])
        elif new_auth.get("refreshExpiresIn"):
            auth["refreshExpiresAt"] = now + int(new_auth["refreshExpiresIn"]) * 1000

        with open(tmp_path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, separators=(",", ":"))
            fh.flush()
            os.fsync(fh.fileno())
        # 原子替换：同目录 rename
        os.replace(tmp_path, path)
        return True, "已写回 %s" % os.path.basename(path)
    except Exception as e:
        for p in (tmp_path,):
            try:
                os.unlink(p)
            except Exception:
                pass
        return False, "写回失败: %s" % e
    finally:
        _release_lock(lock_path)


def ensure_fresh_cred(cred, endpoint, allow_refresh=True):
    """如果 accessToken 已过期（或即将过期），先续期。

    写回策略：仅在客户端未运行时落盘。客户端在跑时它可能随时重写凭据文件，
    我们只把新 token 用于本次运行，避免与客户端争抢文件。
    实测旧 refreshToken 在续期后仍可用，因此外部续期不会让客户端掉线。

    返回 (cred, note)。续期失败时原样返回，由调用方决定后续行为。
    """
    exp = 0
    try:
        import base64
        payload_b64 = cred["token"].split(".")[1]
        payload_b64 += "=" * (-len(payload_b64) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload_b64)).get("exp") or 0
    except Exception:
        exp = 0

    remaining = exp - time.time() if exp else None
    # 留 30 分钟余量，避免签到途中恰好过期
    if remaining is None or remaining > 1800:
        return cred, None

    if not allow_refresh:
        return cred, "token 已过期但已禁用续期（--no-refresh）"

    if not cred.get("refresh"):
        return cred, "token 已过期且没有 refreshToken，只能重新登录客户端"

    ok, info = refresh_session(cred, endpoint)
    if not ok:
        return cred, "自动续期失败（%s）" % info.get("reason")

    new_auth = info["data"]
    cred2 = dict(cred)
    cred2["token"] = new_auth["accessToken"]
    if new_auth.get("refreshToken"):
        cred2["refresh"] = new_auth["refreshToken"]

    if client_running():
        return cred2, "已自动续期（客户端在运行，本次不写回凭据文件）"

    wrote, msg = persist_auth(cred, new_auth)
    if wrote:
        return cred2, "已自动续期并写回凭据文件"
    return cred2, "已自动续期（写回失败：%s，仅在本次运行生效）" % msg


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


def main():
    ap = argparse.ArgumentParser(description="WorkBuddy 每日签到领积分")
    ap.add_argument("--status", action="store_true", help="只查询状态，不领取")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出状态")
    ap.add_argument("--force", action="store_true", help="强制调用领取接口（调试）")
    ap.add_argument("--no-refresh", action="store_true",
                    help="禁止自动续期登录态（只读本地凭据）")
    args = ap.parse_args()

    endpoint = os.environ.get("WB_ENDPOINT") or DEFAULT_ENDPOINT

    try:
        cred = load_cred()
    except Exception as e:
        log("[FAIL] 凭据加载失败：%s" % e)
        return 1

    allow_refresh = not args.no_refresh

    cred, status, payload, note = call_with_refresh(
        STATUS_PATH, cred, endpoint, allow_refresh=allow_refresh)
    if note:
        log("[INFO] %s" % note)

    if not ok(status, payload):
        if auth_failed(status):
            log("[FAIL] 鉴权失败（HTTP %s），accessToken 已过期且无法自动续期。" % status)
            if not allow_refresh:
                log("       本次以 --no-refresh 运行，未尝试续期。")
            else:
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

    cred, status2, payload2, note2 = call_with_refresh(
        CLAIM_PATH, cred, endpoint, allow_refresh=allow_refresh)
    if note2 and note2 != note:
        log("[INFO] %s" % note2)
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
