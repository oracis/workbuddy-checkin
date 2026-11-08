// 从运行中的 WorkBuddy 客户端提取 at-rest build key（atRestSecretKey），写到 at_rest_buildkey.json。
//
// 用法:
//   1) 先给运行中的客户端主进程开调试端口（只开端口，不打断会话）:
//        node attach_debug.js <主进程PID>          -> 监听 127.0.0.1:9229
//      主进程 PID 可用 `tasklist | findstr WorkBuddy` 找（通常是占用内存最大的那个,
//      也等于环境变量 WORKBUDDY_STARTUP_PID）。
//   2) 提取:
//        node extract_buildkey.js [port] [输出json路径]
//
// 原理: build key 由客户端原生模块 electron.workbuddyStorage.loggerGet() 返回，
// 是构建级常量（同版本全安装一致），取出一次即可离线复用。
const fs = require("fs");
const path = require("path");

const port = process.argv[2] || "9229";
const outPath = process.argv[3] || path.join(__dirname, "at_rest_buildkey.json");

async function main() {
  const res = await fetch(`http://127.0.0.1:${port}/json/list`);
  const targets = await res.json();
  const t = targets.find((x) => x.type === "node") || targets[0];
  if (!t) throw new Error("没有可用的调试目标，先运行 attach_debug.js <pid>");

  const ws = await new Promise((resolve, reject) => {
    const s = new WebSocket(t.webSocketDebuggerUrl);
    s.addEventListener("open", () => resolve(s));
    s.addEventListener("error", reject);
  });

  let id = 0;
  const pending = new Map();
  ws.addEventListener("message", (ev) => {
    const m = JSON.parse(ev.data.toString());
    if (m.id && pending.has(m.id)) {
      pending.get(m.id).resolve(m);
      pending.delete(m.id);
    }
  });
  const send = (method, params) =>
    new Promise((resolve) => {
      const mid = ++id;
      pending.set(mid, { resolve });
      ws.send(JSON.stringify({ id: mid, method, params }));
    });

  await send("Runtime.enable", {});
  const expr =
    "process.mainModule.require('electron').workbuddyStorage.loggerGet()";
  const r = await send("Runtime.evaluate", {
    expression: expr,
    awaitPromise: true,
    returnByValue: true,
  });

  if (!r.result || r.result.exceptionDetails) {
    throw new Error("求值失败: " + JSON.stringify(r.result && r.result.exceptionDetails));
  }
  const payload = r.result.result.value;
  const obj = typeof payload === "string" ? JSON.parse(payload) : payload;
  if (!obj || !obj.atRestSecretKey) {
    throw new Error("返回体里没有 atRestSecretKey: " + JSON.stringify(obj).slice(0, 300));
  }

  const out = {
    _comment:
      "WorkBuddy 客户端 at-rest build key（atRestSecretKey）。构建级常量，同版本全安装一致，用于解密本地凭据信封。",
    version: obj.version || 1,
    atRestSecretKey: obj.atRestSecretKey,
    developerPublicKeyId: obj.atRestDeveloperPublicKey && obj.atRestDeveloperPublicKey.id,
    source: "electron.workbuddyStorage.loggerGet() via CDP",
    capturedAt: new Date().toISOString().slice(0, 10),
  };
  fs.writeFileSync(outPath, JSON.stringify(out, null, 2) + "\n", "utf8");
  console.log("written: " + outPath);
  console.log("atRestSecretKey = " + obj.atRestSecretKey);
  ws.close();
}

main().catch((e) => {
  console.error("FAILED: " + (e && e.message ? e.message : e));
  process.exit(1);
});
