// 对**单个**指定 PID 附加 V8 inspector，读取工作存储里的 build key，然后关掉 inspector。
// 由 buildkey_bootstrap.py 调用（Python 负责进程发现，Node 只做 CDP，不派生子进程）。
//
// 用法: node buildkey_cdp.js <pid> [端口=9229]
// 输出一行 JSON：
//   {"ok":true,"payload":{...},"port":9229}
//   {"ok":false,"note":"..."}
//
// 关于端口（踩过的坑）：process._debugProcess(pid) **固定**使用默认端口 9229，
// 无法指定。所以附加前必须先保证 9229 是空的：
//   - 若 9229 空闲                              -> 直接附加
//   - 若 9229 被占且属于同一客户端（残留 inspector）-> 先关掉它，再附加
//   - 若 9229 被其它程序占用                     -> 放弃（绝不误读/误关别人的 inspector）
const pid = Number(process.argv[2]);
const PORT = Number(process.argv[3] || 9229);
const fs = require("fs");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// 同步写 stdout：避免进程结束时异步 console.log 被截断
const emit = (o) => {
  try {
    fs.writeSync(1, JSON.stringify(o) + "\n");
  } catch (e) {}
};

async function listTargets() {
  const ctl = new AbortController();
  const t = setTimeout(() => ctl.abort(), 1000);
  try {
    const res = await fetch(`http://127.0.0.1:${PORT}/json/list`, { signal: ctl.signal });
    const list = await res.json();
    const arr = Array.isArray(list) ? list : [];
    return arr.find((x) => x.type === "node") || arr[0] || null;
  } catch (e) {
    return null;
  } finally {
    clearTimeout(t);
  }
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const to = setTimeout(() => reject(new Error("ws_timeout")), 5000);
    ws.addEventListener("open", () => {
      clearTimeout(to);
      resolve(ws);
    });
    ws.addEventListener("error", () => {
      clearTimeout(to);
      reject(new Error("ws_error"));
    });
  });
}

function makeSender(ws) {
  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data.toString());
    if (m.id && pending.has(m.id)) {
      pending.get(m.id).resolve(m);
      pending.delete(m.id);
    }
  });
  return (method, params) =>
    new Promise((resolve) => {
      const mid = ++id;
      pending.set(mid, { resolve });
      ws.send(JSON.stringify({ id: mid, method, params }));
    });
}

const PROBE_EXPR = `(function () {
  try {
    var mod = process.mainModule;
    var req = mod && mod.require;
    if (typeof req !== 'function') return 'no_require';
    var el = req.call(mod, 'electron');
    var ws = el && el.workbuddyStorage;
    return (ws && typeof ws.loggerGet === 'function') ? 'ours' : 'other';
  } catch (e) { return 'other'; }
})()`;

const GET_EXPR = `(function () {
  try {
    var mod = process.mainModule;
    var req = mod && mod.require;
    if (typeof req !== 'function') return null;
    var el = req.call(mod, 'electron');
    var ws = el && el.workbuddyStorage;
    if (!ws || typeof ws.loggerGet !== 'function') return null;
    var v = ws.loggerGet();
    var obj = (typeof v === 'string') ? JSON.parse(v) : v;
    if (!obj || !obj.atRestSecretKey) return null;
    return JSON.stringify(obj);
  } catch (e) { return null; }
})()`;

const CLOSE_EXPR = `(function () {
  try {
    var mod = process.mainModule;
    if (mod && typeof mod.require === 'function') {
      mod.require.call(mod, 'inspector').close();
      return 'closed';
    }
    return 'no_mainModule';
  } catch (e) { return 'err'; }
})()`;

async function evalWith(target, expression) {
  const ws = await connect(target.webSocketDebuggerUrl);
  const send = makeSender(ws);
  try {
    await send("Runtime.enable", {});
    const r = await Promise.race([
      send("Runtime.evaluate", { expression, awaitPromise: true, returnByValue: true }),
      sleep(8000).then(() => null),
    ]);
    return r && r.result && r.result.result ? r.result.result.value : undefined;
  } finally {
    try {
      ws.close();
    } catch (e) {}
  }
}

// 关掉 inspector。注意：inspector 一关，这条 CDP 连接就断了，
// 回包可能永远不来 —— 所以**绝不能 await 它**，否则事件循环空转、进程静默退出。
async function requestClose(target, waitMs = 700) {
  let ws;
  try {
    ws = await connect(target.webSocketDebuggerUrl);
  } catch (e) {
    return false;
  }
  try {
    ws.send(
      JSON.stringify({
        id: 1,
        method: "Runtime.evaluate",
        params: { expression: CLOSE_EXPR, returnByValue: true },
      })
    );
    await sleep(waitMs);
  } catch (e) {
  } finally {
    try {
      ws.close();
    } catch (e) {}
  }
  return true;
}

async function closeInspector() {
  const t = await listTargets();
  if (!t) return false;
  return await requestClose(t);
}

// 返回 {state, kind}
async function ensurePortFree() {
  const t = await listTargets();
  if (!t) return { state: "free" };
  let kind;
  try {
    kind = await evalWith(t, PROBE_EXPR);
  } catch (e) {
    return { state: "foreign", kind: "unreachable" };
  }
  if (kind !== "ours") return { state: "foreign", kind: String(kind) };
  await closeInspector();
  for (let i = 0; i < 12; i++) {
    if (!(await listTargets())) return { state: "reclaimed" };
    await sleep(250);
  }
  return { state: "foreign", kind: "could_not_close" };
}

async function main() {
  if (!Number.isFinite(pid) || pid <= 0) return { ok: false, note: "bad_pid" };

  const { state: portState } = await ensurePortFree();
  if (portState === "foreign") {
    return { ok: false, note: "port_" + PORT + "_busy_foreign" };
  }

  try {
    process._debugProcess(pid);
  } catch (e) {
    return { ok: false, note: "attach_failed:" + (e && e.message) };
  }

  let target = null;
  for (let i = 0; i < 20; i++) {
    target = await listTargets();
    if (target) break;
    await sleep(250);
  }
  if (!target) return { ok: false, note: "no_inspector_target", portState };

  let outcome;
  try {
    const raw = await evalWith(target, GET_EXPR);
    outcome =
      raw && typeof raw === "string"
        ? { ok: true, payload: JSON.parse(raw), port: PORT, portState }
        : { ok: false, note: "not_main_process", port: PORT, portState };
  } catch (e) {
    outcome = { ok: false, note: "eval_error:" + (e && e.message), port: PORT, portState };
  }

  // 先把结果同步写出去，再清理端口 —— 顺序不能反，否则清理时进程可能提前结束
  emit(outcome);

  // 关键：无论成败都关掉 inspector，不在常驻客户端上留下 open 的调试端口
  await closeInspector();
  return null;
}

main()
  .then((early) => {
    if (early) emit(early);
    process.exit(0);
  })
  .catch((e) => {
    emit({ ok: false, note: "fatal:" + (e && e.message) });
    process.exit(0);
  });
