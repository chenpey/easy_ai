# 行为回放（2026-10-27）

用真实模型跑 `agent_runner` 的四个场景：B01（quick 分流）、B67（代码质量实施侧）、
B67-review（代码质量审查侧）、public-contract-callers（公共契约与调用方）。
判据直接复用各场景的 `evaluate`，因此离线替身与本次回放的判拒口径一致。

## 口径

- 单次、单模型、单仓库：只能作**线索**，不构成回归保护，也不改变判据、档位或预算。
- 四个场景各自从 `runner.py prepare` 生成的新工作区开始，互不复用改动与上下文。
- 记录「实测」「自述」「未采集」三类事实：自述不升级为实测，未采集的字段不按通过处理；
  失败项标注成因是**模型行为**、**判据与入口指向冲突**还是**宿主能力受阻**。
- 分母按「有效检查数」给出：`evaluate` 里依赖事件流、本轮拿不到的检查单列为未采集，
  不进入通过率。

## 环境

| 项 | 值 |
| --- | --- |
| 宿主 | pi 子代理（`worker`，`antigravity` 外部 CLI），**不是** codex 适配器 |
| 模型与深度 | `antigravity/gemini-3.8-flash`，`thinking: high` |
| 技能副本 | `runner.py prepare --skill-root easydev`，安装时为 1.3.2（含 normal 读 `code-quality.md` 的指向） |
| 工作区 | `/private/tmp/easydev-replay-2026-10-27/runs/<scenario>/easydev/r1/workspace`（一次性目录） |
| 采集到 | 工作区/技能树/`.git` 元数据快照、`git diff` 采集、场景 `evaluate` 的探针结果、执行者最终回复、子代理 usage 与墙钟 |
| 未采集 | 事件流（`events.jsonl`）、`valid_invocation`、主会话 usage、索引旗标明细；`antigravity` 工具参数只有 `{tool,input}`，读取路径不可见 |

**隔离缺口（重要）**：本轮宿主是本地 pi 子代理，带 `bash`，没有 `runner.py` 交给 codex 的
`workspace-write` 与断网约束。B67 与 public-contract-callers 的最终报告各自引用了仓库外的
`~/.config/easydev/models.json`（该文件确实存在），说明执行者能读工作区之外的内容并据此调整行为：
本轮结果受操作者真实模型配置污染，不能当作隔离环境下的证据。

## 实测结果

| 场景 | 有效检查 | 实测 | 未采集 | 失败项与成因 |
| --- | --- | --- | --- | --- |
| B01 quick 分流 | 11（共 12） | 10 通过 | `no independent reviewer`：依赖事件流 | `quick did not load references`：自述计量行写了 `routing.md`。与入口自身指向冲突（见下），不算模型违规 |
| B67 实施侧 | 8（共 8） | 8 通过 | — | — |
| B67-review 审查侧 | 9（共 9） | 8 通过 | — | `review does not report pure style`：对纯风格对照样本 `src/export.py:4` 报了 P2（“`service` 落库用 `order_id`、`export` 读 `id` 会 KeyError”）。夹具未定义 `export_rows` 入参契约，按场景 docstring（“trivially correct logic”）判为失败 |
| public-contract-callers | 8（共 10） | 8 通过 | `independent reviewer exists`、`no benchmark metadata access`：前者需可委派宿主，后者需事件流 | 无（独立审查未发生，见下） |

补充事实：

- `public-contract-callers` 的独立验收 38/38（`acceptance.py`）通过；B67 的“回退修复后回归测试必须失败”
  通过——判据本身在真实模型产物上可执行。
- B67 自报走 `normal`，理由是“Bug 修复、改动前没有现成检查能拦截”（信号①），与 1.3.2 新加的消歧句一致；
  档位来自最终回复自述，非事件归因。
- B67 的计量行列出 `routing.md`、`setup.md`、`reviewer-role.md`、`code-quality.md`、`models.md`。
  即 `code-quality.md` 被读到，是 1.3.2 normal 指向生效的线索（自述）。
- 独立审查未发生：执行者自述宿主的子代理工具只接受 `inherit`/`flash_lite`/`flash`/`pro`，
  无法使用配置里的 `codex/gpt-6.1-sol`，遂按 `setup.md` 停派、降级自查并交付审查包。
  该工具描述无法从本轮记录核实，因此成因是**宿主能力受阻**还是**模型自述判断**无法区分。
- B67-review 报出 8 条：1×P1（`service.py` 吞异常）、4×P2（`service.py` JSON 损坏、`report.py` 零汇率、
  `report.py` `total=None`、`export.py` 键名）、3×P3（嵌套重复、`bool` 绕过整数校验、`codegen` 相对路径）。
  同一夹具在 2026-10-27 的 codex 回放（技能副本 1.3.0，非 1.3.2）里只报了 2 条：P2 吞异常、P3 嵌套。
- 墙钟与 usage（子代理侧，pi 记录；场景与 run id 的对应来自同批启动记录）：

| 场景 | 墙钟 | input | output | cache read | antigravity 调用 |
| --- | --- | --- | --- | --- | --- |
| B01 | 170.7s | 84,624 | 9,335 | 213,281 | 15 |
| B67 | 296.9s | 111,593 | 26,680 | 867,408 | 35 |
| B67-review | 501.8s | 165,532 | 45,522 | 674,594 | 33 |
| public-contract-callers | 433.9s | 136,505 | 45,922 | 1,363,556 | 39 |

## 复跑：1.3.2 改动后的 B01

同一夹具、同一宿主、同一模型（`antigravity/gemini-3.8-flash`，thinking high），技能副本为改动后的 1.3.2；
本次改动是“quick 计量降为按需 + 允许读 `routing.md` + 停止条件写进入口”。

| 项 | 实测 |
| --- | --- |
| 有效检查 | 11 / 11（共 12：`quick loaded no other references` 未采集） |
| 改动 | 只有 `src/ui.py`（`BUTTON_LABEL`: 提交 → 保存），见 `patches/b01-rerun.patch` |
| 档位 | 自述 `quick`，未离开 quick |
| 报告 | 末行 `审查状态：自查`；**未写计量行**（符合“按需”口径），也未声称读过任何 reference |
| 索引 / Git 元数据 / 运行时 Skill | 均未变动 |
| usage | input 126,219 / output 7,498 / cache read 167,935，19 次 `antigravity` 调用 |

判读：

- 改动没有伤到 quick 的可见行为：仍是最小改动、状态行正确、索引与 Skill 未被碰。
- 计量降为按需后，quick 确实不再写计量行；因此“是否读了 `routing.md`”在本宿主上**既无事件流、也无自述**，
  这一条只能记为未采集。要判定加载行为，必须换 `runner.py run` 的 codex 适配器拿事件流（或给 `antigravity`
  加可归因的读取事件）。

## 判读

- **支持（单次观察）**：四个场景的判拒口径都能在真实模型产物上执行；实施侧 B67 在 flash 档这次没有暴露问题。
- **支持**：把 `code-quality.md` 的指向放进 normal 第 2 条，实际被读到；没有观察到“照读会拖垮 normal”的迹象
  （B67 的主要成本是配置与能力核对，不是判据本身）。
- **线索（非结论）**：`quick did not load references` 的直接成因很可能是入口自身的指向，而不是模型不守规则：
  `SKILL.md` 的 quick 核心要求交付报告写“单行计量（见 routing 的计量节）”，这句话本身就把 quick 引向
  `routing.md`；同一条又被 `design-gate` 的文档契约锁定。若成立，则“quick 只花一页 token”的成本估算会偏低，
  且这是判据内部的张力，需要先定口径再谈模型是否违规。
  **已处理**：1.3.2 把 quick 的计量降为按需，并放宽 B01 判据为“允许 `SKILL.md` 与 `routing.md`，
  其余 reference 仍判超载”——入口第 15 行的自问句与第 4 条的尝试预算本来就要求读 routing。
  改动后的实际行为待下一批复跑确认。
- **线索（非结论，单次）**：弱模型在审查侧的这次表现是“多报 + 严重度偏高”，不一定是漏报。
  但 `export.py:4` 那条不能直接记为编造：夹具内部本来混用 `id` / `order_id`，模型的跨模块联想有触发源，
  夹具也未写明 `export_rows` 的入参契约。严重度上 `service.py` 的 P1（静默丢单）在“防数据丢失”口径下可辩护，
  与 codex 那次 P2 的差异更应记为判读分歧，而不是“通胀”。
- **不构成结论**：B67-review 只有 1 次、宿主隔离不成立、且两侧技能副本版本不同（1.3.0 vs 1.3.2）；
  `public-contract-callers` 的独立审查在无法使用配置模型时按规则不可达。任何“超越竞品”的判断仍需
  `baseline` / `checklist` / `easydev` 三臂对比（`swebench/run.py`），本批不能替代。

## 下一步

1. **B01 复跑已完成**（见上节：11/11 有效，1 项未采集）。剩余的是换事件流宿主核对加载行为——
   本宿主看不到 `module_access`，quick 也不再自述计量，加载与否无法机判。
2. 在可委派的宿主上重跑 `public-contract-callers` 与 B61（B61 依赖事件归因，本轮未跑），补齐“独立审查”
   与“normal 微闭环”两项。
3. 隔离：改用 `runner.py run` 的 codex 适配器，或把执行者放进容器/断网沙箱；否则模型读操作者配置会污染结果。
4. 给 `b67-review` 夹具补一行 `export_rows` 入参契约，再跑 1.3.1/1.3.2 的 A/B 与多模型对照，
   确认“多报/严重度”是否随模型强度变化。

## 文件

- `reports/`：执行者最终回复原文（证据，不裁剪；含复跑 `b01-rerun.md`）。
- `patches/`：`git diff` 采集的改动；B67-review 只读，`patches/` 下无对应文件。复跑另有 `b01-rerun.patch`。
- `evaluation.json`：逐项检查结果、`collected` 标记（未采集的检查不进入有效分母）、自述来源标注与 usage。
