# workbuddy-checkin

> WorkBuddy（原 CodeBuddy）每日签到自动领积分 —— 幂等、零依赖、可无人值守。

官方积分入口需要手动点「领取今日礼包」，客户端没开着就会漏签、断掉连续签到奖励。
本项目直接调用客户端自身使用的后端签到接口，让 AI 助手（或系统计划任务）帮你自动领。

- **幂等**：先查当天状态，已领则立即跳过，重复运行零副作用
- **零依赖**：只用 Python 标准库，无需 `pip install`
- **无人值守**：配系统计划任务轮询，开机期间没领到就按频率重试，领到当天即收工
- **不碰密码**：全程只用登录态 token，不做交互式登录，不涉及账号密码

## 本仓库有三个包，先选一个

从 1.5.0 起按**拆分架构**分发：签到能力与「读取本机客户端凭据」能力彻底分开。

| 包 | 目录 | 凭据来源 | 适合谁 | 发布位置 |
|---|---|---|---|---|
| **主包 1.5.3** | `workbuddy-checkin/` | 用户**显式提供** token | 想要最小权限、代码与声明逐字对齐；从市场安装 | GitHub / SkillHub / ClawHub |
| **桥接组件 1.5.3** | `workbuddy-checkin-bridge/` | 本机客户端会话（需显式同意） | 想让脚本直接用已登录的会话 | GitHub / SkillHub（**不上 ClawHub**） |
| **完整版 1.4.1（legacy）** | `legacy/workbuddy-checkin-full/` | 自动读取本机凭据并续期写回 | 想装完就能用、不折腾凭据；本机运行时用的就是它 | 仅 GitHub |

**为什么要拆**：ClawHub 的安全扫描把「签到技能里附带凭据解密 + 进程调试提取能力」判为
*声明目的与代码能力不符*。拆开后主包只做签到，桥接组件的用途、风险和同意流程单独写明，
由用户自己决定是否安装。完整版功能最全但能力面最大，因此不再推到市场，只留在仓库里。

> 主包**不能**自己去读本机登录态。要开箱即用的自动续期与写回，请用完整版；
> 要市场版的拆分架构，就按下面「主包 + bridge」两步装。

## 安装

```bash
git clone https://github.com/oracis/workbuddy-checkin.git
cd workbuddy-checkin
```

```powershell
# Windows PowerShell —— 默认装完整版（开箱即用，向后兼容）
./install.ps1

# 装拆分版：主包 + bridge 两个 Skill 一起装
./install.ps1 -Edition split
```

```bash
# macOS / Linux / Git Bash
bash install.sh                          # 完整版
bash install.sh ~/.workbuddy/skills split   # 拆分版
```

也可以手动复制目录到 `~/.workbuddy/skills/`（项目级则放 `<项目>/.workbuddy/skills/`）。
安装脚本遇到同名旧版本会先备份到 `skill-backups/`，不会直接覆盖。

装好后对 WorkBuddy 说「**帮我领取 WorkBuddy 今日积分**」即可；只说「签到」而不指明平台时，
助手会先跟你确认是哪个服务。

## 用法

### 完整版（legacy）

```bash
python legacy/workbuddy-checkin-full/scripts/wb-checkin.py            # 签到
python legacy/workbuddy-checkin-full/scripts/wb-checkin.py --status   # 只查不领
python legacy/workbuddy-checkin-full/scripts/wb-checkin.py --json     # 原始 JSON
python legacy/workbuddy-checkin-full/scripts/wb-checkin.py --force    # 调试：强制领
python legacy/workbuddy-checkin-full/scripts/wb-checkin.py --no-refresh
```

`accessToken` 过期时自动用 `refreshToken` 续期并安全写回凭据文件（排他锁 + 竞态检测 +
原子替换 + `.wb-bak` 备份；客户端在运行时不写回）。

### 拆分版：主包 + bridge

主包只认两个显式入口，都没有就明确报错，**绝不回退去翻系统里的其它文件**：

| 入口 | 环境变量 | 说明 |
|---|---|---|
| 直接给 token | `WB_ACCESS_TOKEN` | 可另配 `WB_REFRESH_TOKEN`、`WB_USER_ID`、`WB_DOMAIN` |
| 指定 JSON 文件 | `WB_TOKEN_FILE` | `{"accessToken","refreshToken","uid","domain"}`；拒绝符号链接 |

```bash
# 1) 显式同意，把本机会话解密成 JSON（bridge 不会自动运行）
python workbuddy-checkin-bridge/scripts/bridge.py --confirm --out C:/secure/token.json

# 2) 主包读该文件完成签到
WB_TOKEN_FILE=C:/secure/token.json python workbuddy-checkin/scripts/wb-checkin.py
```

bridge 输出的 JSON 等同账号登录凭证，用完即删。不落盘也可省略 `--out`，bridge 会把
一行 JSON 打到 stdout 由你自己处理（主包不会去执行 bridge，也不会读它的 stdout）。

退出码：`0` = 成功领取 / 今日已领 / 活动未开启；`1` = 失败。

## 配定时任务：必须用系统级调度

**这是最容易踩的坑（2026-11-08 实测）**：WorkBuddy 的「定时任务」是
`LocalAutomationScheduler` 本地调度，**只有客户端进程在运行时才会触发**。客户端没开＝任务
完全不执行；任务运行中被关掉，那次 run 会一直挂住，下次启动被判定
`ErrorAutomation-MissedWindow` 直接放弃（实测挂过 **144.9 小时**，导致连续 5 天漏签、500 积分）。

脚本本身零客户端依赖，所以要真正做到「不开客户端也能领」，调度必须交给操作系统。

### 推荐：轮询 + 当日守卫

语义：**开机期间按频率轮询，今天没领到就继续试，领到了当天立刻收工**（一天只领一次）。
主包与完整版都随包提供 `scripts/wb-checkin-guard.py`：

```text
marker 日期 == 今天  -> 立刻退出（不联网、不写日志，约 0.4~0.9 秒）
否则跑 wb-checkin.py -> [OK] / [SKIP] 写 marker；[FAIL] 不写，下个周期重试
```

部署要点：

1. 把 `wb-checkin.py` 与 `wb-checkin-guard.py` 放同一目录（如 `~/.workbuddy/scripts/`）
2. Windows：用 `schtasks` 导入 XML，`CalendarTrigger` + `Repetition`（每 30 分钟覆盖全天），
   `StartWhenAvailable=true`（关机错过则开机补跑），动作用 **`pythonw.exe`**（无黑窗）。
   XML 需 UTF-16 编码、`<StartBoundary>` 不晚于当天
3. macOS / Linux：cron 每 30 分钟调守卫，例如

   ```cron
   */30 * * * * /usr/bin/python3 /path/to/wb-checkin-guard.py
   ```

4. 用守卫时**别再让 WorkBuddy 内置定时任务也跑签到**，两边语义冲突（建议暂停客户端任务）

> Git Bash 里 `schtasks` 参数要写双斜杠（`//create`、`//query`），否则 `/create` 会被 MSYS 当路径转换。

守卫的路径安全（1.5.3 起）：解释器只从两个显式可信候选中选取；脚本、marker、日志及所在目录
逐段校验，**链接检查先于 `realpath`**（否则指向外部文件的链接会被解析成看似正常的目标），
解析后还要求目标仍在技能目录内；读写走 `O_NOFOLLOW` + fd 复核，Windows 无该标志时改用
reparse point 属性位。子进程 argv 全是代码内常量、`shell=False`。

## 工作原理

逆向自客户端 `resources/app.asar`：

| 项 | 值 |
|---|---|
| 后端 endpoint | `https://copilot.tencent.com`（取自 `cli/product.json → endpoint`），备用 `https://www.workbuddy.cn` |
| 查询状态 | `POST /v2/billing/meter/checkin-activity-status`，body `{}` |
| 领取积分 | `POST /v2/billing/meter/daily-checkin`，body `{}` |
| 令牌续期 | `POST /v2/plugin/auth/token/refresh`，header `X-Refresh-Token` + `X-Auth-Refresh-Source: plugin` |
| 鉴权 | `Authorization: Bearer <accessToken>` + `X-User-Id` + `X-Domain` |

主包把域名**硬编码为白名单**（仅这两个官方域名，强制 HTTPS + 443、禁 userinfo、禁重定向），
没有任何环境变量或开关能放行到其它主机；`WB_ENDPOINT` 只能在两者间切换。续期得到的新 token
只在本次进程内使用，**不落盘**。完整版额外负责读取/解密本机凭据与写回续期结果。

逆向过程中的几个坑，留给后来者：

1. 共享数据目录常量名是 **`CodeBuddyExtension`**，不是 `WorkBuddyExtension`
2. Electron 的 `session/Local Storage` 里**没有** token，翻了是白费功夫
3. refresh 端点必须走 `/v2/plugin/...`；用 `/v2/auth/token/refresh` 返回 404
4. 签到接口**没有补签/补卡端点** —— 漏掉的日期追不回来，只能靠调度避免再漏
5. 在多个工作区各建一个同名系统任务会双跑，两边回报的累计数据对不上，排查时极易误判

完整字段说明见各包自己的 `SKILL.md`。

## 常见问题

**Q：`[FAIL]` + HTTP 401/403？**
完整版会自动续期并重试；只有连 `refreshToken` 也过期时才会失败，打开一次客户端重新登录即可。
拆分版请重新提供有效凭据（或重跑一次 bridge）。

**Q：报「凭据加载失败」？**
完整版会列出所有尝试过的目录与跳过原因，先确认至少登录过一次客户端，或用 `WB_AUTH_DIR` 指定。
拆分版请检查 `WB_ACCESS_TOKEN` / `WB_TOKEN_FILE` 是否设置。

**Q：一直输出 `[SKIP]`？**
当天已经领过（可能手动点过，或定时任务已领），这是正常的幂等行为。想看详情用 `--status`。

**Q：会不会重复领 / 被判异常？**
先查状态再决定是否调领取接口，已领就不发起领取请求，调用序列和客户端点按钮一致。
守卫脚本还会用当日标记额外拦截重复轮询。

**Q：安全吗？**
主包只访问官方白名单域名，不读本机凭据、不写登录态、不启动额外子进程；bridge 只在用户
显式同意后运行，build key 只在内存中使用、不落盘、不向外部网络发送任何数据。
所有实现都是公开的，建议自己过一遍再装。

## 版本与发布

| 平台 | 条目 | 版本 | 内容 |
|---|---|---|---|
| ClawHub | `oracis/workbuddy-checkin` | 1.5.3 | 主包 |
| SkillHub | `workbuddy-checkin` | 1.5.3 | 主包 |
| SkillHub | `workbuddy-checkin-bridge` | 1.5.3 | bridge（独立条目） |
| GitHub | 本仓库 `main` | 1.5.3 | 主包 + bridge + legacy 完整版 |

1.5.3 相对 1.5.2 的改动：修复守卫脚本 marker/log 的链接绕过缺陷（保留词法路径、
链接检查前置于 `realpath`、`O_NOFOLLOW` + fd 复核），并移除主包的 token provider
子进程分支（主脚本全程零子进程）。1.4.1 完整版**保持原样**，本机运行时不受影响。

## 免责声明

本项目基于对客户端的本地行为分析实现，仅用于**自动化本人账号的正常签到操作**，不涉及绕过
任何付费、额度或风控限制。接口由官方维护，可能随版本变更而失效。使用者需自行确认符合所用
服务的用户协议，风险自负。

## License

MIT
