---
name: workbuddy-checkin
slug: workbuddy-checkin
displayName: WorkBuddy 每日签到领积分
version: 1.2.0
summary: 自动领取 WorkBuddy（原 CodeBuddy）每日签到积分，无需打开客户端 UI；支持新版加密凭据（at-rest 信封）解密，缺 build key 时自动从运行中的客户端提取（自愈），脚本幂等、零依赖，配合定时任务避免漏签。
license: MIT
description: 自动领取 WorkBuddy（原 CodeBuddy）每日签到积分。当用户说"领积分""自动领积分""签到""每日礼包""checkin""领取今日积分"，或定时任务要执行每日签到时使用。接口规格与脚本已固化，无需重新逆向客户端。
category: office-efficiency
subCategories: [office-automation]
platforms: [windows, macos, linux]
---

# WorkBuddy 每日签到领积分

直接调用 WorkBuddy 后端签到接口领取每日积分，无需打开客户端 UI。幂等：当天已领会立即跳过。

## 何时使用
- 用户要求"领积分 / 自动领积分 / 签到 / 每日礼包 / checkin"
- 定时任务触发每日签到时
- 用户问"积分怎么自动领 / 今天领了吗 / 连续签到几天了"

## 执行方式

脚本随本 skill 一起分发，位于 skill 目录下的 `scripts/`。用 skill 基准目录拼出路径后执行（Windows 示例）：

```bash
python "<SKILL_DIR>/scripts/wb-checkin.py"
```

参数：

| 参数 | 作用 |
|---|---|
| 无参数 | 正常签到（幂等，已领则 `[SKIP]`） |
| `--status` | 只查状态不领取 |
| `--json` | 输出原始状态 JSON（便于程序消费） |
| `--force` | 强制调领取接口（调试用） |

退出码：`0` = 成功领取 / 今日已领 / 活动未开启；`1` = 失败（鉴权失效、凭据缺失、接口异常）。

脚本零依赖，只用 Python 标准库，任意 Python 3.8+ 均可运行（AES-GCM 为内置纯 Python 实现，见下）。

### 脚本清单

| 文件 | 作用 | 何时用 |
|---|---|---|
| `wb-checkin.py` | 签到主流程（含自愈入口） | 每天都用 |
| `at_rest.py` | at-rest 信封解密（零依赖 AES-GCM） | 被主流程 import |
| `buildkey_bootstrap.py` | 自愈：定位客户端主进程并提取 build key | 主流程解密失败时自动调用 |
| `buildkey_cdp.js` | 单 PID 的 CDP 提取（由上方 Python 调用） | 同上 |
| `attach_debug.js` / `extract_buildkey.js` | 手动提取链路（调试/排障用） | 自愈失败时人工排查 |

## 输出解读

- `[OK]` — 本次成功领取，会带上本次积分与累计积分
- `[SKIP]` — 今天已领过，或当前无进行中活动。**属正常情况，如实汇报即可，不要重试、不要加 `--force`**
- `[INFO]` — `--status` 模式的正常结束
- `[FAIL]` — 失败，按下方故障处置

汇报给用户时保持简短：活动名称、连续签到天数、累计积分、本次是否领取、领了多少。不要复述实现细节。

## 故障处置

| 现象 | 处置 |
|---|---|
| `[FAIL]` + HTTP 401/403 | `accessToken` 过期。告诉用户「请打开一次 WorkBuddy 客户端让它刷新登录态，之后会自动恢复」。**不要**改脚本、动凭据文件、或尝试重新登录 |
| `[FAIL] 凭据加载失败` | 报错信息里已列出所有尝试过的目录与被跳过的文件原因。若含 `client_not_running`，让用户启动客户端后重跑；非 Windows 平台可用 `WB_AUTH_DIR` 指定凭据目录 |
| `[WARN] 自动提取 build key 失败` | 看 hint：`client_not_running` → 启动客户端重跑；`node_not_found` → 装 Node.js 或设 `WB_NODE` |
| 报 `keyId mismatch` 且自愈也没救回来 | 客户端换了 build key 且提取失败。先确认客户端在跑、Node 可用；仍不行则按上文手动链路排查 |
| `[FAIL]` + 其它 HTTP 码 | 打印原始错误让用户判断，可能是活动规则变更。不要自行猜测重试 |
| 脚本文件缺失 | 按下方接口规格重建，或重新拉取本 skill 仓库 |

## 接口规格（逆向自客户端 `app.asar`，已实测可用）

- 后端 `endpoint`：`https://copilot.tencent.com`（取自 `cli/product.json -> endpoint`，可用 `WB_ENDPOINT` 覆盖）
- 查状态：`POST /v2/billing/meter/checkin-activity-status`，body `{}`
- 领积分：`POST /v2/billing/meter/daily-checkin`，body `{}`
- 请求头（缺 `Authorization` 必 401）：
  - `Authorization: Bearer <accessToken>`
  - `X-User-Id: <uid>`
  - `X-Domain: <auth.domain>`
  - `Content-Type: application/json`、`Accept: application/json`
  - 源码中另有可选的 `X-Device-Token`（图灵盾），实测不带也能成功
- 响应：`{code: 0, msg: "OK", requestId, data: {...}}`

`data` 关键字段：

| 字段 | 含义 |
|---|---|
| `active` | 活动是否进行中 |
| `today_checked_in` | 今日是否已签（**幂等判断依据**） |
| `streak_days` | 连续签到天数 |
| `daily_credit` / `today_credit` | 每日 / 今日积分 |
| `total_credits` | 活动内累计积分 |
| `week_checkin_days` | 本周已签天数 |
| `week_progress` | 本周进度（bool 数组，用于 ○/● 渲染） |
| `activity_name` / `theme_name` | 活动名 / 主题名 |
| `season` | 期数 |
| `start_time` / `end_time` | 活动时间范围 |

## 凭据位置

客户端把登录态存在共享数据目录下的 `*.info` 里，脚本自动探测：

- **Windows**（已实测）：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\`
- macOS（候选，未实测）：`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/`
- Linux（候选，未实测）：`$XDG_DATA_HOME/CodeBuddyExtension/Data/Public/auth/`

要点：
- 目录常量名是 **`CodeBuddyExtension`**，不是 `WorkBuddyExtension`（源码常量 `EXTENSION_DATA_DIR_NAME`）
- 路径来自 `FileAuthenticationStorage.getAuthSavePath()` = `sharedDataPath/auth/<authenticationId>.info`
- `account.uid` 是明文，即使 token 加密也能读到，用它锁定目标账号
- 形如 `xxx.2026-07-07T06-34-08-857Z.info` 的是历史备份；当前会话文件优先，但备份可作回退
- Electron 的 `session/Local Storage` 里**没有** token，不用去翻

## 凭据加密与 build key（2026-09-19 起）

新版客户端不再写明文 token，而是 at-rest 加密信封：

```json
"accessToken": { "$wbEncrypted": 1, "envelope": "<base64 of JSON>" }
```

信封结构（suite `sym-v1`，AES-256-GCM）：

```
envelope = { suite:1, keyId:"<16hex>", nonce:"<b64>", authTag:"<b64>", ciphertext:"<b64>" }
k0       = SHA256(utf8(atRestSecretKey))      # 32 字节
keyId    = SHA256(k0)[:16]                    # 必须等于 envelope.keyId
AAD      = "WB-AAD\0" + [1] + L("WBEV1") + L("sym-v1") + u32(suite) + L(keyId) + [2] + [0] + [0]
明文      = AES-256-GCM(k0, nonce, ciphertext||authTag, aad)
```

解密由同目录 `at_rest.py` 完成（零依赖，内置纯 Python AES-GCM；若环境有 `cryptography` 则优先用它）。
自测覆盖：FIPS-197 AES-256 向量、GCM spec 向量（空 AAD / 带 AAD）、篡改检测，并与 OpenSSL 后端做 200 组随机交叉校验。

**build key 是构建级常量**（同版本全安装一致），取出一次即可离线复用：

- 缓存文件 `at_rest_buildkey.json`（放在 `~/.workbuddy/scripts/`，或脚本同目录）
- 也可用环境变量 `WB_AT_REST_SECRET_KEY` 直接给
- 查找顺序：环境变量 → 脚本同目录 → `~/.workbuddy/scripts/`

重新提取（客户端升级导致 `keyId mismatch` 时）—— **正常不需要手动做**，脚本会自愈：

```bash
# 自动（推荐）：脚本在解密失败时会自己走这条路
python "<SKILL_DIR>/scripts/buildkey_bootstrap.py"

# 手动（同一链路的显式版）：
tasklist | findstr WorkBuddy          # 1) 找主进程 PID（内存占用最大的那个）
node "<SKILL_DIR>/scripts/attach_debug.js" <PID>   # 2) 附加调试端口（只读）
node "<SKILL_DIR>/scripts/extract_buildkey.js" 9229  # 3) 提取并写 at_rest_buildkey.json
```

> 注：`--inspect` 启动新实例会被客户端单实例锁挡住，必须**附加到已运行的进程**。

### 自愈链路（1.2.0）

```
解密失败(缺 key / keyId 不匹配)
   -> buildkey_bootstrap.py  (Python)
        - 用 Windows Toolhelp32 枚举 WorkBuddy 进程，按「父进程不属于本应用」挑出主进程
        - 逐个候选 PID 调用 buildkey_cdp.js  (Node)
             - 先把 9229 端口腾干净（残留 inspector 会先关掉；别人的则直接放弃）
             - process._debugProcess(pid) 附加，求值 workbuddyStorage.loggerGet()
             - **无论成败都关掉 inspector**，不在常驻客户端上留 open 的调试端口
        - 写入 at_rest_buildkey.json
   -> 重试解密 -> 正常签到
```

前提与降级：

| 前提 | 不满足时 |
|---|---|
| 客户端**正在运行** | 报 `client_not_running`，提示先启动客户端 |
| 本机装了 **Node.js**（或用 `WB_NODE` 指定） | 报 `node_not_found` |
| 解密仍失败 | 回退历史明文 token；再不行才报错 |

自愈只在**解密失败时**触发，正常路径零开销。**设计上刻意不打包任何密钥** ——
每个用户提取自己客户端的 build key，所以客户端升级后仍能自动跟上，不会随版本失效。

> 实现上的两个坑（改这块代码时务必注意）：
> 1. `process._debugProcess(pid)` **固定**使用默认端口 9229，无法指定；所以附加前必须先确保该端口空闲。
> 2. 关闭 inspector 会让那条 CDP 连接断开、**回包永远不来**。若 `await` 它，事件循环会空转让 Node 静默退出（rc=0、输出全丢）。所以结果要先同步写出，关闭动作不能 await 回包。

## 凭据选取顺序（脚本内部）

1. 同账号（uid 匹配最新文件）优先
2. 未过期（`auth.expiresAt`）优先
3. 当前会话文件优先于历史备份
4. 再按文件修改时间倒序

加密信封走 `at_rest.unwrap()` 解密；**解密失败会自动回退到目录里的历史明文 token**，
所以即使 build key 失效（客户端升级换 key），只要还有未过期的明文备份就仍能签到。

## 注意

- 脚本只读取本地凭据文件，不做网络登录，也不会把 token 写到任何地方
- build key 等同本机凭据的解密钥匙，`at_rest_buildkey.json` 不要外传；**本 skill 不附带任何密钥**，
  每个用户由自愈链路提取自己客户端的那一份
- 自愈需要「客户端在运行 + 本机有 Node.js」；两者都没有时不会崩，会优雅降级到明文 token 回退
- **执行方式**：主流程只用 Python 标准库；仅自愈阶段会调用一次 `node`（`buildkey_cdp.js`）
- 建议配合定时任务每几小时跑一次：脚本幂等，多跑无副作用，可避免客户端不常开而漏签断连续奖励
- 若配 RRULE 定时：`BYHOUR` 只接受单个整数，`BYHOUR=9,21` 这种写法会被拒；要多时段请用 `FREQ=HOURLY;INTERVAL=n`
