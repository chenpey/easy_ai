#Requires -Version 5.1
<#
.SYNOPSIS
把仓库内的规则文件显式发布到用户级运行目录，并提供只读核对模式（Windows / PowerShell 版）。

.DESCRIPTION
职责边界与 scripts/sync-agent-rules.sh 一致：不安装技能、不创建或修复 harness 入口、不提交、不提权、不监听变化。
默认 -Check 只读核对；-Apply 用同目录临时文件替换。退出码：0 一致、1 不一致或拓扑异常、2 用法错误。

入口与目标路径沿用 POSIX 版的 $HOME 布局。若某个 harness 在 Windows 使用不同路径，改下面三个数组即可。
链接目标必须是完全限定路径（盘符绝对或 UNC）；`\foo`、`C:foo` 与相对路径一律拒绝。用法错误（未知参数或同时传 `-Check -Apply`）退出 2。
#>
param(
    [switch]$Check,
    [switch]$Apply
)

if ($args.Count -gt 0) {
    [Console]::Error.WriteLine('Usage: sync-agent-rules.ps1 [-Check | -Apply]')
    exit 2
}

if ($Check -and $Apply) {
    [Console]::Error.WriteLine('Usage: sync-agent-rules.ps1 [-Check | -Apply]')
    exit 2
}
$Mode = if ($Apply) { 'Apply' } else { 'Check' }

$ErrorActionPreference = 'Stop'

$RootDir = Split-Path -Parent $PSScriptRoot

$Sources = @(
    (Join-Path $RootDir 'AGENTS.md'),
    (Join-Path $RootDir 'agent-rules/antigravity.md')
)
$Targets = @(
    (Join-Path $HOME '.config/agent-rules/AGENTS.md'),
    (Join-Path $HOME '.gemini/config/rules/antigravity.md')
)
$Entries = @(
    (Join-Path $HOME '.pi/agent/AGENTS.md'),
    (Join-Path $HOME '.codex/AGENTS.md'),
    (Join-Path $HOME '.gemini/config/AGENTS.md')
)

function Fail([string]$Message) {
    [Console]::Error.WriteLine($Message)
    exit 1
}

function Get-LinkType([string]$Path) {
    if (-not (Test-Path -LiteralPath $Path)) { return $null }
    return (Get-Item -LiteralPath $Path -Force).LinkType
}

function Test-IsFullyQualified([string]$Path) {
    # 只接受盘符绝对（C:\ 或 C:/）与 UNC（\\server\share）；拒绝 \foo、C:foo 与其余相对形式。
    if ([string]::IsNullOrWhiteSpace($Path)) { return $false }
    if ($Path -match '^[A-Za-z]:[\\/]') { return $true }
    if ($Path -match '^\\\\') { return $true }
    return $false
}

function Get-LinkTarget([string]$Path) {
    $target = (Get-Item -LiteralPath $Path -Force).Target
    if ($target -is [array]) { $target = $target[0] }
    if (-not (Test-IsFullyQualified $target)) { return $null }
    return [IO.Path]::GetFullPath($target)
}

function Test-IsSymlink([string]$Path) {
    return ((Get-LinkType $Path) -eq 'SymbolicLink')
}

function Get-Sha256([string]$Path) {
    (Get-FileHash -LiteralPath $Path -Algorithm SHA256).Hash.ToLower()
}

# 入口必须是符号链接且指向通用规则的发布目标；相对目标视为异常。
foreach ($entry in $Entries) {
    if (-not (Test-IsSymlink $entry)) { Fail "Unexpected rule entry: $entry" }
    $target = Get-LinkTarget $entry
    if ($null -eq $target) { Fail "Unexpected relative rule entry target: $entry" }
    if ($target -ne [IO.Path]::GetFullPath($Targets[0])) { Fail "Unexpected rule entry: $entry" }
}

for ($i = 0; $i -lt 2; $i++) {
    if (-not (Test-Path -LiteralPath $Sources[$i] -PathType Leaf)) { Fail "Missing source: $($Sources[$i])" }
    if (Test-Path -LiteralPath $Targets[$i] -PathType Container) { Fail "Target is a directory: $($Targets[$i])" }
}

# 通用规则的发布文件必须是普通文件；硬链接仍是普通文件，不算软链。
if (Test-IsSymlink $Targets[0]) { Fail 'Shared rule target must be a regular file' }
if ((Test-IsSymlink $Targets[1])) {
    $target = Get-LinkTarget $Targets[1]
    if (($null -eq $target) -or ($target -ne [IO.Path]::GetFullPath($Sources[1]))) {
        Fail 'Unexpected AGY symlink target'
    }
}

if ($Mode -eq 'Apply') {
    for ($i = 0; $i -lt 2; $i++) {
        $targetDir = Split-Path -Parent $Targets[$i]
        if (-not (Test-Path -LiteralPath $targetDir)) {
            New-Item -ItemType Directory -Path $targetDir -Force | Out-Null
        }
        $tmp = Join-Path $targetDir ('.agent-rule.' + [guid]::NewGuid().ToString('N') + '.tmp')
        try {
            Copy-Item -LiteralPath $Sources[$i] -Destination $tmp -Force
            if ((Get-Sha256 $Sources[$i]) -ne (Get-Sha256 $tmp)) { Fail "Copy verification failed: $tmp" }
            Move-Item -LiteralPath $tmp -Destination $Targets[$i] -Force
        }
        finally {
            if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force }
        }
    }
}

for ($i = 0; $i -lt 2; $i++) {
    if (Test-IsSymlink $Targets[$i]) { Fail "Runtime rule must be a regular file: $($Targets[$i])" }
    if ((Get-Sha256 $Sources[$i]) -ne (Get-Sha256 $Targets[$i])) {
        Fail "Files differ: $($Sources[$i]) -> $($Targets[$i])"
    }
}

foreach ($path in ($Sources + $Targets)) {
    '{0}  {1}' -f (Get-Sha256 $path), $path
}
Write-Output 'Rule files match; harness loading has not been verified.'
exit 0
