# 安装 workbuddy-checkin Skill 到用户级 skills 目录
# 用法: ./install.ps1 [-DestRoot <skills 目录>]
param(
    [string]$DestRoot = (Join-Path $HOME ".workbuddy\skills")
)

$ErrorActionPreference = "Stop"

$SrcDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$SkillName = "workbuddy-checkin"
$SrcSkill = Join-Path $SrcDir $SkillName
$Dest = Join-Path $DestRoot $SkillName

if (-not (Test-Path (Join-Path $SrcSkill "SKILL.md"))) {
    Write-Error "在 $SrcSkill 下找不到 SKILL.md，请在仓库根目录运行。"
    exit 1
}

if (-not (Test-Path $DestRoot)) {
    New-Item -ItemType Directory -Path $DestRoot -Force | Out-Null
}

if (Test-Path $Dest) {
    $stamp = Get-Date -Format "yyyyMMddHHmmss"
    $backup = "$Dest.bak.$stamp"
    Write-Host "已存在旧版本，备份到：$backup"
    Move-Item -Path $Dest -Destination $backup
}

Copy-Item -Path $SrcSkill -Destination $DestRoot -Recurse
Write-Host "已安装到：$Dest"

# 找一个可用的 python 做自检
$py = $null
foreach ($c in @("python", "python3", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}

if ($py) {
    Write-Host ""
    Write-Host "--- 自检（查询签到状态，不领取）---"
    $script = Join-Path $Dest "scripts\wb-checkin.py"
    & $py $script --status
    if ($LASTEXITCODE -ne 0) {
        Write-Host "（自检未通过，请确认已登录 WorkBuddy 客户端）"
    }
} else {
    Write-Host "未找到 python，跳过自检。"
}

Write-Host ""
Write-Host "完成。现在可以对 WorkBuddy 说「帮我领积分」。"
