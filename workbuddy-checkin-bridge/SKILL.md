---
name: workbuddy-checkin-bridge
slug: workbuddy-checkin-bridge
displayName: WorkBuddy 签到 · 登录态桥接组件
version: 1.5.3
summary: 独立组件。由用户显式运行，把本机 WorkBuddy 客户端的登录态解密成标准 JSON 凭据，供 workbuddy-checkin 主包使用；只读凭据、不写回登录态，build key 只在内存中使用。
license: MIT
description: 仅当用户明确要求「让 WorkBuddy 签到脚本直接读取本机客户端登录态」时使用。用于生成主包所需的 accessToken JSON。会读取本机客户端会话文件、解密 at-rest 凭据；可选从运行中的客户端进程提取 build key（需 --allow-live-extract 显式开启）。不要自动运行，不要用于其它用途。
category: dev-programming
subCategories: [dev-script]
platforms: [windows]
allowed-tools: Bash, Read, Write
---

# WorkBuddy 签到 · 登录态桥接组件

这是 `workbuddy-checkin` 主包的**可选、独立**组件。主包刻意只接受「用户显式提供的 token」，不具备读取本机客户端凭据的能力；如果用户希望签到脚本直接用本机已登录的会话，就由本组件负责把会话转成标准 JSON。

**本组件不会被自动调用。** 必须由用户显式运行并显式同意。

## 它会做什么 / 不会做什么

会：

- 读取本机 WorkBuddy/CodeBuddy 客户端的会话文件（`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`，可用 `WB_AUTH_DIR` 覆盖）
- 解密其中 at-rest 加密的 `accessToken` / `refreshToken`（AES-256-GCM，实现见 `scripts/at_rest.py`）
- 输出一行 JSON：`{"accessToken","refreshToken","uid","domain"}`
- 可选：当没有可用 build key 缓存时，从**正在运行的客户端主进程**取一次 build key（需显式 `--allow-live-extract`）

不会：

- 写回或修改客户端的登录态文件、不留备份/临时文件
- 把 token、build key 打印到终端或写进日志
- 把 build key 写到磁盘（实时提取的 secret 只在内存里）
- 向任何外部网络地址发送数据（`buildkey_cdp.js` 只连本机 `127.0.0.1:9229` 的 V8 inspector 端口，且用完即关）
- 在客户端进程上留下开放的调试端口

## 显式同意

```bash
# 拒绝（默认）：不确认就直接退出，不读取任何凭据
python "<SKILL_DIR>/scripts/bridge.py"

# 同意 + 写到用户自己指定的文件
python "<SKILL_DIR>/scripts/bridge.py" --confirm --out C:/secure/token.json

# 无人值守（例如用户自己的计划任务）用环境变量同意
WB_BRIDGE_CONSENT=1 python bridge.py --out C:/secure/token.json
```

`--out` 由用户显式指定；写完后会尽量把权限收紧为仅当前用户可读。不指定 `--out` 时把一行 JSON 输出到 stdout，供管道消费。

## 与主包的契约（主包只接受两种显式凭据入口）

主包 `workbuddy-checkin` 从 1.5.2 起只接受**两种**显式凭据入口：环境变量 `WB_ACCESS_TOKEN`，
或 `WB_TOKEN_FILE` 指向的 JSON 文件。为保持主包零子进程，原先的
`WB_TOKEN_PROVIDER` 命令入口已移除 —— 本组件统一通过「写文件 + 主包读文件」对接：

```bash
# 1) 显式同意，把会话解密成 JSON 文件
python "<SKILL_DIR>/scripts/bridge.py" --confirm --out C:/secure/token.json

# 2) 主包读该文件完成签到
WB_TOKEN_FILE=C:/secure/token.json python "<MAIN_SKILL>/scripts/wb-checkin.py"
```

token 文件用完建议删除。若不想落盘，可省略 `--out`，bridge 会把一行 JSON 打到 stdout，
由用户自行决定如何处理（主包不会自己去执行 bridge，也不会读取 stdout）。

## build key 的来源与优先级

1. 环境变量 `WB_AT_REST_SECRET_KEY`
2. 用户自己已有的缓存文件 `at_rest_buildkey.json`（同目录或 `~/.workbuddy/scripts/`）
3. `--allow-live-extract`：调用 `scripts/buildkey_bootstrap.py` → `scripts/buildkey_cdp.js`

第 3 步会在目标进程上临时启用 V8 inspector 读取一个构建级常量，然后**无论成功失败都关闭** inspector（实现在 `buildkey_cdp.js` 的 `closeInspector()`）；端口被其它程序占用时会直接放弃，绝不误关别人的调试端口。它要求客户端正在运行且本机有 Node.js（可用 `WB_NODE` 指定），且 secret **不落盘**。

> 本包**不含**手动附加调试器的脚本。手动链路容易留下忘记关闭的 inspector，已整体移除；自动链路本身已覆盖该能力。

## 风险与处置

- 输出的 JSON 含明文 accessToken/refreshToken，等同账号登录凭证。不要提交到仓库、不要发给别人、用完即删
- `at_rest_buildkey.json` 是本机凭据的解密钥匙，不要外传；本包**不附带任何密钥**
- 实时提取会短暂触碰客户端进程内存，仅在用户显式开启时才发生；不需要它就不要加 `--allow-live-extract`
- 若客户端升级导致 `keyId mismatch`，用 `--allow-live-extract` 重新取一次内存 secret
- 从未登录过 WorkBuddy 的机器无法产出凭据

## 为什么拆成两个包

ClawHub 的安全扫描把「签到技能里附带凭据解密 + 进程调试提取能力」判为**声明目的与代码能力不符**。拆开后：

- 主包只做签到，代码能力和描述逐字对齐，可正常公开分发；
- 本组件专门做凭据桥接，用途、风险、同意流程全部写在最前面，由用户自行决定是否安装。

本组件只发布到 GitHub 与 SkillHub，**不提交 ClawHub**。
