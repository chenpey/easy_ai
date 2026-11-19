# easypi

当前版本：`0.4.0`

专用于 Antigravity（agy）向 `pi` 分派任务并走 `easydev` **`gate` 模式**完成闭环：**agy 设计 → Pi 走 `easydev gate` 审设计（无分歧后开发自验）→ agy 开发终审（无分歧后结束）**；在用户指定开局即走或批准升级到 **`full` 协同分支**时，保留 `agy ↔ Pi`（会话 A）开发协同，按 `easydev` 模型组配置（`models.json`）与外发门槛读取 `developer`/`reviewer` 模型（无效、`inherit` 或不可用时不静默回退，交用户指定），由 agy 在外层另起全新 `--session-id` 的只读 `Pi` 实例补齐独立第三方 `d/3`、`r/5` 与终审两阶段闭环（详见 [references/full.md](references/full.md)）。

## 目录结构

```text
easypi/
├── CHANGELOG.md
├── README.md
├── SKILL.md
├── references/
│   └── full.md
└── scripts/
    └── run_pi.sh
```

## 安装到 Antigravity

在本仓库根目录执行以下命令（幂等软链到 Antigravity 全局技能目录）：

```bash
mkdir -p "$HOME/.gemini/config/skills"
ln -sfn "$PWD/easypi" "$HOME/.gemini/config/skills/easypi"
```

本技能依赖仓库内的 `scripts/run_pi.sh`，且只在 Antigravity 宿主使用，所以整目录软链是刻意的，不走其他技能的运行时文件集安装契约（也不在 `scripts/smoke-install.sh` 覆盖范围内）。

## 辅助脚本用法

`scripts/run_pi.sh` 封装了非交互模式与 stdin 封闭（`</dev/null`），第二个参数是 prompt 文件（不内联到命令行，避免宿主钩子把设计正文里的反引号与 `$(...)` 当命令复核）；脚本自身不切换工作目录（`exec pi` 继承调用方 `cwd`）：

```bash
bash "$HOME/.gemini/config/skills/easypi/scripts/run_pi.sh" "<session-id>" "<prompt-file>" --model "<provider/id>" --approve </dev/null
```

## prompt 模板

写成文件后作为第二个参数传入；只写设计路径与约束，不内联设计正文：

```text
请激活 easydev skill，严格按 gate 模式执行本任务。

设计：<已冻结 design.md 的绝对路径>（先核对首行版本，只读审查）
目标文件与验收标准：<...>

约束：
1. 先按 gate 执行设计审查（d/3）：发现 P0/P1、范围内 P2、验收缺口或设计分歧时，严禁写代码，停在施工闸前输出分歧清单等待修订。
2. 无分歧后严格按设计编码与自验；保护工作区已有未提交改动，禁止 git commit 与 git push。
3. 跳过内部 r/5 代码审查，停在待终审态，由外层 Antigravity 执行开发终审。
4. 发现任务实际触及 easydev 高风险边界时立即停写回报，不要自行改走 full 或跳过 r/5。
5. 当前为非交互（-p）运行：需要用户确认的命令会被钩子硬拦，遇到时停写回报。
6. 完成后汇报：是否加载 easydev（gate 模式）、设计审查结论、改动与新增文件清单、实际验证命令与真实结果、遗留风险。
```

## `full` 协同分支模板

`full` 协同分支的完整规程以及专用的 [developer prompt 模板](references/full.md#developer-prompt-模板) 与 [reviewer prompt 模板](references/full.md#reviewer-prompt-模板) 均维护在 [references/full.md](references/full.md)。
