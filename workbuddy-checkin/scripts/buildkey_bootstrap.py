"""自愈：当本地 build key 缺失/失效时，从**正在运行的 WorkBuddy 客户端**自动提取。

分工（实测得出，别随意改）：
- 进程发现由 Python 做：Windows Toolhelp32（ctypes），**不派生任何子进程**。
  理由：node 在本机环境下 spawn 任何子进程都会 EBUSY（连 cmd.exe 都起不来），
  所以「让 node 自己去找 PID」这条路走不通。
- CDP 交互由 Node 做（fetch + WebSocket）：`buildkey_cdp.js`，同样不派生子进程。
- Python 只在最后一步派生一次 node，这一步是可行的。

安全约束：
- 附加前先确认端口空闲，避免读到别的进程的 inspector；
- 提取成功/失败都让 node 侧调用 `inspector.close()` 关掉调试端口，
  不在常驻的客户端上留下 open 的 inspector。
"""
import ctypes
import json
import os
import subprocess
import sys
from ctypes import wintypes

CLIENT_EXE = os.environ.get("WB_CLIENT_EXE", "WorkBuddy.exe")
DEFAULT_PORT = int(os.environ.get("WB_INSPECT_PORT", "9229"))
PER_PID_TIMEOUT = 25

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def client_processes(exe=CLIENT_EXE):
    """返回 [(pid, ppid)]，按 Windows Toolhelp32 枚举；失败返回 None 表示枚举不可用。"""
    if not sys.platform.startswith("win"):
        return None
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    except OSError:
        return None
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(PROCESSENTRY32W)]
    k32.CloseHandle.argtypes = [ctypes.c_void_p]

    snap = k32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if snap == INVALID_HANDLE_VALUE or snap is None:
        return None
    out = []
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.szExeFile and entry.szExeFile.lower() == exe.lower():
                out.append((int(entry.th32ProcessID), int(entry.th32ParentProcessID)))
            ok = k32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        k32.CloseHandle(snap)
    return out


def candidate_pids():
    """主进程优先的候选 PID 列表。

    主进程判据：父进程不是本应用自己的进程（Electron 主进程由 exe 直接启动，
    渲染/GPU/utility 子进程的父进程则是主进程）。多个候选时按 PID 升序（先创建者在前）。
    """
    procs = client_processes()
    if procs is None:
        procs = _fallback_tasklist()
    if not procs:
        return []
    self_pids = {pid for pid, _ in procs}
    mains = [pid for pid, ppid in procs if ppid not in self_pids]
    others = sorted(pid for pid, _ in procs if pid not in mains)
    return sorted(mains) + others


def _fallback_tasklist():
    """兜底：tasklist 拿不到 PPID，只能按 PID 升序猜（不保证命中主进程）。"""
    try:
        r = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq %s" % CLIENT_EXE, "/FO", "CSV", "/NH"],
            capture_output=True, text=True, encoding="latin1", errors="replace", timeout=20,
        )
    except Exception:
        return None
    out = []
    for line in (r.stdout or "").splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) >= 2 and parts[0].lower() == CLIENT_EXE.lower():
            try:
                out.append((int(parts[1]), 0))
            except ValueError:
                pass
    return out or None


def find_node():
    """定位 node 可执行文件。"""
    env = os.environ.get("WB_NODE")
    if env and os.path.isfile(env):
        return env
    for c in (
        os.path.join(os.path.expanduser("~"), ".workbuddy", "binaries", "node", "versions"),
    ):
        if os.path.isdir(c):
            cands = []
            for name in os.listdir(c):
                exe = os.path.join(c, name, "node.exe")
                if os.path.isfile(exe):
                    cands.append(exe)
            if cands:
                return sorted(cands)[-1]
    for exe in (
        r"C:\Program Files\nodejs\node.exe",
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "nodejs", "node.exe"),
    ):
        if os.path.isfile(exe):
            return exe
    from shutil import which
    return which("node")


def _run_cdp(node_exe, cdp_js, pid, port):
    try:
        r = subprocess.run(
            [node_exe, cdp_js, str(pid), str(port)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=PER_PID_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "note": "timeout"}
    except Exception as e:
        return {"ok": False, "note": "spawn_failed:%s" % e}
    line = ""
    for ln in reversed((r.stdout or "").splitlines()):
        if ln.strip().startswith("{"):
            line = ln.strip()
            break
    if not line:
        return {"ok": False, "note": "no_output(rc=%s)" % r.returncode}
    try:
        return json.loads(line)
    except Exception:
        return {"ok": False, "note": "bad_json"}


def bootstrap(out_path=None, verbose=False, max_pids=12):
    """尝试从运行中的客户端提取 build key 并写入 out_path。

    返回 (ok: bool, info: dict)。
    """
    def log(*a):
        if verbose:
            print(*a, flush=True)

    here = os.path.dirname(os.path.abspath(__file__))
    out_path = out_path or os.path.join(here, "at_rest_buildkey.json")
    cdp_js = os.path.join(here, "buildkey_cdp.js")

    node_exe = find_node()
    if not node_exe:
        return False, {"reason": "node_not_found",
                       "hint": "安装 Node.js，或用 WB_NODE 指定 node 路径"}
    if not os.path.isfile(cdp_js):
        return False, {"reason": "missing_tool", "hint": cdp_js}

    pids = candidate_pids()
    if not pids:
        return False, {"reason": "client_not_running",
                       "hint": "请先启动 WorkBuddy 客户端，再重试"}

    tried = []
    port = DEFAULT_PORT
    for pid in pids[:max_pids]:
        res = _run_cdp(node_exe, cdp_js, pid, port)
        tried.append({"pid": pid, "note": "ok" if res.get("ok") else res.get("note")})
        log("  pid=%s -> %s" % (pid, "ok" if res.get("ok") else res.get("note")))
        if res.get("ok"):
            payload = res["payload"]
            data = {
                "_comment": ("WorkBuddy 客户端 at-rest build key（atRestSecretKey）。构建级常量，"
                             "同版本全安装一致，用于解密本地凭据信封。由 buildkey_bootstrap 自动提取。"),
                "version": payload.get("version") or 1,
                "atRestSecretKey": payload["atRestSecretKey"],
                "developerPublicKeyId": (payload.get("atRestDeveloperPublicKey") or {}).get("id"),
                "source": "electron.workbuddyStorage.loggerGet() via CDP, pid=%s" % pid,
            }
            import datetime
            data["capturedAt"] = datetime.date.today().isoformat()
            try:
                with open(out_path, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
            except Exception as e:
                return False, {"reason": "write_failed:%s" % e, "tried": tried}
            return True, {"pid": pid, "out": out_path, "tried": tried}
        if res.get("note") == "port_range_busy_abort":
            break  # 端口被占，继续试别的 PID 也没意义

    return False, {"reason": "no_main_process_found", "tried": tried,
                   "hint": "客户端可能刚启动还没初始化完，稍后再试"}


if __name__ == "__main__":
    argv = sys.argv[1:]
    ok, info = bootstrap(verbose=True)
    print(("OK " if ok else "FAIL ") + json.dumps(info, ensure_ascii=False))
    sys.exit(0 if ok else 1)
