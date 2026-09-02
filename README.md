# workbuddy-checkin

> WorkBuddy（原 CodeBuddy）每日签到自动领积分 Skill —— 幂等、零依赖、可配定时任务。

官方积分入口需要手动点「领取今日礼包」，客户端没开着就会漏签、断掉连续签到奖励。
这个 Skill 直接调用客户端自身使用的后端签到接口，让 AI 助手（或系统定时任务）帮你自动领。

- **幂等**：先查当天状态，已领则立即跳过，重复运行零副作用
- **零依赖**：只用 Python 标准库，无需 `pip install`
- **不碰密码**：只读取客户端已有的本地登录态，不做网络登录，不存储任何凭据
- **可定时**：配合 WorkBuddy 定时任务或系统计划任务，每几小时跑一次即可

## 效果

```
$ python workbuddy-checkin/scripts/wb-checkin.py
活动        : 开学季（Buddy加油站）
期数        : 第 8 期
时间范围      : 2026-09-01 00:00:00 ~ 2026-09-15 23:59:59
已签到天数     : 3（本周 3 天）
累计积分      : 300
今日积分      : 100
本周进度      : ○●●●○○○
[SKIP] 今天已经领过了，无需重复领取（今日 +100 积分）
```

## 安装

### 方式一：作为 WorkBuddy Skill（推荐）

把 `workbuddy-checkin/` 目录整个复制到用户级 skills 目录：

```bash
git clone https://github.com/oracis/workbuddy-checkin.git
cd workbuddy-checkin

# Windows (Git Bash)
cp -r workbuddy-checkin ~/.workbuddy/skills/

# macOS / Linux
cp -r workbuddy-checkin ~/.workbuddy/skills/
```

或用附带的安装脚本：

```bash
# Windows PowerShell
./install.ps1

# macOS / Linux / Git Bash
bash install.sh
```

装好后直接对 WorkBuddy 说「**帮我领积分**」即可，助手会自动加载本 Skill 完成签到。

安装到项目级（团队共享）则复制到 `<项目>/.workbuddy/skills/`。

### 方式二：当普通脚本用

不用 Skill 机制也行，直接跑：

```bash
python workbuddy-checkin/scripts/wb-checkin.py
```

## 用法

| 命令 | 作用 |
|---|---|
| `python wb-checkin.py` | 正常签到（幂等，已领则跳过） |
| `python wb-checkin.py --status` | 只查状态，不领取 |
| `python wb-checkin.py --json` | 输出原始状态 JSON，便于脚本消费 |
| `python wb-checkin.py --force` | 强制调用领取接口（调试用） |

退出码：`0` = 成功领取 / 今日已领 / 活动未开启；`1` = 失败。

环境变量：

| 变量 | 说明 |
|---|---|
| `WB_AUTH_DIR` | 手动指定凭据目录（自动探测失败时用） |
| `WB_ENDPOINT` | 覆盖后端地址，默认 `https://copilot.tencent.com` |

## 配定时任务

### WorkBuddy 内置定时任务

新建一个每 8 小时触发的循环任务，prompt 写：

```
调用 workbuddy-checkin skill 完成 WorkBuddy 每日签到领积分。
脚本幂等：已签到输出 [SKIP]，未签到才领（输出 [OK]），失败输出 [FAIL]。
用一到两行汇报：活动名称、连续签到天数、累计积分、本次是否领取。
若 [FAIL] 且提示鉴权失败，告知用户打开一次 WorkBuddy 客户端刷新登录态即可。
```

> ⚠️ RRULE 的 `BYHOUR` 只接受**单个整数**，`BYHOUR=9,21` 会被直接拒掉。
> 要多时段请用 `FREQ=HOURLY;INTERVAL=8`。

**为什么每 8 小时而不是每天一次？** 单点触发要求那一刻客户端/服务在跑，容易漏签断掉连续奖励。脚本幂等，多跑只是多一次几十毫秒的状态查询。

### Windows 计划任务

```powershell
schtasks /create /tn "WorkBuddy Checkin" /tr "python C:\path\to\wb-checkin.py" /sc hourly /mo 8
```

### cron（macOS / Linux）

```cron
0 */8 * * * /usr/bin/python3 /path/to/wb-checkin.py >> /tmp/wb-checkin.log 2>&1
```

## 工作原理

逆向自客户端 `resources/app.asar`：

| 项 | 值 |
|---|---|
| 后端 endpoint | `https://copilot.tencent.com`（取自 `cli/product.json → endpoint`） |
| 查询状态 | `POST /v2/billing/meter/checkin-activity-status`，body `{}` |
| 领取积分 | `POST /v2/billing/meter/daily-checkin`，body `{}` |
| 鉴权 | `Authorization: Bearer <accessToken>` + `X-User-Id` + `X-Domain` |

**凭据来源**：客户端把登录态以明文 JSON 存在
`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`
（对应源码 `FileAuthenticationStorage.getAuthSavePath()` = `sharedDataPath/auth/<authenticationId>.info`）。
脚本取其中**最新修改**的会话文件，读 `auth.accessToken` / `account.uid` / `auth.domain`。

逆向过程中的两个坑，留给后来者：

1. 共享数据目录常量名是 **`CodeBuddyExtension`**，不是 `WorkBuddyExtension`（`EXTENSION_DATA_DIR_NAME`）
2. Electron 的 `session/Local Storage` 里**没有** token，翻了是白费功夫

完整接口字段说明见 [`workbuddy-checkin/SKILL.md`](workbuddy-checkin/SKILL.md)。

## 常见问题

**Q：报 `[FAIL]` + HTTP 401/403？**
`accessToken` 过期了。打开一次 WorkBuddy 客户端让它自己刷新登录态，之后脚本就恢复正常。不需要改任何配置。

**Q：报「凭据加载失败」？**
报错里会列出所有尝试过的目录。先确认已登录过客户端；非 Windows 平台的路径尚未实测，可用 `WB_AUTH_DIR` 手动指定。

**Q：一直输出 `[SKIP]`？**
说明当天已经领过（可能你手动点过，或上一次定时任务已经领了），这是正常的幂等行为。想看详情用 `--status`。

**Q：会不会重复领 / 被判异常？**
脚本先查状态再决定是否调领取接口，已领就不会发起领取请求，调用序列和客户端点按钮一致。

**Q：安全吗？**
脚本只读本地已有的凭据文件，不发送账号密码，不上传 token 到任何第三方，全部请求只发往官方 endpoint。源码一共 ~250 行，建议自己过一遍。

## 免责声明

本项目基于对客户端的本地行为分析实现，仅用于**自动化本人账号的正常签到操作**，不涉及绕过任何付费、额度或风控限制。接口由官方维护，可能随版本变更而失效。使用者需自行确认符合所用服务的用户协议，风险自负。

## License

MIT
