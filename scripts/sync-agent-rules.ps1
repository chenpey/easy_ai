#Requires -Version 5.1
<#
.SYNOPSIS
把规则与守卫文件显式发布到用户级运行目录，并提供只读核对模式（Windows / PowerShell 版）。

.DESCRIPTION
支持两种源模式：
1. 本地仓库：若本地存在 AGENTS.md 则直接使用本地文件（方便本地开发测试）。
2. 远端模式：若无本地仓库或远程执行，自动从 GitHub 远端拉取最新规则，无需 clone 代码。
职责边界：不安装技能、不提交、不提权、不监听变化。
默认 -Check 只读核对；-Apply 幂等初始化软链入口，并用同目录临时文件替换规则文件、按命名键合并 hooks.json。
退出码：0 一致、1 不一致或拓扑异常、2 用法错误。

发布内容与语义对齐 POSIX 版 scripts/sync-agent-rules.sh：四个规则/守卫目标加 `~/.gemini/config/hooks.json`；
目标为非普通文件或非预期软链时报错退出；hooks.json 存在但不是合法 JSON 对象时报错且不覆盖；
所有写入都走同目录临时文件后原子替换；`-Check` 不修改部署目标；远程模式仅创建并清理临时下载文件。
入口与目标路径沿用 POSIX 版的 $HOME 布局。
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

# 兼容老旧 Windows PowerShell 5.1 默认未开启 TLS 1.2 的情况
try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
} catch {}

$RawBase = if ($env:EASY_AI_RAW_BASE) { $env:EASY_AI_RAW_BASE } else { 'https://raw.githubusercontent.com/chenpey/easy_ai/main' }
$RelPaths = @(
    'AGENTS.md',
    'agent-rules/antigravity.md',
    'agent-hooks/check_command.py',
    'agent-hooks/pi/enforce-noninteractive.ts'
)
$HooksRelPath = 'agent-hooks/antigravity/hooks.json'

$PSScriptDir = if ($PSScriptRoot) { $PSScriptRoot } else { '' }
$RootDir = if ($PSScriptDir) { Split-Path -Parent $PSScriptDir } else { '' }

$Targets = @(
    (Join-Path $HOME '.config/agent-rules/AGENTS.md'),
    (Join-Path $HOME '.gemini/config/rules/antigravity.md'),
    (Join-Path $HOME '.config/agent-hooks/check_command.py'),
    (Join-Path $HOME '.pi/agent/extensions/enforce-noninteractive.ts')
)
$HooksTarget = Join-Path $HOME '.gemini/config/hooks.json'
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
    # 悬空软链（目标已不存在）下 Test-Path 返回 $false，不能先拿它当存在性前置；
    # 直接取项再读 LinkType，否则 -Apply 会把已存在的软链当成空位新建并崩溃。
    $item = Get-Item -LiteralPath $Path -Force -ErrorAction SilentlyContinue
    if ($null -eq $item) { return $null }
    return $item.LinkType
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

function Read-JsonObject([string]$Path, [string]$Label) {
    try {
        $data = Get-Content -LiteralPath $Path -Raw -Encoding UTF8 | ConvertFrom-Json
    } catch {
        Fail "$Label is not valid JSON: $Path : $_"
    }
    if ($null -eq $data -or -not ($data -is [System.Management.Automation.PSCustomObject])) {
        Fail "$Label must be a JSON object: $Path"
    }
    return $data
}

function Get-JsonText($Value) {
    ConvertTo-Json -InputObject $Value -Depth 100 -Compress
}

# 与 POSIX 版一致的合并语义：保留目标中其他命名键，只覆盖源文件声明的键。
function Merge-Hooks([string]$SourcePath, [string]$TargetPath, [string]$TempPath) {
    $src = Read-JsonObject $SourcePath 'Hooks source'
    if (Test-Path -LiteralPath $TargetPath) {
        $item = Get-Item -LiteralPath $TargetPath -Force
        if ($item.LinkType) { Fail "Hooks target must be a regular file: $TargetPath" }
        if (-not ($item -is [System.IO.FileInfo])) { Fail "Hooks target must be a regular file: $TargetPath" }
        $dst = Read-JsonObject $TargetPath 'Hooks target'
    } else {
        $dst = New-Object System.Management.Automation.PSObject
    }
    foreach ($prop in $src.PSObject.Properties) {
        if ($dst.PSObject.Properties.Name -contains $prop.Name) {
            $dst.($prop.Name) = $prop.Value
        } else {
            $dst | Add-Member -MemberType NoteProperty -Name $prop.Name -Value $prop.Value
        }
    }
    $text = (ConvertTo-Json -InputObject $dst -Depth 100) + "`n"
    [IO.File]::WriteAllText($TempPath, $text, (New-Object Text.UTF8Encoding($false)))
}

$useLocal = $false
if ($RootDir -and (Test-Path -LiteralPath (Join-Path $RootDir $RelPaths[0]) -PathType Leaf) -and (Test-Path -LiteralPath (Join-Path $RootDir $HooksRelPath) -PathType Leaf)) {
    $useLocal = $true
    foreach ($rel in $RelPaths) {
        if (-not (Test-Path -LiteralPath (Join-Path $RootDir $rel) -PathType Leaf)) {
            $useLocal = $false
            break
        }
    }
}

$remoteTempDir = $null
$Sources = @()
$HooksSource = ''
$hooksTmp = ''

try {
    if ($useLocal) {
        foreach ($rel in $RelPaths) { $Sources += (Join-Path $RootDir $rel) }
        $HooksSource = Join-Path $RootDir $HooksRelPath
    } else {
        $remoteTempDir = Join-Path ([IO.Path]::GetTempPath()) ('easy_ai_rules_' + [guid]::NewGuid().ToString('N'))
        New-Item -ItemType Directory -Path $remoteTempDir -Force | Out-Null
        foreach ($rel in $RelPaths) {
            $dest = Join-Path $remoteTempDir $rel
            $destDir = Split-Path -Parent $dest
            if (-not (Test-Path -LiteralPath $destDir)) { New-Item -ItemType Directory -Path $destDir -Force | Out-Null }
            $url = "$RawBase/$rel"
            try {
                Invoke-RestMethod -Uri $url -OutFile $dest -TimeoutSec 30
            } catch {
                Fail "Failed to download $url : $_"
            }
            $Sources += $dest
        }
        $HooksSource = Join-Path $remoteTempDir $HooksRelPath
        $hooksDir = Split-Path -Parent $HooksSource
        if (-not (Test-Path -LiteralPath $hooksDir)) { New-Item -ItemType Directory -Path $hooksDir -Force | Out-Null }
        $hooksUrl = "$RawBase/$HooksRelPath"
        try {
            Invoke-RestMethod -Uri $hooksUrl -OutFile $HooksSource -TimeoutSec 30
        } catch {
            Fail "Failed to download $hooksUrl : $_"
        }
    }

    # 先把 hooks 合并结果写进临时文件并校验，成功后才动入口软链与规则文件。
    if ($Mode -eq 'Apply') {
        $hooksDir = Split-Path -Parent $HooksTarget
        if (-not (Test-Path -LiteralPath $hooksDir)) { New-Item -ItemType Directory -Path $hooksDir -Force | Out-Null }
        $hooksTmp = Join-Path $hooksDir ('.agent-hooks.' + [guid]::NewGuid().ToString('N') + '.tmp')
        Merge-Hooks $HooksSource $HooksTarget $hooksTmp
        [void](Read-JsonObject $hooksTmp 'Merged hooks')
    }

    # 入口检查与初始化
    if ($Mode -eq 'Apply') {
        foreach ($entry in $Entries) {
            $entryDir = Split-Path -Parent $entry
            if (-not (Test-Path -LiteralPath $entryDir)) {
                New-Item -ItemType Directory -Path $entryDir -Force | Out-Null
            }
            if (Test-IsSymlink $entry) {
                $target = Get-LinkTarget $entry
                if (($null -eq $target) -or ($target -ne [IO.Path]::GetFullPath($Targets[0]))) {
                    Fail "已有非预期软链入口: $entry"
                }
            } elseif (Test-Path -LiteralPath $entry) {
                Fail "已有非预期入口（非符号链接），请先核对并备份: $entry"
            } else {
                New-Item -ItemType SymbolicLink -Path $entry -Target $Targets[0] | Out-Null
            }
        }
    } else {
        foreach ($entry in $Entries) {
            if (-not (Test-IsSymlink $entry)) { Fail "Unexpected rule entry: $entry" }
            $target = Get-LinkTarget $entry
            if ($null -eq $target) { Fail "Unexpected relative rule entry target: $entry" }
            if ($target -ne [IO.Path]::GetFullPath($Targets[0])) { Fail "Unexpected rule entry: $entry" }
        }
    }

    for ($i = 0; $i -lt $Sources.Count; $i++) {
        if (-not (Test-Path -LiteralPath $Sources[$i] -PathType Leaf)) { Fail "Missing source: $($Sources[$i])" }
        if (Test-Path -LiteralPath $Targets[$i] -PathType Container) { Fail "Target is a directory: $($Targets[$i])" }
    }
    if (Test-Path -LiteralPath $HooksTarget -PathType Container) { Fail "Hooks target is a directory: $HooksTarget" }

    # 运行时规则/守卫发布文件必须是非软链真文件；仅 AGY 专有规则允许本地仓库模式下的过渡软链。
    for ($i = 0; $i -lt $Targets.Count; $i++) {
        if (-not (Test-IsSymlink $Targets[$i])) { continue }
        if ($i -ne 1) { Fail "Runtime rule target must be a regular file: $($Targets[$i])" }
        if ($useLocal) {
            $target = Get-LinkTarget $Targets[$i]
            if (($null -eq $target) -or ($target -ne [IO.Path]::GetFullPath($Sources[$i]))) {
                Fail 'Unexpected AGY symlink target'
            }
        } else {
            Fail 'Unexpected AGY symlink target'
        }
    }

    # hooks 临时文件已就绪，先替换规则/守卫文件，最后才动 hooks.json。
    if ($Mode -eq 'Apply') {
        for ($i = 0; $i -lt $Sources.Count; $i++) {
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

        Move-Item -LiteralPath $hooksTmp -Destination $HooksTarget -Force
        $hooksTmp = ''
    }

    for ($i = 0; $i -lt $Sources.Count; $i++) {
        if (Test-IsSymlink $Targets[$i]) { Fail "Runtime rule must be a regular file: $($Targets[$i])" }
        if (-not (Test-Path -LiteralPath $Targets[$i] -PathType Leaf)) { Fail "Missing target file: $($Targets[$i])" }
        if ((Get-Sha256 $Sources[$i]) -ne (Get-Sha256 $Targets[$i])) {
            Fail "Files differ: $($Sources[$i]) -> $($Targets[$i])"
        }
    }

    if (Test-IsSymlink $HooksTarget) { Fail "Missing or non-regular hooks target: $HooksTarget" }
    if (-not (Test-Path -LiteralPath $HooksTarget -PathType Leaf)) { Fail "Missing or non-regular hooks target: $HooksTarget" }
    $hooksSrc = Read-JsonObject $HooksSource 'Hooks source'
    $hooksDst = Read-JsonObject $HooksTarget 'Hooks target'
    foreach ($prop in $hooksSrc.PSObject.Properties) {
        $dstProp = $hooksDst.PSObject.Properties[$prop.Name]
        if ($null -eq $dstProp) { Fail "Hooks key mismatch ($($prop.Name)) in $HooksTarget" }
        if ((Get-JsonText $dstProp.Value) -ne (Get-JsonText $prop.Value)) {
            Fail "Hooks key mismatch ($($prop.Name)) in $HooksTarget"
        }
    }

    foreach ($path in ($Sources + @($HooksSource) + $Targets + @($HooksTarget))) {
        '{0}  {1}' -f (Get-Sha256 $path), $path
    }
    Write-Output 'Rule files match; harness loading has not been verified.'
}
finally {
    if ($hooksTmp -and (Test-Path -LiteralPath $hooksTmp)) { Remove-Item -LiteralPath $hooksTmp -Force -ErrorAction SilentlyContinue }
    if ($remoteTempDir -and (Test-Path -LiteralPath $remoteTempDir)) {
        Remove-Item -LiteralPath $remoteTempDir -Recurse -Force -ErrorAction SilentlyContinue
    }
}
exit 0
