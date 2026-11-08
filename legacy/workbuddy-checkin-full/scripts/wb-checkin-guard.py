# -*- coding: utf-8 -*-
"""WorkBuddy 签到守卫：按频率轮询，领到即当天收工。

由 Windows 计划任务（pythonw.exe 启动，无窗口）周期性调用：
- 今天的 marker 已存在 → 立刻退出，不联网、不写日志
- 否则跑一次 wb-checkin.py：
    [OK]  领取成功 → 写 marker（今天到此为止）
    [SKIP] 今天已被领过 → 同样写 marker（一天只领一次）
    [FAIL] → 不写 marker，下个周期继续重试

marker: checkin-done.json  {"date": "2026-11-08", "total": 3200, ...}
日志:   checkin-os.log（只在真正执行或失败时写，已领的轮询不留痕）
"""
import datetime
import json
import os
import subprocess
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
PY = os.path.join(BASE, "..", "binaries", "python", "versions", "3.13.12", "python.exe")
PY = os.path.abspath(PY)
SCRIPT = os.path.join(BASE, "wb-checkin.py")
MARK = os.path.join(BASE, "checkin-done.json")
LOG = os.path.join(BASE, "checkin-os.log")
LOG_MAX = 1024 * 1024


def today():
    return datetime.date.today().isoformat()


def read_mark():
    try:
        with open(MARK, "r", encoding="utf-8") as fh:
            return json.load(fh) or {}
    except Exception:
        return {}


def log(text):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > LOG_MAX:
            os.remove(LOG)
    except Exception:
        pass
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write("[%s] %s\n" % (ts, text))


def main():
    if read_mark().get("date") == today():
        return 0  # 今天已领，本周期直接收工

    if not os.path.exists(PY):
        # 解释器路径可能随客户端升级变化，回退到当前解释器
        exe = sys.executable
    else:
        exe = PY

    try:
        proc = subprocess.run(
            [exe, "-u", SCRIPT],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180,
        )
        out = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired:
        log("TIMEOUT > 180s，下个周期重试")
        return 1
    except Exception as exc:
        log("EXEC ERROR: %s" % exc)
        return 1

    headline = ""
    for line in out.splitlines():
        if line.startswith("[OK]") or line.startswith("[SKIP]") or line.startswith("[FAIL]"):
            headline = line.strip()
            break

    if "[OK]" in out or "[SKIP]" in out:
        total = None
        for line in out.splitlines():
            if "累计积分" in line:
                try:
                    total = int(line.split(":")[-1].strip())
                except Exception:
                    total = None
        try:
            with open(MARK, "w", encoding="utf-8") as fh:
                json.dump(
                    {
                        "date": today(),
                        "total": total,
                        "at": datetime.datetime.now().isoformat(timespec="seconds"),
                        "result": headline,
                    },
                    fh, ensure_ascii=False,
                )
        except Exception as exc:
            log("MARKER WRITE FAILED: %s" % exc)
        log(headline or "done")
        return 0

    log("FAILED\n" + out.strip()[:900])
    return 1


if __name__ == "__main__":
    sys.exit(main())
