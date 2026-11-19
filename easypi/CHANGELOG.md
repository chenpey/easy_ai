# Changelog

## 0.4.0 更新说明（2026-11-19）

- 新增 **`full` 协同分支**（[references/full.md](references/full.md)）：支持开局直接指定或从 `gate` 经用户批准升级到 `full` 协同，保留 `agy ↔ Pi`（会话 A 原 `--session-id`）开发协同，按 `easydev` 模型组配置（`models.json`）解析 `developer` / `reviewer` 模型与深度（配置无效、`inherit` 或不可用时不静默回退，暂停交用户指定），由 `agy` 在外层调起全新 `--session-id` 的独立只读 `Pi` 实例补齐 `d/3`、`r/5` 盲审（含发现逐条核实与 P0/P1 排除复核）及终审两阶段闭环，并继承既有预算。
- 明确区分 `easydev` 原生 `full`（AGY 接管全流程，不调 `Pi`）与 `easypi` `full` 协同分支（`agy` 编排 + 多 `Pi` 会话调用第三方模型），并补齐 `full` 协同专用的 `developer` / `reviewer` prompt 模板。

## 0.3.1 更新说明（2026-11-18）

- `scripts/run_pi.sh` 校验非空 `SESSION_ID`、拒绝额外参数传入裸 `--` 并使用 `cat -- "$PROMPT_FILE"`。
- `SKILL.md` 与 `README.md` 的 `run_pi.sh` 调用示例末尾补充 `</dev/null`，对齐宿主非交互前置钩子。

## 0.3.0 更新说明

- 定位收敛为单一闭环：`agy` 设计 → `Pi` 走 `easydev` `gate` 模式审设计并开发 → `agy` 开发终审。
- 设计冻结与预算衔接：设计方案先落盘冻结版本（唯一事实源，Pi 不另存副本），修订重审计入 `easydev` 的 `d/3`（上限 3 轮），用尽即停并报用户。
- 分派改为 prompt 文件：`run_pi.sh` 第二个参数改为 prompt 文件，设计正文不内联到命令行，避开宿主钩子对双引号内反引号与 `$(...)` 的命令复核。
- 非交互硬拦说明：`pi -p` 无 UI，`force_ask` 级命令（`rm -rf`、`git reset/restore/clean/checkout --`、`commit/push`）会被钩子直接阻断，遇到时停写回报。
- 高风险边界不适用本流程：改按 `easydev` 的 `full` 保留内部独立审查，或经用户明确接受未覆盖风险后执行并披露；运行中发现高风险立即停写回报。
- 第 3 步补齐 `git status --short --untracked-files=all` 后逐个读取未跟踪新增文件全文的核验要求，防止 `git diff` 漏审新增代码。
- 安装命令改为 `mkdir -p` + `ln -sfn` 幂等写法。

## 0.2.0 更新说明

- 明确“审查者不得兼任实现者”与高风险任务的独立审查实例隔离规则。
- 落地“agy 设计 → Pi 按设计走 `easydev` 开发与自验 → agy 对照设计审查”四步编排流程，统一通过 `scripts/run_pi.sh` 调用并补充超时中断恢复、返工上限暂停与项目 `.pi/` 资源 `--approve` 说明。
- 接入仓库 `scripts/check.sh` 版本一致性与格式门禁。
