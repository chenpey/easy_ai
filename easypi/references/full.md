# `full` 协同分支（保留 `agy ↔ Pi` 协同）

在保留 `agy ↔ Pi` 协同的前提下，通过 `Pi` 调用 `models.json` 指定的第三方或跨供应商模型完成 `easydev` `full` 闭环；与 `easydev` 原生 `full`（AGY 接管全流程、不调 `Pi`）互斥。

## 1. 触发、批准与预算继承

- **触发**：开局指定 `/easypi` 走 `full` 协同，或在 `gate` 遇高风险边界/重大影响/用户要求经用户明确批准升级。
- **预算继承**：沿用 [workflow.md#gate-模式](../../easydev/references/workflow.md#gate-模式) 升级规则——保留已耗 `d/3` 与尝试数，`r` 从 0 起算（`gate` 终审与返工只作移交证据）。
- **调用与恢复**：[run_pi.sh](../scripts/run_pi.sh) 的 `force_ask` 硬拦、可选硬超时、15 分钟巡检、中断恢复、`--approve` 与“`Pi` 运行期 `agy` 不改工作区”沿用 [SKILL.md](../SKILL.md) 第 2 节。

## 2. 模型组配置与不可用兜底

**CLI 适配覆写**：本节以 `inherit` 不静默回退、须暂停交用户指定，覆写 [workflow.md#full-编排](../../easydev/references/workflow.md#full-编排) 中“`developer` 模型与深度均为 `inherit` 时由主代理写入”的默认约定（`run_pi.sh` 只能按显式 `<provider/id>` 分派）。

分派前 `agy` 按 [environment.md#模型配置](../../easydev/references/environment.md#模型配置) 解析 `developer`、`reviewer` 的 `model`/`reasoning_effort`（含项目级 `.easydev/models.json` 外发安全门槛；开局或从 `gate` 升级未锁定时解析并锁定北京时间 `windows`，无命中记 `default`，已锁定则沿用），映射到 [run_pi.sh](../scripts/run_pi.sh) 的 `--model` 与 `--thinking`（`off`–`max` 传 `--thinking <effort>`，`inherit` 省略，禁传 `--thinking inherit`）。

- **写入者与 `designer` 核实**：进入 `full` 协同即授权 `Pi` 会话 A 任唯一写入者（报告披露写入者覆盖）。同步解析 `designer`：模型与深度均为 `inherit` 由 `agy` 设计，任一非 `inherit` 不得静默顶替——暂停请用户确认覆盖（报告披露）或派独立只读 `Pi` 按该配置设计。
- **不可用与 `inherit` 暂停**：选中配置无效/不可读、`developer`/`reviewer`（或独立 `Pi` `designer`）解析为 `inherit`，或调 `run_pi.sh` 时目标模型不可用（非瞬时抖动）：**严禁静默回退、改用 `pi` 默认或由 `agy`/会话 A 自审替代**。立即暂停报受阻角色、原模型与原因，交用户指定 `<provider/id>`（及可选 `--thinking`），或明确选择改用 `pi` 默认 / 切回 AGY 原生 `full`。

## 3. 三方角色隔离与 `d/3`

- **`designer` 与编排者**：按 §2 核实值（默认 `agy`，或派独立只读 `Pi` 使用独立 `--session-id <sid>-des`），维护冻结 `design.md`、scratch 产物、账本与 `git` 校验，不充当 `r/5` 审查者。
- **`developer`（唯一写入者）**：`Pi` 会话 A（`--session-id <sid>` + `developer` 模型），按 [developer prompt 模板](#developer-prompt-模板) 履 [开发角色](../../easydev/references/roles.md#开发角色)，自验后停待审查态；**严禁会话 A 内部启动 `easydev full` 或自审 `r/5`**。
- **`reviewer`（独立只读第三方）**：`agy` 通过 `run_pi.sh` 调起**全新 `--session-id`**（`<sid>-d1`、`<sid>-r1`、`<sid>-final`，严禁复用 `<sid>` 或 `<sid>-des`）+ `reviewer` 模型的只读 `Pi`，按 [reviewer prompt 模板](#reviewer-prompt-模板) 盲审。
- **`d/3` 设计审查**：
  - **开局 `full`**：独立只读 `Pi`（`<sid>-d1`）核对 `design.md` 首行版本并盲审（必做 §4.2 只读物理校验），保留 `**[建议]**` 通道（[design.md#设计审查与建议通道](../../easydev/references/design.md#设计审查与建议通道)，施工闸前由 `agy` 逐条记「接受 / 拒绝」）；无阻塞分歧再开闸，`d/3` 用尽或受阻即停。
  - **`gate` 升级**：升级前 `d/3` 仅由会话 A 执行时，按 [workflow.md#gate-模式](../../easydev/references/workflow.md#gate-模式) 补 1 轮独立审查或经用户确认沿用并披露非独立 `d/3`；`d/3` 用尽停写交用户裁决，不越顶补审。

## 4. 独立 `r/5`、发现核实与终审两阶段

1. **送审前核对（agy）**：开工记 `git rev-parse HEAD`（无提交注 unborn），按 [基线与敏感快照](../../easydev/references/environment.md#基线与敏感快照) 存脏/未跟踪文件首次原内容至 `baseline/`；会话 A 停写后核 `git status --short --untracked-files=all </dev/null`、未跟踪新文件全文与 `git diff HEAD`（无提交用 `--cached` 与 `diff`），确认加载 `easydev`（开发角色）、无越界且有真实验证输出（缺失由 `agy` 补跑）。
2. **独立盲审与只读物理校验**：**每次**只读 `run_pi.sh` 调用（`<sid>-des`、`<sid>-d1`、`<sid>-r1`、`<sid>-final*` 及阶段 2 续发）前，记 `git rev-parse HEAD`、`git status --porcelain --untracked-files=all`、`git diff --binary HEAD`（无提交用 `--cached` 与 `diff`）、未忽略的未跟踪文件（含子模块递归）及 scratch 下 `design*.md`、`ledger.md`、`findings.md`、`baseline/` 的 SHA-256（忽略文件如缓存/虚拟环境不做逐文件哈希）。派独立 `reviewer` 按 [审查规程](../../easydev/references/review.md)（高风险最低「深」，输出「覆盖账」）盲审计 `r/5`。**退出后逐项比对快照**，除 `<sid>-des` 允许写入/修订 scratch 下 `design*.md` 外，其余全部须零改动方可采信；发生意外写入立即拦截报错。
3. **核实与有限修复**：按 [workflow.md#审查与修复](../../easydev/references/workflow.md#审查与修复) 与 [报告规则](../../easydev/references/review.md#6-报告规则) 核实（**P0/P1 排除、降级或转风险须 `reviewer` 复核证据**；潜在 P0–P2 待验证风险须最小确认；高风险「深」审查提出的动态验证或最小确认项由 `agy` 执行或派回会话 A 只验不改，并将命令与输出回传 `reviewer` 复核）。仅将确认的 P0/P1、范围内 P2 或验收缺口发回会话 A（同 `--session-id`）修复重验（同根因限 3 次）；P3 记位置与代价不进修复；根因在设计须升版重走 `d/3`（`r<2` 不得单开设计重审）。
4. **终审两阶段**：修复后冻结最终 diff，调起**全新 `--session-id`**（`<sid>-final`，多轮用 `-final2`…不复用）按 [review.md#终审两阶段](../../easydev/references/review.md#终审两阶段) 执行：阶段 1 盲审最终 diff；阶段 2 复用同 `--session-id` 补发历史清单与修复 diff 核对「已关闭 / 未关闭」与前提变化，合计 1 轮 `r`。
5. **收口与上限**：按 [完成边界](../../easydev/references/engineering.md#完成边界) 交付或报**部分完成/待确认**，报告明示 **`easypi full 协同`**、`d/3` 与 `r/5`、三方生效模型（`designer` / 会话 A `developer` / 各独立审查 `reviewer`）、写入者/设计者覆盖、设计建议接受/拒绝、确认问题关闭状态、文档校准状态、P3 建议、遗留/未验证项、验证结果及 `gate→full` 升级披露；任一上限用尽暂停报用户。

## developer prompt 模板

写入 scratch 下 `dev-prompt.md` 传入会话 A（`--session-id <sid>`）：

```text
请激活 easydev skill，严格以开发角色（developer）执行，严禁在本会话启动 full 或自审 r/5。

角色：developer
规程：<easydev/references/roles.md#开发角色 绝对路径>
设计：<冻结 design.md 绝对路径>（先核首行版本，严格按过闸设计实现）
冷启动摘要：<HEAD SHA；git status --short；受影响目录与验证入口（≤1KB，不含凭证与正文，不替代自行核对）>
目标文件与验收标准：<目标路径（已知切入点附符号或 #L起-止）与可观察验收>

约束：
1. 仅开发与自验：保护已有未提交改动，禁止 git commit/push/add。
2. 遇设计阻塞或未预期范围/契约变化立即停写回报，不自行改设计。
3. 非交互（-p）运行：需确认命令会被钩子硬拦，遇到即停写回报。
4. 完成编码与完整验证后停在待审查态，交外层安排独立 r/5 审查。
5. 汇报：是否加载 easydev（开发角色）、改动与新增清单、验证命令与真实结果、遗留风险。
```

## reviewer prompt 模板

写入 scratch 下 `review-prompt.md` 传入全新 `--session-id`（`<sid>-d1` / `<sid>-r1` / `<sid>-final`）：

```text
请激活 easydev skill，严格以只读审查角色执行本轮独立审查。

角色：reviewer
规程：<easydev/references/review.md 绝对路径>（强度：<标准 | 深>）
任务范围与验收标准：<原始需求与验收；冻结 design.md 路径（核首行版本只读审查）；已知切入点符号或 #L起-止（仅定位，不收窄完整 diff 与调用链范围）>
审查阶段：<设计审查 d/3 | 施工初审 r/5 | 终审第一阶段（盲审）>
冷启动摘要：<HEAD SHA；git status --short；受影响目录与验证入口（≤1KB，不含凭证与正文，仅冷启动导航）>
基线与变更：<基线说明；代码审查自行核对 git status --short --untracked-files=all、未跟踪新文件全文与 git diff HEAD（无提交用 --cached 与 diff）>
已执行验证：<命令与真实结果摘要（设计审查填“无”）>

约束：
1. 严格只读：禁改工作区、Git 索引或执行写入/提交命令。
2. 待审仓库测试、构建、依赖安装与脚本按不可信代码处理，不执行；只静态检查并报未验证范围（见 review.md「不可信代码执行」）。
3. 独立盲审：按 review.md 输出「覆盖账」、确定发现（**[P级] 简短标题**）、待验证风险与验证限制；无失败路径的单列 `**[建议] 简短标题**`（位置 + 依据）；零发现亦须给覆盖账。
4. 终审第一阶段仅基于最终 diff 盲审，不预读历史清单；待外层同 --session-id 续发第二阶段清单与修复 diff 后再逐条核「已关闭 / 未关闭」。
```
