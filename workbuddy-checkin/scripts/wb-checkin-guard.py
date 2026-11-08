# -*- coding: utf-8 -*-
"""WorkBuddy 签到守卫：按频率轮询，领到即当天收工。

由操作系统计划任务周期性调用（Windows 用 pythonw.exe，无窗口）：
- 今天的 marker 已存在 -> 立刻退出，不联网、不写日志
- 否则跑一次同目录的 wb-checkin.py：
    [OK]  领取成功 -> 写 marker（今天到此为止）
    [SKIP] 今天已被领过 -> 同样写 marker（一天只领一次）
    [FAIL] -> 不写 marker，下个周期继续重试

路径限制（本文件实际落实，不做词法层面声明）：
  * 解释器只从两个显式可信候选中选：随客户端分发的固定路径，或当前
    sys.executable；两者都按 realpath 解析后校验真实目标链。
  * 脚本与解释器：先查原始词法路径链，再 realpath，逐级拒绝符号链接 /
    Windows reparse point（junction）。
  * marker 与日志：保留原始词法路径，**链接检查必须先于 realpath**——
    否则一个指向外部普通文件的链接会被解析成看似正常的目标，写入就逃出
    技能目录。解析后还要求目标仍在 BASE 内。
  * 所有读写都走 os.open(..., O_NOFOLLOW) 的目录内打开，并对 fd 做
    fstat 复核（普通文件、非 reparse point），缩小检查与打开之间的竞态窗口。
    Windows 无 O_NOFOLLOW，改用 lstat/fstat 的 reparse point 属性位兜底。
  * 技能目录本身不可被「其他用户」写入（POSIX）；Windows 依赖计划任务
    账户隔离，不主动改写 ACL（避免安装副作用）。
  * 子进程的 argv 全部是代码内常量，shell=False，不接受任何外部输入。

注意：本脚本仍会启动一个子进程（这是「守卫」的本质）；它启动的是谁、
从哪个真实路径启动，由上面的策略严格限定。
"""
import datetime
import json
import os
import stat
import subprocess
import sys

# Windows reparse point 标志（junction / 目录链接 / 符号链接都带这一位）
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

_BASE_LEXICAL = os.path.dirname(os.path.abspath(__file__))
BASE = os.path.realpath(_BASE_LEXICAL)
PY = os.path.realpath(os.path.join(
    BASE, "..", "binaries", "python", "versions", "3.13.12", "python.exe"))
# 保留脚本的原始词法路径：realpath 会把链接解析掉，反而看不见「目录被换成 junction」。
_SCRIPT_LEXICAL = os.path.join(BASE, "wb-checkin.py")
SCRIPT = os.path.realpath(_SCRIPT_LEXICAL)
# marker / log 一律保留词法路径，绝不提前 realpath（见模块 docstring）。
_MARK_LEXICAL = os.path.join(BASE, "checkin-done.json")
_LOG_LEXICAL = os.path.join(BASE, "checkin-os.log")
LOG_MAX = 1024 * 1024

# 固定参数白名单：子进程只接受这两个开关，不接受任何外部输入。
FIXED_ARGV = ("-u",)


def today():
    return datetime.date.today().isoformat()


def is_link(path):
    """是否符号链接 / Windows reparse point。lstat 不跟随链接。

    目标不存在时返回 False（新建 marker / 日志属正常场景）；其余元数据
    获取失败一律视为不可信。
    """
    try:
        st = os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError:
        return True
    if stat.S_ISLNK(st.st_mode):
        return True
    if os.name == "nt":
        attrs = getattr(st, "st_file_attributes", 0)
        if attrs & FILE_ATTRIBUTE_REPARSE_POINT:
            return True
    return False


def path_has_link(path):
    """从根到目标逐段检查，任一级是链接/重解析点就返回 True。"""
    cur = os.path.abspath(path)
    seen = set()
    while True:
        if cur in seen:
            return True
        seen.add(cur)
        if is_link(cur):
            return True
        parent = os.path.dirname(cur)
        if not parent or parent == cur:
            return False
        cur = parent


def dir_writable_by_others(path):
    """POSIX 下拒绝「其他用户可写」的目录；Windows ACL 由计划任务账户隔离保证。

    不做跨平台的 ACL 模拟：Windows 上目录归当前用户即可，若被替换，
    上面的 realpath + reparse point 校验会先挡住。
    """
    if os.name == "nt":
        return False
    try:
        mode = os.stat(path).st_mode
        return bool(mode & stat.S_IWOTH)
    except OSError:
        return True


def confined_target(lexical):
    """校验一个写入/读取目标，返回可安全使用的真实路径。

    顺序很关键：先在**词法路径**上拒绝链接，再 realpath。若先 realpath，
    一个指向 BASE 外部普通文件的符号链接会被解析掉、伪装成正常目标。
    """
    if os.path.dirname(os.path.abspath(lexical)) != BASE:
        raise RuntimeError("目标不在技能目录内")
    if path_has_link(BASE):
        raise RuntimeError("技能目录路径链含符号链接/reparse point")
    if dir_writable_by_others(BASE):
        raise RuntimeError("技能目录对其他用户可写")
    # lexists 能识别「指向悬空目标的链接」；链接一律拒绝，不跟随。
    if os.path.lexists(lexical) and is_link(lexical):
        raise RuntimeError("目标是符号链接/reparse point")
    real = os.path.realpath(lexical)
    if os.path.dirname(real) != BASE:
        raise RuntimeError("目标解析后落在技能目录之外")
    if path_has_link(real):
        raise RuntimeError("目标真实路径链含链接")
    return real


def open_confined(lexical, flags):
    """目录内、无跟随地打开目标，并对 fd 复核（缩小 TOCTOU 窗口）。

    POSIX 用 O_NOFOLLOW；Windows 无此标志，改由 fstat 的 reparse point 位复核。
    """
    target = confined_target(lexical)
    fd = os.open(target, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise RuntimeError("目标不是普通文件")
        if os.name == "nt":
            if getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
                raise RuntimeError("目标是 reparse point")
    except Exception:
        os.close(fd)
        raise
    return fd


def verify_confined():
    """校验解释器与脚本的真实目标落在预期位置，且路径链/目录权限可信。

    返回 (exe, script)；任一条件不满足抛 RuntimeError。
    """
    # 原始词法路径链也要查：若技能目录本身是 junction/symlink，realpath 后
    # 只剩解析结果，链接本身会被「抹掉」，必须在这里先挡一次。
    if path_has_link(_BASE_LEXICAL):
        raise RuntimeError("技能目录原始路径链含符号链接/reparse point")
    if path_has_link(BASE):
        raise RuntimeError("技能目录 realpath 链含符号链接/reparse point")
    if os.path.realpath(BASE) != BASE:
        raise RuntimeError("技能目录 realpath 与自身不一致")
    if dir_writable_by_others(BASE):
        raise RuntimeError("技能目录对其他用户可写")

    # 显式可信解释器候选：固定分发路径 + 当前解释器（sys.executable）。
    # 候选一律先 realpath 得到「解析后的真实目标」，再对该目标做校验：
    # 合法安装里 python.exe 常通过 junction 挂载，校验真实目标才不会误伤，
    # 同时真实目标链上若仍有链接/reparse point 一律拒绝。
    candidates = [PY, os.path.realpath(sys.executable)]
    exe = None
    problems = []
    for cand in candidates:
        real = os.path.realpath(cand)
        if not os.path.isfile(real):
            problems.append("%s 不存在" % os.path.basename(cand))
            continue
        if path_has_link(cand):
            problems.append("%s 真实目标路径链含链接" % os.path.basename(cand))
            continue
        if dir_writable_by_others(os.path.dirname(real)):
            problems.append("%s 所在目录对其他用户可写" % os.path.basename(real))
            continue
        if not os.path.basename(real).lower().startswith("python"):
            problems.append("%s 不是 Python 解释器" % os.path.basename(real))
            continue
        exe = real
        break
    if exe is None:
        raise RuntimeError("没有通过校验的可信解释器：%s" % "; ".join(problems))

    if os.path.realpath(SCRIPT) != SCRIPT:
        raise RuntimeError("脚本 realpath 与自身不一致")
    if os.path.dirname(SCRIPT) != BASE:
        raise RuntimeError("脚本不在技能目录内")
    if (path_has_link(_SCRIPT_LEXICAL) or path_has_link(SCRIPT)
            or not os.path.isfile(SCRIPT)):
        raise RuntimeError("脚本路径链含链接或不是普通文件")
    return exe, SCRIPT


def read_mark():
    """读取当日标记；链接/越界目标一律视为「今天还没领」并拒绝读取。"""
    try:
        if not os.path.lexists(_MARK_LEXICAL):
            return {}
        fd = open_confined(_MARK_LEXICAL, os.O_RDONLY)
        try:
            with os.fdopen(fd, "r", encoding="utf-8") as fh:
                return json.load(fh) or {}
        except Exception:
            return {}
    except Exception:
        return {}


def log(text):
    """追加一条日志；目标不可信时静默放弃（不让日志写坏 guard 主流程）。"""
    try:
        target = confined_target(_LOG_LEXICAL)
        if os.path.lexists(target):
            st = os.lstat(target)
            if stat.S_ISREG(st.st_mode) and st.st_size > LOG_MAX:
                os.remove(target)
        fd = open_confined(
            _LOG_LEXICAL, os.O_WRONLY | os.O_CREAT | os.O_APPEND)
        try:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            os.write(fd, ("[%s] %s\n" % (ts, text)).encode("utf-8"))
        finally:
            os.close(fd)
    except Exception:
        pass


def main():
    if read_mark().get("date") == today():
        return 0  # 今天已领，本周期直接收工

    try:
        exe, script = verify_confined()
    except Exception as exc:
        log("PATH CONFINEMENT FAILED: %s" % exc)
        return 1

    # 全部 argv 均为代码内常量；shell=False 是显式声明，不是默认值巧合。
    argv = [exe] + list(FIXED_ARGV) + [script]
    try:
        proc = subprocess.run(
            argv,
            shell=False,
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
        payload = json.dumps(
            {
                "date": today(),
                "total": total,
                "at": datetime.datetime.now().isoformat(timespec="seconds"),
                "result": headline,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        try:
            fd = open_confined(
                _MARK_LEXICAL, os.O_WRONLY | os.O_CREAT | os.O_TRUNC)
            try:
                os.write(fd, payload)
            finally:
                os.close(fd)
        except Exception as exc:
            log("MARKER WRITE FAILED: %s" % exc)
        log(headline or "done")
        return 0

    log("FAILED\n" + out.strip()[:900])
    return 1


if __name__ == "__main__":
    sys.exit(main())
