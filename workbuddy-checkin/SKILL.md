---
name: workbuddy-checkin
description: 自动领取 WorkBuddy（原 CodeBuddy）每日签到积分。当用户说"领积分""自动领积分""签到""每日礼包""checkin""领取今日积分"，或定时任务要执行每日签到时使用。接口规格与脚本已固化，无需重新逆向客户端。
---

# WorkBuddy 每日签到领积分

直接调用 WorkBuddy 后端签到接口领取每日积分，无需打开客户端 UI。幂等：当天已领会立即跳过。

## 何时使用
- 用户要求"领积分 / 自动领积分 / 签到 / 每日礼包 / checkin"
- 定时任务触发每日签到时
- 用户问"积分怎么自动领 / 今天领了吗 / 连续签到几天了"

## 执行方式

脚本随本 skill 一起分发，位于 skill 目录下的 `scripts/wb-checkin.py`。
用 skill 基准目录拼出路径后执行（Windows 示例）：

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

脚本零依赖，只用 Python 标准库，任意 Python 3.8+ 均可运行。

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
| `[FAIL] 凭据加载失败` | 报错信息里已列出所有尝试过的目录。确认用户已登录客户端；非 Windows 平台可用 `WB_AUTH_DIR` 环境变量指定凭据目录 |
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

客户端把登录态以**明文 JSON** 存在共享数据目录下，脚本自动探测：

- **Windows**（已实测）：`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\`
- macOS（候选，未实测）：`~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/`
- Linux（候选，未实测）：`$XDG_DATA_HOME/CodeBuddyExtension/Data/Public/auth/`

要点：
- 目录常量名是 **`CodeBuddyExtension`**，不是 `WorkBuddyExtension`（源码常量 `EXTENSION_DATA_DIR_NAME`）
- 路径来自 `FileAuthenticationStorage.getAuthSavePath()` = `sharedDataPath/auth/<authenticationId>.info`
- 取**最新修改**的 `*.info`；形如 `xxx.2026-07-07T06-34-08-857Z.info` 的是历史备份，需排除
- 读 `auth.accessToken` / `account.uid` / `auth.domain`
- Electron 的 `session/Local Storage` 里**没有** token，不用去翻

## 注意

- 脚本只读取本地凭据文件，不做网络登录，也不会把 token 写到任何地方
- 建议配合定时任务每几小时跑一次：脚本幂等，多跑无副作用，可避免客户端不常开而漏签断连续奖励
- 若配 RRULE 定时：`BYHOUR` 只接受单个整数，`BYHOUR=9,21` 这种写法会被拒；要多时段请用 `FREQ=HOURLY;INTERVAL=n`
