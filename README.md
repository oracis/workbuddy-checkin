# workbuddy-checkin

> WorkBuddy（原 CodeBuddy）每日签到自动领积分 Skill —— 幂等、零依赖、无需客户端在线、可无人值守。

官方积分入口需要手动点「领取今日礼包」，客户端没开着就会漏签、断掉连续签到奖励。
这个 Skill 直接调用客户端自身使用的后端签到接口，让 AI 助手（或系统定时任务）帮你自动领。

- **幂等**：先查当天状态，已领则立即跳过，重复运行零副作用
- **零依赖**：只用 Python 标准库，无需 `pip install`
- **不碰密码**：只读取客户端已有的本地登录态，不做网络登录，不存储任何凭据
- **自动续期**：`accessToken` 过期时用 `refreshToken` 自动换新并安全写回（排他锁 + 竞态检测 + 原子替换）
- **无人值守**：配系统计划任务轮询，开机期间没领到就按频率重试，领到当天即收工
- **不开客户端也能跑**：脚本只依赖本机凭据文件，不依赖客户端进程（但账号必须至少登录过一次）

## 效果

```
$ python workbuddy-checkin/scripts/wb-checkin.py
活动        : Buddy应用（Buddy加油站）
期数        : 第 10 期
时间范围      : 2026-09-30 00:00:00 ~ 2026-12-31 23:59:59
已签到天数     : 32（本周 2 天）
累计积分      : 3200
今日积分      : 100
本周进度      : ●●○○○○○
[SKIP] 今天已经领过了，无需重复领取（今日 +100 积分）
```

## 安装

### 方式一：作为 WorkBuddy Skill（推荐）

把 `workbuddy-checkin/` 目录整个复制到用户级 skills 目录：

```bash
git clone https://github.com/oracis/workbuddy-checkin.git
cd workbuddy-checkin

# Windows (Git Bash) / macOS / Linux
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
| `python wb-checkin.py --no-refresh` | 禁用自动续期（排查续期链路时用） |

退出码：`0` = 成功领取 / 今日已领 / 活动未开启；`1` = 失败。

环境变量：

| 变量 | 说明 |
|---|---|
| `WB_AUTH_DIR` | 手动指定凭据目录（自动探测失败时用） |
| `WB_ENDPOINT` | 覆盖后端地址，默认 `https://copilot.tencent.com` |
| `WB_NODE` | 自愈提取 build key 时指定 Node.js 可执行文件 |

## 配定时任务：优先用系统计划任务，别只依赖客户端定时任务

**这是最容易踩的坑（2026-11-08 实测）**：WorkBuddy 的「定时任务」是 `LocalAutomationScheduler`
本地调度，**只有客户端进程在运行时才会触发**。客户端没开＝任务完全不执行；客户端在任务运行中被关掉，
那次 run 会一直挂住，下次启动被判定 `ErrorAutomation-MissedWindow` 直接放弃
（实测挂过 **144.9 小时**，导致连续 5 天漏签、损失 500 积分）。

脚本本身零客户端依赖，所以要真正做到「不开客户端也能领」，调度必须交给操作系统。

### 推荐：轮询 + 当日守卫

语义：**开机期间按频率轮询，今天没领到就继续试，领到了当天立刻收工**（一天只领一次）。
随包提供 `scripts/wb-checkin-guard.py`：

```python
if read_mark().get("date") == today():
    return 0                      # 今天已领：不联网、不写日志、约 380ms 静默退出
out = run([PY, "-u", "wb-checkin.py"])          # 未领才真的跑
if "[OK]" in out or "[SKIP]" in out:            # [SKIP] = 别处已领，同样算今天完成
    write_mark({"date": today(), "result": headline})
else:
    log("FAILED\n" + out)                       # [FAIL] 不写标记 → 下个周期自动重试
```

部署要点（完整 XML 与逐条说明见 [`workbuddy-checkin/SKILL.md`](workbuddy-checkin/SKILL.md) 的「调度方式」）：

1. 把 `wb-checkin.py` 与 `wb-checkin-guard.py` 放到同一目录（如 `~/.workbuddy/scripts/`）
2. Windows：用 `schtasks` 导入 XML，`CalendarTrigger` + `Repetition`（每 30 分钟，覆盖全天），
   `StartWhenAvailable=true`（关机错过则开机补跑），动作用 **`pythonw.exe`**（无黑窗）
3. macOS / Linux：cron 每 30 分钟调守卫，例如

   ```cron
   */30 * * * * /usr/bin/python3 /path/to/wb-checkin-guard.py
   ```

4. 用守卫时**别再让 WorkBuddy 内置定时任务也跑签到**，两边语义冲突（建议把客户端任务暂停）

> Git Bash 里 `schtasks` 参数要写双斜杠（`//create`、`//query`），否则 `/create` 会被 MSYS 当路径转换。

如果仍想用 WorkBuddy 内置定时任务：RRULE 的 `BYHOUR` 只接受**单个整数**
（`BYHOUR=9,21` 会被拒），要多时段请用 `FREQ=HOURLY;INTERVAL=8`。请明确接受
「客户端不在线就不触发」这一限制。

## 工作原理

逆向自客户端 `resources/app.asar`：

| 项 | 值 |
|---|---|
| 后端 endpoint | `https://copilot.tencent.com`（取自 `cli/product.json → endpoint`，可用 `WB_ENDPOINT` 覆盖） |
| 查询状态 | `POST /v2/billing/meter/checkin-activity-status`，body `{}` |
| 领取积分 | `POST /v2/billing/meter/daily-checkin`，body `{}` |
| 令牌续期 | `POST /v2/plugin/auth/token/refresh`，header `X-Refresh-Token` + `X-Auth-Refresh-Source: plugin`（注意必须带 `/plugin` 前缀，另一条路径是 404） |
| 鉴权 | `Authorization: Bearer <accessToken>` + `X-User-Id` + `X-Domain` |

**凭据来源**：客户端把登录态存在
`%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth\*.info`。
新版客户端用 AES-256-GCM 的 at-rest 信封加密 token 字段，由 `scripts/at_rest.py` 解密
（零依赖纯 Python 实现；缺 build key 时会从运行中的客户端自动提取）。
脚本取其中**最新可用**的会话文件，读 `auth.accessToken` / `auth.refreshToken` / `account.uid` / `auth.domain`。

`accessToken` 过期时脚本会自动续期并把新 token 加密写回凭据文件。写回有四道保护：
客户端在运行时不写、写前重读做竞态检测、`O_EXCL` 排他锁、临时文件 + `os.replace()` 原子替换
（另留 `*.wb-bak` 备份）。只有 `auth` 段里的 token 与时间戳会被改动。

逆向过程中的几个坑，留给后来者：

1. 共享数据目录常量名是 **`CodeBuddyExtension`**，不是 `WorkBuddyExtension`（`EXTENSION_DATA_DIR_NAME`）
2. Electron 的 `session/Local Storage` 里**没有** token，翻了是白费功夫
3. refresh 端点必须走 `/v2/plugin/...`；用 `/v2/auth/token/refresh` 返回 404
4. 签到接口**没有补签/补卡端点** —— 漏掉的日期追不回来，只能靠调度避免再漏

完整接口字段说明见 [`workbuddy-checkin/SKILL.md`](workbuddy-checkin/SKILL.md)。

## 常见问题

**Q：报 `[FAIL]` + HTTP 401/403？**
正常情况下脚本会自动用 `refreshToken` 续期并重试，无需人工干预。
只有当连 `refreshToken` 也过期（通常约 60 天完全没开过客户端）时才会失败，
这时打开一次 WorkBuddy 客户端重新登录即可恢复。

**Q：报「凭据加载失败」？**
报错里会列出所有尝试过的目录与被跳过文件的原因。先确认已登录过客户端；
非 Windows 平台的路径尚未实测，可用 `WB_AUTH_DIR` 手动指定。

**Q：一直输出 `[SKIP]`？**
说明当天已经领过（可能你手动点过，或上一次定时任务已经领了），这是正常的幂等行为。想看详情用 `--status`。

**Q：会不会重复领 / 被判异常？**
脚本先查状态再决定是否调领取接口，已领就不会发起领取请求，调用序列和客户端点按钮一致。
守卫脚本还会额外用当日标记拦截重复轮询。

**Q：安全吗？**
脚本只读本地已有的凭据文件，不发送账号密码，不上传 token 到任何第三方，全部请求只发往官方 endpoint。
续期与写回的实现是公开的（见 `scripts/wb-checkin.py` 与 `scripts/at_rest.py`），建议自己过一遍。

## 免责声明

本项目基于对客户端的本地行为分析实现，仅用于**自动化本人账号的正常签到操作**，不涉及绕过任何付费、额度或风控限制。接口由官方维护，可能随版本变更而失效。使用者需自行确认符合所用服务的用户协议，风险自负。

## License

MIT
