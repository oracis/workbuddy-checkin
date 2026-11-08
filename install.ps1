# 安装 workbuddy-checkin 到用户级 skills 目录
#
# 用法:
#   ./install.ps1                          # 默认装 full（完整版 1.4.1，开箱即用）
#   ./install.ps1 -Edition split           # 装拆分版：主包 + bridge
#   ./install.ps1 -DestRoot D:\skills      # 自定义 skills 目录
param(
    [ValidateSet("full", "split")]
    [string]$Edition = "full",
    [string]$DestRoot = (Join-Path $HOME ".workbuddy\skills")
)

$ErrorActionPreference = "Stop"
$SrcDir = $PSScriptRoot
$BackupRoot = Join-Path (Split-Path -Parent $DestRoot) "skill-backups"

if (-not (Test-Path $DestRoot)) {
    New-Item -ItemType Directory -Path $DestRoot -Force | Out-Null
}

function Install-Pack([string]$RelativeSrc, [string]$DestName) {
    $src = Join-Path $SrcDir $RelativeSrc
    if (-not (Test-Path (Join-Path $src "SKILL.md"))) {
        Write-Error "找不到 $src\SKILL.md，请在仓库根目录运行。"
        exit 1
    }
    $dest = Join-Path $DestRoot $DestName
    if (Test-Path $dest) {
        # 备份到 skills 目录之外，避免残留目录被当成损坏的 skill 加载
        if (-not (Test-Path $BackupRoot)) {
            New-Item -ItemType Directory -Path $BackupRoot -Force | Out-Null
        }
        $backup = Join-Path $BackupRoot ("$DestName." + (Get-Date -Format "yyyyMMddHHmmss"))
        Write-Host "已存在旧版本，备份到：$backup"
        Move-Item -Path $dest -Destination $backup
    }
    Copy-Item -Path $src -Destination $dest -Recurse
    Write-Host "已安装：$DestName -> $dest"
    return $dest
}

$py = $null
foreach ($c in @("python", "python3", "py")) {
    $cmd = Get-Command $c -ErrorAction SilentlyContinue
    if ($cmd) { $py = $cmd.Source; break }
}

if ($Edition -eq "full") {
    $dest = Install-Pack "legacy/workbuddy-checkin-full" "workbuddy-checkin"
    Write-Host ""
    Write-Host "已安装【完整版 1.4.1】：自动读取本机客户端凭据，开箱即用。"
    if ($py) {
        Write-Host "--- 自检（只查状态，不领取）---"
        & $py (Join-Path $dest "scripts\wb-checkin.py") --status
        if ($LASTEXITCODE -ne 0) {
            Write-Host "（自检未通过，请确认已登录过 WorkBuddy 客户端）"
        }
    } else {
        Write-Host "未找到 python，跳过自检。"
    }
} else {
    $main = Install-Pack "workbuddy-checkin" "workbuddy-checkin"
    $bridge = Install-Pack "workbuddy-checkin-bridge" "workbuddy-checkin-bridge"
    Write-Host ""
    Write-Host "已安装【拆分版】：主包只接受显式 token，bridge 需显式同意才会运行。"
    Write-Host "  1) 生成凭据： python `"$bridge\scripts\bridge.py`" --confirm --out C:\secure\token.json"
    Write-Host "  2) 执行签到： WB_TOKEN_FILE=C:\secure\token.json python `"$main\scripts\wb-checkin.py`""
    Write-Host "（主包需要显式凭据，跳过签到自检）"
    if ($py) {
        Write-Host "--- 语法自检 ---"
        & $py -m py_compile `
            (Join-Path $main "scripts\wb-checkin.py") `
            (Join-Path $main "scripts\wb-checkin-guard.py") `
            (Join-Path $main "scripts\token_provider.py")
        if ($LASTEXITCODE -eq 0) { Write-Host "语法校验通过" }
    }
}

Write-Host ""
Write-Host "完成。"
