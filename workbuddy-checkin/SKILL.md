---
name: workbuddy-checkin
slug: workbuddy-checkin
displayName: WorkBuddy 每日签到领积分
version: 1.5.3
summary: 用用户显式提供的 accessToken 调用 WorkBuddy 官方签到接口，领取每日积分并汇报结果；脚本幂等、零依赖（仅 Python 标准库），只访问官方白名单域名，不读取也不写入本机客户端凭据。
license: MIT
description: 仅在用户明确指 WorkBuddy（原 CodeBuddy）客户端的每日签到积分时使用。触发语句如「领取 WorkBuddy 今日积分」「WorkBuddy 签到」「WorkBuddy 每日礼包」「WorkBuddy checkin」，或定时任务明确要求执行 WorkBuddy 签到。若用户只说「签到」「领积分」而未指明是哪个平台或服务，必须先询问确认，不要擅自执行。
category: office-efficiency
subCategories: [office-automation]
platforms: [windows, macos, linux]
allowed-tools: Bash, Read, Write
---

# WorkBuddy 每日签到领积分

调用 WorkBuddy 官方签到接口领取当日积分。幂等：当天已领会立即跳过，不重复领取。

## 能力边界（重要）

这个技能**只做一件事**：拿一个由**用户显式提供**的 `accessToken`，调用官方签到接口，输出结果。

它**不会**：

- 扫描、读取或解密本机客户端的登录凭据文件
- 读取其他进程的内存、附加调试器、提取任何密钥
- 写入或修改客户端的登录态文件、备份文件
- 向白名单以外的域名发送任何数据
- 启动任何外部命令或 token provider 子进程

请求只发往 `copilot.tencent.com` 或 `www.workbuddy.cn`，强制 HTTPS、默认 443 端口、禁止自动重定向（防止 Bearer token 被带到第三方）。**白名单是硬约束，没有任何环境变量或开关可以放行到其它主机**；`WB_ENDPOINT` 只能在这两个官方域名之间切换。token 续期后的新凭据只在本次进程内使用，**不落盘**；如需持久化，由提供凭据的那一侧负责。

需要「直接从本机 WorkBuddy 客户端会话取得 token」的用户，请自行安装独立的 `workbuddy-checkin-bridge` 组件（单独分发，不在本包内）。是否安装、何时运行完全由用户决定。

## 运行时能力说明（为什么需要这些能力）

本技能是一个**标准库 CLI 脚本**，由 Agent 通过 `Bash` 一次性调用，不是一个常驻服务。它确实用到两类系统能力，二者都是完成签到的**必要条件**，且都被上文约束到最小范围：

| 能力 | 为什么必要 | 约束 |
|---|---|---|
| 读环境变量 | `accessToken` 由用户显式放在 `WB_ACCESS_TOKEN` / `WB_TOKEN_FILE` 里传入；这是唯一的凭据入口，不读就无从拿到 token | 只读这几个白名单变量名；不枚举环境、不回显到日志 |
| 访问网络 | 签到接口在官方后端，必须 POST 才能查询和领取 | 域名白名单 + 强制 HTTPS + 443 + 禁重定向 + 校验响应最终 host；Bearer 头只带用户自己给的 token |

`allowed-tools` 保持 `Bash, Read, Write` 的最小集合：本技能不调用任何额外的宿主工具、不申明超出实际用途的能力，也不通过扩大工具预批准来粉饰扫描结论。把能力范围写清楚，是为了让审核者能逐条核对「声明」与「代码」是否一致，而不是请求更多权限。

## 何时使用

- 用户明确要求「领取 WorkBuddy 今日积分 / WorkBuddy 签到 / WorkBuddy 每日礼包」
- 定时任务明确写着执行 WorkBuddy 签到
- 用户问「WorkBuddy 积分今天领了吗 / 连续签到几天了」

**歧义时先问**：只说「签到」「领积分」「checkin」而没有指明 WorkBuddy，先确认是哪个平台，不要直接跑。

## 凭据输入（二选一，均由用户主动设置）

主脚本通过同目录 `token_provider.py` 读取，优先级从高到低：

| 入口 | 环境变量 | 说明 |
|---|---|---|
| 1. 直接给 token | `WB_ACCESS_TOKEN` | 明文 JWT。可另配 `WB_REFRESH_TOKEN`、`WB_USER_ID`、`WB_DOMAIN` |
| 2. 指定 JSON 文件 | `WB_TOKEN_FILE` | 指向用户自己写的 `{"accessToken": "...", "refreshToken": "...", "uid": "...", "domain": "..."}` |

文件入口会拒绝符号链接 / 重解析点，防止读到被链接替换的目标。两者都没有时脚本会明确报错并列出上述指引，**绝不回退去翻系统里的其它文件**。

> 推荐：直接从本机客户端会话取 token 的用户，先运行 `workbuddy-checkin-bridge` 写出 JSON 文件，再用入口 2 读取。本主包全程零子进程。

## 执行方式

```bash
python "<SKILL_DIR>/scripts/wb-checkin.py"
```

| 参数 | 作用 |
|---|---|
| 无参数 | 正常签到（幂等，已领则 `[SKIP]`） |
| `--status` | 只查状态不领取 |
| `--json` | 输出原始状态 JSON |
| `--force` | 强制调领取接口（调试用） |
| `--no-refresh` | 禁止自动续期 |

退出码：`0` = 成功领取 / 今日已领 / 活动未开启；`1` = 失败（凭据缺失、鉴权失效、接口异常）。仅需 Python 3.8+ 标准库。

## 输出解读

- `[OK]` 本次成功领取，带本次积分与累计积分
- `[SKIP]` 今天已领过，或当前无进行中活动。**属正常，如实汇报，不要重试、不要加 `--force`**
- `[INFO]` 状态提示（如仅查询、自动续期）
- `[FAIL]` 失败，按下方处置

汇报给用户时保持简短：活动名称、连续签到天数、累计积分、本次是否领取、领了多少。

## 故障处置

| 现象 | 处置 |
|---|---|
| `[FAIL] 凭据加载失败` | 按报错里的两条指引任选其一提供凭据。本技能不会自己去客户端找登录态 |
| `[FAIL] endpoint 校验失败` | `WB_ENDPOINT` 只允许填两个官方域名之一、必须 https、只能用 443 端口。白名单是硬约束，没有放行开关；请改回官方域名 |
| `[FAIL]` + HTTP 401/403 | accessToken 过期且续期未成功。请重新提供有效凭据，或打开一次 WorkBuddy 客户端刷新登录态。不要改脚本、不要动凭据文件 |
| `[INFO] 已自动续期…` | 正常。新 token 仅本次运行生效，未写入任何文件 |
| `[FAIL]` + 其它 HTTP 码 | 打印原始错误让用户判断，可能是活动规则变更。不要自行猜测重试 |

## 接口规格

- 后端：`https://copilot.tencent.com`（备 `https://www.workbuddy.cn`）。`WB_ENDPOINT` 只能在这两个之间切换，白名单硬编码在脚本里，无放行开关
- 查状态：`POST /v2/billing/meter/checkin-activity-status`，body `{}`
- 领积分：`POST /v2/billing/meter/daily-checkin`，body `{}`
- 续期：`POST /v2/plugin/auth/token/refresh`，body `{}`（`/v2/auth/token/refresh` 是 404，前缀 `/plugin` 不可省）
- 请求头：`Authorization: Bearer <accessToken>`、`X-User-Id: <uid>`、`X-Domain: <domain>`、`Content-Type/Accept: application/json`
- 续期额外头：`X-Refresh-Token: <refreshToken>`、`X-Auth-Refresh-Source: plugin`
- 响应：`{code: 0, msg: "OK", requestId, data: {...}}`

`data` 关键字段：`active`、`today_checked_in`（幂等判断依据）、`streak_days`、`daily_credit` / `today_credit`、`total_credits`、`week_checkin_days`、`week_progress`、`activity_name` / `theme_name`、`season`、`start_time` / `end_time`。

## 调度：一天只领一次

主脚本本身幂等，但要做到「开机期间没领到就按频率重试、领到了当天不再跑」，请用随包提供的 `scripts/wb-checkin-guard.py`：

- 守卫先读 `checkin-done.json`；标记日期是今天就直接退出（不联网、不写日志，约 0.4 秒）
- 未领才执行主脚本；输出 `[OK]` 或 `[SKIP]` 就写标记，`[FAIL]` 不写标记、下个周期自动重试
- 日志 `checkin-os.log` 只记真正执行或失败
- 执行前对解释器、脚本及其所在目录逐段校验，拒绝符号链接 / Windows reparse point；解释器只从两个显式可信候选中选取
- marker / 日志保留**原始词法路径**，链接检查先于 `realpath`（否则指向外部文件的链接会被解析成看似正常的目标），解析后还要求目标仍在技能目录内；读写走 `O_NOFOLLOW` + fd 复核，Windows 无该标志时改用 reparse point 属性位
- 子进程的 argv 全是代码内常量、`shell=False`

Windows 可在「任务计划程序」里建一个每天 00:05 起、每 30 分钟重复、错过则开机补跑的任务，动作为 `pythonw.exe` 运行该守卫脚本（无窗口）。任务定义 XML 需 UTF-16 编码、`<StartBoundary>` 不晚于当天。如需停用或卸载，请在 Windows 任务计划程序界面操作该任务，不要用脚本拼命令删除。

> 实测教训：WorkBuddy 客户端内置的定时任务是本地调度，**客户端进程不运行就完全不触发**；曾因一次 run 挂住 144.9 小时导致连续 5 天漏签。要脱离客户端运行，必须用操作系统级计划任务。

## 注意

- 从未登录过 WorkBuddy 无法签到 —— 凭据来自登录，凭空造不出来；本技能不负责获取凭据
- 续期只用 `refreshToken`，全程不涉及账号密码、不做交互式登录
- 凭据属敏感信息：不要写进日志、不要提交到仓库、不要在对话里回显完整 JWT
- 建议给 `WB_TOKEN_FILE` 指向的文件设置仅当前用户可读的权限；用完可删除
- 若在多个工作区各建一个同名系统任务，会双跑且两边回报的累计数据对不上，排查时极易误判
