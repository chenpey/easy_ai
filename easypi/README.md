# easypi

当前版本：`0.6.1`

专用于 Antigravity（agy）向 `pi` 分派任务并走 `easydev` **`gate` 模式**完成闭环：**agy 设计 → Pi 走 `easydev gate` 审设计（无分歧后开发自验）→ agy 开发终审（无分歧后结束）**；在用户指定开局即走或批准升级到 **`full` 协同分支**时，保留 `agy ↔ Pi`（会话 A）开发协同，按 `easydev` 模型组配置（`models.json`）与外发门槛读取 `developer`/`reviewer` 模型（无效、`inherit` 或不可用时不静默回退，交用户指定），由 agy 在外层另起全新 `--session-id` 的只读 `Pi` 实例补齐独立第三方 `d/3`、`r/5` 与终审两阶段闭环（详见 [references/full.md](references/full.md)）。

## 目录结构

```text
easypi/
├── CHANGELOG.md
├── README.md
├── SKILL.md
├── references/
│   ├── full.md
│   └── gate.md
└── scripts/
    └── run_pi.sh
```

## 安装到 Antigravity

在本仓库根目录执行以下命令（幂等软链到 Antigravity 全局技能目录）：

```bash
mkdir -p "$HOME/.gemini/config/skills"
ln -sfn "$PWD/easypi" "$HOME/.gemini/config/skills/easypi"
```

本技能依赖仓库内的 `easypi/scripts/run_pi.sh`，且只在 Antigravity 宿主使用，所以整目录软链是刻意的，不走其他技能的运行时文件集安装契约；`scripts/smoke-install.sh` 会在临时 `$HOME` 里校验该软链契约（安装后 `SKILL.md` 与 `scripts/run_pi.sh` 可达）。

## 辅助脚本用法

`easypi/scripts/run_pi.sh` 封装非交互模式与 stdin 封闭（`</dev/null`），第二参数是 prompt 文件（不内联设计正文，避免宿主钩子把反引号与 `$(...)` 当命令复核）；脚本自身不切工作目录（`pi` 继承调用方 `cwd`）。硬超时 `EASYPI_TIMEOUT`（默认 900 秒）到期终止整组进程并以 `124` 退出；正常退出也清理同组残留子进程，外部 `SIGTERM` 以 `143` 退出。会话 ID 不得以 `-` 开头，额外参数只接受 `--model`/`--thinking`/`--approve`（其余一律 `exit 2`），取值须非空白且不以 `-` 开头。完整语义见 [SKILL.md](SKILL.md) 第 2 节与 [full 协同分支](references/full.md)。

`--model` 只在确有需要时传入：`gate` 不按 `models.json` 委派角色，调用方未指定即省略；`full` 协同按 `developer` 的核实值传入（独立审查实例另按 `reviewer` 核实值），解析为 `inherit` 或不可用时按 [full.md](references/full.md) 暂停交用户指定，不静默省略或猜模型。

```bash
bash "$HOME/.gemini/config/skills/easypi/scripts/run_pi.sh" "<session-id>" "<prompt-file>" </dev/null
```

## prompt 模板

`gate` 设计审查用的 prompt 模板、`**[建议]**` 通道要求与 `baseline/` 敏感快照口径维护在 [references/gate.md](references/gate.md#prompt-模板)；模板写成文件后作为第二个参数传入，不内联到命令行。

## `full` 协同分支模板

`full` 协同分支的完整规程以及专用的 [developer prompt 模板](references/full.md#developer-prompt-模板) 与 [reviewer prompt 模板](references/full.md#reviewer-prompt-模板) 均维护在 [references/full.md](references/full.md)。
