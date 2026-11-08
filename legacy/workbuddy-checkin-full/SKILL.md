---
name: workbuddy-checkin
slug: workbuddy-checkin
displayName: WorkBuddy 每日签到领积分
version: 1.4.1
summary: 自动领取 WorkBuddy（原 CodeBuddy）每日签到积分，无需打开客户端 UI；支持新版加密凭据（at-rest 信封）解密，缺 build key 时自动从运行中的客户端提取（自愈）；token 过期能用 refreshToken 自动续期并安全写回（排他锁 + 竞态检测 + 原子替换）；附系统计划任务轮询方案（pythonw + 当日守卫），开机期间没领到就每 30 分钟重试、领到当天即收工。脚本幂等、零依赖。
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
| `--no-refresh` | 禁止自动续期登录态（只读本地凭据，排障用） |

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
| `[INFO] 已自动续期…` | 正常。脚本发现 token 邻近/已过期，自己换新了，无需干预。看后缀判断是否落盘：`并写回凭据文件` = 下次直接可用；`客户端在运行，本次不写回` = 仅本次生效，客户端会自己续 |
| `[FAIL]` + HTTP 401/403（且已尝试续期） | 续期也失败：多为 refreshToken 也过期（约 60 天未开客户端）。告诉用户「请打开一次 WorkBuddy 客户端重新登录，之后会自动恢复」。**不要**改脚本、动凭据文件、或尝试重新登录 |
| `[FAIL] 凭据加载失败` | 报错信息里已列出所有尝试过的目录与被跳过的文件原因。若含 `client_not_running`，让用户启动客户端后重跑；非 Windows 平台可用 `WB_AUTH_DIR` 指定凭据目录 |
| `[WARN] 自动提取 build key 失败` | 看 hint：`client_not_running` → 启动客户端重跑；`node_not_found` → 装 Node.js 或设 `WB_NODE` |
| 报 `keyId mismatch` 且自愈也没救回来 | 客户端换了 build key 且提取失败。先确认客户端在跑、Node 可用；仍不行则按上文手动链路排查 |
| `[FAIL]` + 其它 HTTP 码 | 打印原始错误让用户判断，可能是活动规则变更。不要自行猜测重试 |
| 脚本文件缺失 | 按下方接口规格重建，或重新拉取本 skill 仓库 |

## 接口规格（逆向自客户端 `app.asar`，已实测可用）

- 后端 `endpoint`：`https://copilot.tencent.com`（取自 `cli/product.json -> endpoint`，可用 `WB_ENDPOINT` 覆盖）
- 查状态：`POST /v2/billing/meter/checkin-activity-status`，body `{}`
- 领积分：`POST /v2/billing/meter/daily-checkin`，body `{}`
- 续期（见下节）：`POST /v2/plugin/auth/token/refresh`，body `{}`
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

## 登录态自动续期（1.3.0 起）

`accessToken` 只有 **30 天**寿命（`expiresIn` 2592000），`refreshToken` **60 天**。原先 token 一过期脚本就只能报 401、等用户开客户端；现在会自己续。

### 触发时机

不靠定时，靠**两层判断**：

1. **事前预判**：读凭据时解析 JWT 的 `exp`，若剩余 < 30 分钟，先续期再签到（避免签到途中恰好过期）。
2. **事后兜底**：任意接口返回 401/403，立即续期并重试该请求一次。

token 仍然有效时，不发任何额外请求 —— 正常路径零开销。

### 续期接口（逆向自 `app.asar`，已实测）

```
POST /v2/plugin/auth/token/refresh
X-Refresh-Token: <refreshToken 明文>
X-Auth-Refresh-Source: plugin
X-User-Id / X-Domain: 同签到接口
body: {}
```

- `prefixPath` 来自 `cli/product.json -> authentication.attributes.prefixPath`（值 `/plugin`）
- **`/v2/auth/token/refresh` 是 404**，漏了 `/plugin` 必失败
- 响应 `data` 字段：`accessToken` / `refreshToken` / `expiresIn` / `refreshExpiresIn` / `tokenType` / `domain` / `sessionState` / `scope` / `notBeforePolicy`
- `sessionState` 每次刷新都会变，必须一并写回，否则客户端会认为会话不一致

### 写回策略（安全边界）

新 token 要落盘才有意义，但凭据文件是**客户端的资产**，不能写坏。四道保护：

| 保护 | 做法 |
|---|---|
| 客户端在跑就不写 | 客户端可能随时重写该文件。此时只把新 token 用于本次运行，不落盘（`[INFO] …客户端在运行，本次不写回`） |
| 竞态检测 | 写前重读文件，若 `accessToken` 已和读入时不同，说明客户端抢先更新了 → 放弃写回（`凭据已被客户端更新`） |
| 排他锁 | 同目录 `O_EXCL` 锁文件，防两个定时任务并发写；锁超过 120s 视为陈旧可接管 |
| 原子替换 + 备份 | 写 `.wb-tmp` → `os.replace()`；写前留一份 `.wb-bak` |

只改 `auth` 里的 token 与时间戳字段，其余原样保留（`account` / `accounts` / `allAccounts`、`nickname`/`phoneNumber` 等其它加密字段都不碰）。
字段加密形态跟着原字段走：原来是信封就 `at_rest.seal()` 加密，原来是明文就写明文。

> **轮换语义实测**：旧 `refreshToken` 在新 token 签发后**仍然可用**（连打两次均 200），
> 所以从外部续期不会让正在运行的客户端掉线。这也是"客户端在跑时不写回"能成立的前提。

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
自测覆盖：FIPS-197 AES-256 向量、GCM spec 向量（空 AAD / 带 AAD）、篡改检测、加解密双向往返、nonce 随机性、错密钥拒绝，并与 OpenSSL 后端做 200 组随机交叉校验。

同一模块也提供**加密侧**（续期写回 token 用）：

- `seal_field(plaintext, secret=None)` → 信封 base64（字段名/顺序与客户端一致）
- `seal(value, secret=None)` → 完整 `{"$wbEncrypted":1,"envelope":...}`，是 `unwrap()` 的逆操作

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

## 调度方式：客户端定时任务 vs 系统计划任务

**关键区别（2026-11-08 实测踩坑）**：WorkBuddy 的「定时任务」是 `LocalAutomationScheduler` 本地调度，
只有**客户端进程在运行**时才会触发。客户端没开 = 任务完全不执行；客户端被关在任务运行中 = 该次 run 会挂住，
下次启动时被判定 `ErrorAutomation-MissedWindow` 并直接放弃（实测挂过 **144.9 小时**，导致连续 5 天漏签）。

所以要真正做到「不开客户端也能领」，必须用操作系统级计划任务，脚本本身零客户端依赖。

### Windows：schtasks 轮询 + 当日守卫（推荐，已在本机部署为 `WorkBuddyCheckin`）

目标语义：**开机期间按频率轮询，今天没领到就继续试，领到了当天立刻收工**（一天只领一次）。
靠一个守卫脚本 `wb-checkin-guard.py` + `checkin-done.json` 标记实现，用 `pythonw.exe` 启动，全程无窗口。

1. 守卫脚本随包提供：`scripts/wb-checkin-guard.py`，把它和 `wb-checkin.py` 一起放到
   `~/.workbuddy/scripts/` 下（该目录与 `~/.workbuddy/binaries/` 同级，守卫里的解释器相对路径才成立）。
   核心逻辑：

```python
if read_mark().get("date") == today():
    return 0                      # 今天已领，本周期直接退出：不联网、不写日志、约 380ms
out = run([PY, "-u", "wb-checkin.py"])          # 未领才真的跑
if "[OK]" in out or "[SKIP]" in out:            # [SKIP] = 客户端/别处已领，同样算今天完成
    write_mark({"date": today(), "total": ..., "result": headline})
else:
    log("FAILED\n" + out)                       # [FAIL] 不写标记 → 下个周期自动重试
```

2. 任务用 `CalendarTrigger` + `Repetition`（`Interval=PT30M`，`Duration=PT23H55M`，`DaysInterval=1`），
   `StartWhenAvailable=true`（关机/休眠错过就开机补跑）。用 XML 导入：

```bash
# XML 必须是 UTF-16，<StartBoundary> 写今天或更早的 00:05，否则当天的轮询窗口不生效
schtasks //create //tn "WorkBuddyCheckin" //xml "C:\Users\DELL\.workbuddy\scripts\WorkBuddyCheckin.xml" //f
schtasks //query  //tn "WorkBuddyCheckin" //v //fo LIST   # 确认 Next Run 在 30 分钟内
schtasks //run    //tn "WorkBuddyCheckin"                 # 立即触发验证
```

3. 注意事项
   - **一定要用 `pythonw.exe`**：`python.exe` / `cmd.exe` 是控制台程序，每次触发都会闪一下黑窗，
     `pythonw.exe` 无窗口。想用 VBS/wscript 隐藏也不行——LOLBin 会被 WorkBuddy 安全策略拦截
   - `schtasks` 在 Git Bash 里必须写双斜杠 `//create`，否则 `/create` 会被 MSYS 当路径转换
   - 默认只在用户已登录 Windows 时运行（不需要 WorkBuddy 登录，但需要 Windows 会话）
   - 删除：`schtasks //delete //tn "WorkBuddyCheckin" //f`；删掉后记得把 WorkBuddy 定时任务恢复
   - 日志 `checkin-os.log`（已领的轮询不留痕，只记真正执行/失败），标记 `checkin-done.json`
   - 想手动跑一次看输出：`python wb-checkin.py`（不要走守卫，守卫会被标记挡掉）

**建议**：系统轮询作主，WorkBuddy 客户端定时任务可**暂停**（否则一天里又会被触发几次，与"领到就收工"冲突）。
但**不要在两个不同工作区各建一个同名任务**——会双跑，且两边回报的累计数据可能对不上（实测遇到过），排查时极易误判。

## 注意

- 脚本不需要客户端**正在运行**：只要本机有凭据文件 + build key 缓存，就能离线签到
- 「从未登录过」无法签到 —— 凭据来自登录，凭空造不出来
- 续期用 `refreshToken`，全程不涉及账号密码，也不做交互式登录
- 写回凭据时只动 `auth` 里的 token/时间戳，**客户端在运行时不写回**（详见"写回策略"）
- build key 等同本机凭据的解密钥匙，`at_rest_buildkey.json` 不要外传；**本 skill 不附带任何密钥**，
  每个用户由自愈链路提取自己客户端的那一份
- 自愈需要「客户端在运行 + 本机有 Node.js」；两者都没有时不会崩，会优雅降级到明文 token 回退
- **执行方式**：主流程只用 Python 标准库；仅自愈阶段会调用一次 `node`（`buildkey_cdp.js`）
- 调度见上节「调度方式」：**优先用系统计划任务轮询 + 当日守卫**，别只依赖 WorkBuddy 客户端定时任务
  （客户端不运行就完全不触发，实测连续漏签 5 天）。脚本幂等，未领时多跑无副作用，领到后守卫会当天静默收工
- 若仍要配 WorkBuddy 内置 RRULE 定时：`BYHOUR` 只接受单个整数，`BYHOUR=9,21` 会被拒；
  要多时段请用 `FREQ=HOURLY;INTERVAL=n`
- 凭据写回会在同目录留下 `*.wb-bak`（写前备份），可随时手工回滚；`*.wb-lock` / `*.wb-tmp` 正常不会残留
