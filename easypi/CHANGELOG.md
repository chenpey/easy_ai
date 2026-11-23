# Changelog

## 0.6.2 更新说明（2026-11-23）

- **派单模板冷启动摘要**：`references/gate.md` 与 `references/full.md` 的 `gate`、`developer`、`reviewer` 模板补充 `≤1KB` 冷启动摘要（`HEAD` SHA、`git status --short` 摘要、受影响目录与验证入口命令）与 `#L起-止` 行号锚点，并明示摘要不含凭证与文件正文、不替代 `baseline/` 首次原内容保存与自行 `git status`/`git diff` 核对；`reviewer` 模板注明切入点仅作定位、不收窄完整 diff 与调用链的审查范围；`SKILL.md` 入口与 `references/gate.md`「使用方式」同步该口径。

## 0.6.1 更新说明（2026-11-19）

- **口径与路径澄清**：`SKILL.md` 明确「返工达 2 轮或同根因 3 次修复任一上限即暂停」（4986 字节 ≤ 5000）；`README.md` 将 `scripts/run_pi.sh` 明确为 `easypi/scripts/run_pi.sh`。

## 0.6.0 更新说明（2026-11-19）

- **模板归位与口径对齐**：新增 [references/gate.md](references/gate.md) 承接 `gate` prompt 模板（同步「禁止 `git add`」与 `**[建议]**` 通道）及 `baseline` 敏感快照安全细则；修复 `README.md` 与 `SKILL.md` 的 `--model` 传参口径冲突，并在 `references/full.md` 显式标注 `inherit` 禁令为 CLI 适配覆写。
- **安装契约补齐**：`scripts/smoke-install.sh` 新增场景 7 校验 `easypi` 整目录软链安装契约，并同步更新 `README.md` 描述。
- **破坏性 CLI 迁移披露**：共享守卫 `agent-hooks/check_command.py` 已无兼容别名地将 `--mode antigravity` 更名为 `--mode json`（缺省同步改为 `json`），同步更新 Antigravity `hooks.json` 与 Pi `enforce-noninteractive.ts`；升级后须通过 `sync-agent-rules` 同步更新全部三个部署副本并禁止混合版本运行窗口。

## 0.5.0 更新说明（2026-11-19）

- `scripts/run_pi.sh` 加固：额外参数改为白名单（`--model`/`--thinking`/`--approve`，取值非空白且不以 `-` 开头），新增 `EASYPI_TIMEOUT` 硬超时（默认 900 秒，超时以 124 退出），正常退出也清理同组残留子进程（1 秒后强杀），并把创建子进程窗口纳入信号屏蔽与同一 `try/finally`，避免竞态泄漏整组进程。
- `full` 协同分支补齐基线与只读物理校验：开工记录 `git rev-parse HEAD` 并把任务相关脏/未跟踪文件首次原内容存入 `baseline/`；每次只读 `run_pi.sh` 调用都核对 `git status --ignored`、`git diff --binary HEAD`、未跟踪/忽略文件与 scratch 产物的 SHA-256；`designer` 配置非 `inherit` 时不得由 `agy` 静默顶替，`d/3` 用尽或受阻时停写报用户。
- 同步 `SKILL.md` / `README.md` / `references/full.md`：设计审查保留 `[建议]` 通道，开发者禁止 `git add` 以保持索引干净，终审与报告改用 `git diff HEAD` 并补文档校准与遗留项，`--model` 按 `gate`/`full` 语义处理。

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
