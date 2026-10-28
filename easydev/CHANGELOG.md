# 更新日志

版本变更记录。最近一次更新见 [README](README.md)。

## 1.4.1 更新说明（2026-10-28）

修正 1.4.0 的四处真实适配与性能问题（Antigravity transcript 归因、Antigravity Hook 协议、未激活会话开销、脏目标基线协议），并按 v2 复审修正带 transcript 的未激活短路、Antigravity `PostToolUse` 归一与非工具事件 Flat 结构，按 v3 复审修正未激活 child 成功读取 `SKILL.md` 后的局部激活及其读取消费去重；规则正文未新增，总量不变。

- **Antigravity transcript 真实解析（P1）**：`hooks/transcripts.py` 对 `PLANNER_RESPONSE.tool_calls[].args` 的字符串值做配对双引号解码（失败保留原值），`read_file` 的 `Path` 与 `run_command` 的 `CommandLine` 走同一解码；真实日志无 `tool_calls[].id`、工具结果在 planner 行之后连续的独立 `GENERIC` 行，改按 `call_index` 位置映射（优先 `plan_row+1+call_index`，非连续时向前扫描但不跨过下一条 `PLANNER_RESPONSE`），`GENERIC.status == DONE` 不再等同于成功：`run_command` 只从宿主元数据头（`Created At`/`Completed At` 之后、`Command output:`/`Output:` 之前的元数据区，含输出被截断时的 `Here is a preview …` 前言）取 `Command exited with code N`（兼容 `The command exited …`），头部为 0 才确认、非 0 或缺失都不确认，输出正文里的同名字样不能覆盖头部退出码；读取类工具要求 DONE、`content` 是非空字符串、结果头无明确失败标记，且结果头里有整行匹配的带行号范围读取结果行（`Showing lines <int>-<int> of <int>`、`Showing lines <int> to <int>`、`Partial read (lines <int>-<int> of <int>). Showing bytes <int>-<int> of <int> in this line range, truncated at the <int>-byte limit.` 及其两种已知续读指引（`call this tool again with the same StartLine and EndLine and ByteOffset=<int>.` / `read it with a larger byte budget.`））或完整措辞的真实截断通知（`The tool's output was truncated due to being too long. …`）；`Created At`/`Completed At`、`File Path`/`Total Lines`/`Total Bytes` 与加行号说明只是宿主包装，只用于让证据扫描跨过它们，不能单独证明读取成功（只有时间戳、时间戳后只有空白、正文里才提到这些字样、首段说明文字恰好以 `Showing lines`/`The tool's output was truncated`/`Here is a preview` 开头、数字后粘连文本与数字格式示例句、`Partial read` 点号后的任意其它尾句或缺少字节范围/上限说明都保留 requested），缺 content、空 content、非字符串 content 同样保留 requested（“没有 error 字样”不再是成功证据）；无真实 ID 时用稳定合成 ID `ag:<planner 偏移>:<call 序号>` 参与同 ID 冲突/去重。以本机 13 个真实 `~/.gemini/antigravity/brain/*/…/transcript.jsonl` 只读实测：修正前 `references` 恒为空、`confirmed` 全为 false、`confirmed_skill_read_offset` 恒为 `-1`；修正后 13 个中 8 个取得 `references`，`confirmed_skill_read_offset` 在 6 个会话上返回真实偏移。
- **Antigravity Hook 协议（P1）**：模板改为命名 Hook 组 `{"easydev-hooks": {"SessionStart": [{"hooks": [{"type": "command", …}]}], …}}`；`install.py generate` 为每个事件槽位生成带 `--event <EventName>` 的命令，`_entry_records`/`apply`/`uninstall` 识别命名组（顶层 `hooks` 仍兼容 Codex/DSH，merge 保留其他命名组，`probe→enforce` 仍替换而不追加）；`core.py` 新增 `--event`（stdin 无事件名时以 CLI 事件为准，stdin 有时以 stdin 为准），支持 `workspacePaths` 数组（取第一个可用路径，多个时告警）并保留 `workspacePath`；Antigravity `PreToolUse` 放行输出 `{"decision": "allow"}`，DSH `PreToolUse` 放行输出 `{"permissionDecision": "allow"}`，Codex/pi 契约不变。未在真实宿主执行 `--mode probe`，不宣称 Antigravity/DSH 已端到端验证。
- **未激活会话 O(1) 短路（P2）**：`handle()` 最入口只读**本事件所属仓库分区**的 actor 状态（`<state_root>/actors/<hash(host, session, actor)>.json`，一个 0600 小 JSON），分区由新增的廉价仓库根 `paths.cheap_repo_root()` 定位——从 tool cwd（无则 record.cwd）逐级向上找 `.git` 目录/文件，不启动 Git 子进程，无 cwd 时直接走完整判定。actor 明确为未激活且事件不是 `input`/`child_start`/“读取精确 `SKILL.md` 的 post”时立即按宿主协议编码放行，不调 Git、不 `_prepare_write`、不建锁、不写状态、不再刷 post 告警；首次事件走一次完整判定并写下只含 `activation_scope` 的锚点，此后才是常数开销，状态不可读时退回完整判定并按既有 fail-open 告警放行。分区与权威 actor 状态一致，因此同一会话在另一 Git 仓库的未激活判定不会掩盖本仓库的激活状态（r1 审查的跨仓库漏拦源于此前按 host/session/actor 全局共享的 `sessions/` 索引，该索引已删除，也不再存在“写/删失败后陈旧 inactive 长期生效”的问题）。本机实测未激活 `PreToolUse(Write)`：修正前 56.3 ms/次（进程内，Git 子进程为主），修正后首次 59.6 ms、稳态 0.46 ms（Codex）、0.48 ms（DSH，并返回 `{"permissionDecision": "allow"}`）、0.45 ms（Antigravity，并返回 `{"decision": "allow"}`）；CLI 总耗时仍受解释器启动与 import（约 57 ms）限制，未改变宿主侧进程模型。
- **脏目标基线改为自动快照（P1 UX/安全）**：不再要求模型先提供 `EASYDEV_TASK_ROOT` 与手工 `baseline/`。需要内容基线的目标（dirty/untracked/unreconstructible）在 `_prepare_write` 预取阶段（task 锁外）把**首次**当前内容保存到 `<state_root>/baselines/<task_key>/<repo_id>/<相对路径>`：目录 0700、文件 0600、逐层 `dir_fd + O_NOFOLLOW`、`os.link` 发布不覆盖；第二次写不覆盖首次快照。源文件非普通文件/软链/超过 8 MiB/不可读 → `deny` 并给出可操作原因；`clean_tracked`/`absent` 不要求快照；成功 post 后的外部修改仍 `deny external_modification`；显式 `EASYDEV_TASK_ROOT`/机器 `task_root` 存在时优先旧的手工核对路径（含 `reused_task_root`/epoch 复用限制），不再出现 `missing_task_root`。README 与 `hooks/README.md` 明确该快照是私有缓存、不是业务备份，Hook 仍不写业务文件。
- **带 transcript 的未激活短路与同回合 SKILL 激活（P1）**：`_activate_input` 只在 actor 已激活（`task_active`/`role_only`）时凭 `transcript_key == last_transcript_input` 早退，未激活时继续处理 `skill_read_offset`；`_handle_state` 只要 `session_id` 存在且未激活就写 `activation_scope: "inactive"`（不再用 `not actor`，此前 `last_transcript_input` 先写入 actor 导致锚点永不落盘，带 `transcript_path` 的真实首回合之后每次工具调用都重付 Git 探测与写预取并刷 post 告警）；只有 `activation_scope == "task_active"` 才执行 `_prepare_write`，`_run_rules` 在 `not scope_active` 时对所有事件直接返回（含 `post_tool`/`child_done`）。Codex 真实 `UserPromptSubmit`/Pi `input`（带 transcript、未激活）后后续 `PreToolUse(Write)` 零 Git 子进程、零新锁、零告警；Antigravity `PreInvocation #1`（带 `transcriptPath`、无 `prompt`）→ 同回合 `PostToolUse(read_file SKILL.md)` 成功 → `PreInvocation #2` 能激活（同一 `USER_INPUT` 记录键不再挡住），已激活时的同记录去重与停用边界不变。
- **Antigravity `PostToolUse` 归一（P1）**：`FIELD_TYPES["antigravity"]` 补 `error`/`invocationNum`/`initialNumSteps`；`normalize_antigravity` 把官方整型 `stepIdx` 转成回合键（不再被 `_string()` 丢成空串），`toolCall` 内无 `id` 时回退合成 ID `step:<stepIdx>`（官方载荷没有调用 ID，同一步内多调用共用该 ID，写配对按 ID 覆盖，已在 `hooks/README.md` 披露），`PostToolUse` 按顶层 `error` 判定结果（非空字符串 = 失败，缺失或空串 = 成功，不依赖真实宿主没有的 `status`）。此前真实 Antigravity 下 `record.outcome`/`tool["id"]` 恒为空，导致 `SKILL.md` 直读无法激活、`pending_writes`/`last_success`/`write_seq` 永不建立。
- **Antigravity 非工具事件 Flat 结构（P2）**：`hooks/templates/antigravity.json` 只保留 `PreToolUse`/`PostToolUse` 的 Grouped 包装（`matcher` + `hooks[]`），`SessionStart`/`PreInvocation`/`Stop` 改为官方 Flat handler 对象（`{"type": "command", …}`）；`install.py` 的条目识别/合并/卸载本来就按清单摘要匹配条目（`_entry_command` 优先取顶层 `command`），Flat 条目照常 `probe→enforce` 替换、保留用户改动、只动 `easydev-hooks` 组；`check.sh` §6b 改为按事件类别断言结构（工具事件必须有 `hooks` 数组与 `matcher`，非工具事件必须 Flat，`PreInvocation` 必须注册），并保留 `--event` 注入检查。
- **未激活 child 成功读取 `SKILL.md` 的局部激活（P2）**：此前 `_run_rules` 的全事件未激活早退发生在 `_record_post` 之前，而 `_restore_from_skill_read` 又排除 child，导致未激活子任务即使成功读取精确安装路径的 `SKILL.md` 也保持 `inactive`，随后的嵌套分派被静默放行。现把 `_record_post` 的 child 局部激活分支抽成 `_activate_child_local_scope`，在未激活早退处只对“可信 child + 成功读取精确 `SKILL.md`”的 post 调用它：只把该 actor 置为 `task_active`（`role_source: skill_read`），不建立父任务绑定、不写父任务写序列、不开新 epoch；失败、只有 requested/结果未知、其他路径的同名文件都不激活。普通未激活 child 的 post、根 actor 的未激活 post 仍不进入 `_record_post`、不刷告警，未激活根会话的 O(1) 零 Git 稳态短路不变。该局部激活与根路径共享“同 actor、同工具调用 ID 只能作为一次激活证据”：首次接受时把调用 ID/digest/epoch 写入 actor 级 `consumed_read_ids`/`consumed_reads`（不写任务缓存、不建父任务/epoch），已 `task_active` 的 child 成功读取也补记该证据但不重复激活；因此显式 `active=false` 之后重投同 actor、同调用 ID 的成功读取（同参数/同结果，或改参数/改 `success`↔`completed` 拼写）都保持 `inactive`、嵌套放行，只有新的调用 ID 的成功读取才重新启用本 actor 的禁嵌套约束。
- **测试提速与门禁**：`tests/hooks/support.py` 改为 `setUpClass` 一次建模板 Git 仓库并逐例 `copytree`，协议与规则用例经进程内 `core.run()` 驱动（CLI 入口、安装器与分发启动仍用子进程）。hooks 套件从 546 项 458.8 s 降到 608 项 73.5 s（本机 macOS，`<120 s` 目标达成），后续修复轮补测试后为 758 项（约 110 s）；CI 的 Hook 离线矩阵超时从 10 分钟调回 5 分钟。新增 `test_baseline_auto.py`、`test_short_circuit.py`、真实形状的 Antigravity transcript 夹具与命名组安装用例；`test_transcripts`、`test_install`、`test_protocol` 同步更新。v2 复审修复再补：带 transcript 的未激活锚点/零开销/同回合 `SKILL.md` 激活（`test_short_circuit.py`）、官方 Antigravity 载荷的激活与写序列闭环（`test_protocol.py`）、Flat 非工具事件的生成/apply/uninstall/用户改动保护（`test_install.py`）；v3 复审修复再补：未激活 child 成功读取 `SKILL.md` 的局部激活与禁嵌套，以及局部激活的读取消费去重（显式停用后重投旧成功读取仍 inactive、新调用 ID 才重新激活；`test_short_circuit.py`，含 Pi 环境变量 child 的真实 CLI）。FIFO 写入目标由“告警降级”改为“拒绝”（非普通文件没有可信首次内容），对应旧用例改名并改断言。
- **版本**：根 README、`easydev/README.md`、`SKILL.md` metadata、本 CHANGELOG 与 `hooks/manifest.json` 同步为 1.4.1；规则文档总量未新增（仍为 131715 字节，棘轮与 1.3.2 的双写数值不变）。

未验证：四宿主真实 Hook 信任、stdin 结构与输出消费仍需各自 probe（本版未在真实 Antigravity/DSH 进程里执行 `--mode probe`，也未做端到端拦截验证；Antigravity 的 Grouped/Flat 事件结构与 `PostToolUse` 的 `error`/整型 `stepIdx` 只按官方文档与离线夹具核对）；Antigravity 只做真实 transcript 只读归因实测，桌面端未装 Hook；DSH 本机无 CLI；pi 适配器仍只有 Node 夹具。离线全绿不等于宿主强隔离证明。

## 1.4.0 更新说明（2026-10-27）

按 `ROADMAP.md` 落地可选的 Hook Plugin 与回放 Harness 增强：规则正文只做必要措辞修正，确定性检查放进独立 Plugin，不进入 Skill 安装集。契约设计与验收口径见任务设计 v3（外部记录），本版不声明真实宿主已端到端验证。

- **P0 transcript 归因**：`tests/experiments/role_metric.py` 新增 `--host pi|antigravity|codex`（默认 pi）、`--transcript`、可重复 `--child-transcript` 与可选 `--role-map`；pi 默认行为与 JSON 字段保持不变。Antigravity 解析 `PLANNER_RESPONSE.tool_calls[]` 的明确读工具与静态读命令，Codex 分开支持 rollout（`response_item/function_call`）与 `exec --json`（`item.*`）两种格式，同 ID 优先 completed、畸形行计数、只把有可关联成功结果的读取标为 `confirmed`；交接 prompt、补丁与 `update_plan` 里的路径不再污染读取统计。非 pi 输入必须显式文件，不扫描用户会话目录。
- **P0 回放读取守卫**：`tests/experiments/agent_runner/replay_read_guard.py` 在隔离 `CODEX_HOME`、所有 before 快照之前生成 `<run_dir>/policy/` 受信脚本、policy 与工作区 `.codex/hooks.json`；codex invocation 增加 `--dangerously-bypass-hook-trust`（跳过 Hook 信任确认，不跳过 sandbox/approval）。守卫对 canonical workspace 与被测 Skill 副本两个允许根之外的可观察读取一律 `deny`，覆盖 HOME 内外、sibling、软链、`..`、显式系统路径；递归读取前 lstat 不跟链枚举整棵候选树，`rg --follow`/`find -L`/`ls -L` 固定拒绝，每调用累计 20k 入口/2s，超限或异常也 `deny` 并记脱敏健康失败；模型前探针失败不启动回放。
- **P1–P3 可选 Plugin**：新增独立 `easydev/hooks/`（Python 共享核心、四宿主 envelope、状态/锁、shell/路径解析、唯一 models validator、transcripts、installer、pi 薄适配器与原生模板）。P1 覆盖破坏性 Git（包装解包、`-C`、多段、`reset --hard`、`checkout --`、`restore` 区分 `--staged`、`clean -f`、`stash`、强推）、脏/未跟踪/不可重建目标的基线只读核对与 reviewer 只读；P2 校验 models（来源顺序、无合并、重复键、完整 ID、升级相邻差异、局部覆盖合成后复验）、reviewer 交接格式与可证 child 的禁嵌套；P3 只在根 actor 正常 settle 且审查后又出现代码写入时每回合提醒一次，可信 child 写进入共享写序列、checkpoint 取 `max`、旧 epoch 不归并。运行期故障告警放行；回放守卫不复用 fail-open。
- **安装与文档**：`hooks/install.py` 默认只生成到显式 `--output`，`apply` 才按唯一 command 标识合并、保留原字节备份并原子写入；`uninstall` 只移除本 Plugin 条目。`hooks/README.md` 写四宿主安装/probe/激活/能力差异/降级/卸载与 Windows 限制；`easydev/README.md` 增加可选 Plugin 入口。Plugin 不安装 Skill/tests，不改 models 与用户宿主配置。
- **模型措辞与门禁**：`references/models.md` 的“不另设运行时插件”改为“Skill 无内置运行时依赖；可选 Plugin 复用同一 schema 预检”，并用同节精简抵消新增字节。`check.sh` §1 纳入 `*.ts`；§3 把 `hooks/manifest.json` release 纳入 easydev 版本组；§6 改为 import `hooks/models.py` 唯一 validator 并保留 README 跨供应商示例断言；新增 §6b 的 Python AST/JSON/pi 契约静态检查；§9 新增“SessionStart 不注入/不 overwrite/不定档、Plugin 分发与安装器不含 Skill/tests”契约。§10 范围不变，总量实测降至 131715 字节并向下重设棘轮，`MAX_TOTAL` 与本节合计不得超过 131715 字节双写同步，不上调。
- **测试与 CI**：新增 `tests/hooks/` 一组离线用例文件（当前 16 个 `test_*.py`，含 r3–r9 修复回归）与 `agent_runner/test_replay_read_guard.py`，按“合规放行、违规拒绝、未激活放行、畸形/异常降级”四分支覆盖四宿主 envelope，并回放激活/role_only/共享写序列/双审查逆序/换任务旧根拒绝/显式文档排除等场景；`tests/README.md` 增加第八项命令，`tests/behavior.md` 登记 B68；CI 新增 Hook 离线矩阵项（不装 PyYAML、10 分钟），原九项保留。

- **P3 完成事件与父子绑定（施工审查后修订）**：完成事件只认结构化成功终态 + 结构化报告，显式 `failed`/`cancelled`/`timeout`/`running` 优先排除，非空报告不能把失败终态反转成 completed。Codex 按官方子 Hook 契约（子事件 `session_id` 是父 session、`agent_id` 才是 child actor）关联：模板注册的 `SubagentStart`/`SubagentStop` 用父 session + child `agent_id` 匹配父分派记录，完成后由 `SubagentStop` 的 `last_assistant_message` 计入；无需宿主不存在的 `parent_session_id`，无法关联时明确告警并只保留 `role_only`，不宣称已支持。pi/Antigravity/DSH 标记 `completion_events=false`，可加载 `Stop` 但无法可靠归因审查后写入，P3 不启用并披露能力缺口。父子关联按父分派 pre 记录（分派 ID、初始任务摘要、父 session、epoch）固定 epoch：已有绑定的 actor 重投 `SubagentStart` 时优先返回既有绑定，绝不凭“当前唯一待关联”改绑新 epoch；匹配缺失/歧义或 epoch 已退役时只保留 `role_only`，不写父任务写序列/基线/审查关系；root 状态键含 host+root session+root actor。写入配对键含 actor + task epoch + 调用 ID 并核对目标集合，父子同名工具 ID 互不覆盖。
- **状态与预算加固（施工审查后修订）**：私有状态只规范化已核实的系统临时根别名，其余层级 `dir_fd + O_DIRECTORY|O_NOFOLLOW` 逐层 `fstat` 校验；`task_root` 从其祖先链起逐层核验（任何一层软链即拒绝，只有逐层核验成功的安全路径才作为旧根归属键），`baseline/` 与备份父链同样无跟随打开；业务文件或其仓库内父链是软链时拒绝写入。状态、baseline 备份、models 与 transcript 的打开统一 nofollow + nonblock，`fstat` 普通文件后才做有界读取，命名管道等替代品不会阻塞；一次调用的 1500ms 内部截止由 Git/指纹/transcript 共享。Git 查询、指纹与分派前的仓库/models 读取在 task 锁外完成，持锁后只重算 root/task/epoch 身份并按锁外已取证上下文签名复核（锁内不启动 Git、不做配置全文读取），签名变化即丢弃预取并降级、不向旧 task 提交；状态故障只降低依赖状态的规则，已取证的破坏性 Git 与 reviewer 只读拒绝在锁忙时仍保留。
- **r4 施工审查修复**：退役 child 保留不可再解释的 child→原分派/epoch 墓碑（两次 `SubagentStart` 重投不再被唯一兜底改绑新 epoch）；状态 JSON 解码/结构故障统一转为状态不可用，锁内任何可捕获故障都保留已确定的危险 Git 与 reviewer 只读拒绝，`--mode probe` 的提前返回一律只观察；无命令名的重定向、包装内相对 `env -C`、`cp`/`mv`/`install` 的目录目的与 `-t`/`-T` 语义都落到真实目标；业务软链在仓库发现之前核对；`grep`/`rg` 组合短选项的 pattern 文件纳入读取目标、未知组合保守拒绝；transcript 从尾部有界取得最新用户记录、所有真实 user 记录去重、exec 非零退出码与 `is_error` 冲突按失败处理、`session_meta.id` 核对、每条读取使用自己的有效 cwd；分派 ID 改内容整段失效；`probe` 锁忙放行；未激活 child 不再被禁止嵌套；运行配置即使误列 `doc_paths` 也计入；夹具默认隔离 HOME/XDG/Git 全局配置。
- **安装清单（施工审查后修订）**：`install.py generate` 输出逐条目身份/摘要，`apply`/`uninstall` 只按清单匹配（默认用法不再需要 `--plugin-root`），`probe`→`enforce` 替换上一代条目而非追加，用户修改过的条目保留并报冲突，含空格/单引号/`$`/反引号的 Plugin 根可识别；`--host pi` 额外把 `install-context.json` 写到 `<plugin-root>`（扩展默认读取位置），pi 适配器默认使用生成物记录的绝对解释器路径。
- **r5 施工审查修复**：退役墓碑核对原 epoch/task_identity 且保留首次墓碑（同分派 ID 在新 epoch 复用不再复活旧 child）；重定向按运算符区分 fd 复制与文件打开（`> 1`/`2> 1` 是普通文件目标，`>&1`/`2>&1`/`/dev/null` 不是）；`cd . > file` 同段重定向按执行 cd 前的 cwd 处理；`cp`/`install -t` 共用 operand 解析（`-t` 下所有位置参数都是源），`mv` 的每种目的语法都附加被移走的源；`rg --files` 的位置参数全是根；`session_meta` 身份核验先于“用户指令”和“SKILL 读取”两条 transcript 激活路径，历史 SKILL 读取只保持激活而不重算角色/只读，transcript 中的原始 user 机器上下文（含 `active=false`）统一解析；`apply`/`uninstall` 在任何备份/写入前逐层核验目标与父链并用 `dir_fd` 备份+更新（拒绝时不留备份）；input 的 transcript 取证归一在锁外，锁内只复核记录键/actor/root/task/epoch。CI 的 Hook 离线矩阵项超时从 5 分钟调到 10 分钟（本机 hooks suite 已超过 5 分钟预算）；本版未取得 CI 运行结果，不宣称 CI 已通过。

未验证：四宿主真实 Hook 信任、stdin 结构、输出消费与 Stop 续跑仍需各自 probe；DSH 本机无 CLI，只交付离线夹具与安装文档；pi/Antigravity/DSH 子代理完成结果没有稳定 selector，P3 完成事件不启用；pi 适配器已由 Node 夹具（假 `ExtensionAPI` + 真实 `child_process` argv）验证真实核心的 deny 往返，未做 TS 类型检查；Antigravity/Codex 子 transcript 关联仍需最小脱敏夹具；Windows ACL/PowerShell 未验证。离线全绿不能替代宿主级隔离证明。

## 1.3.2 更新说明（2026-10-27）

按“规则正文做减法、确定性门禁做加法”收紧实现期判据可见性与记账双源；不改判定口径的语义、预算数值与审查轮数，也不新增状态机分支。

- **normal 实现期接入代码质量判据**：`routing.md` normal 第 2 条新增“实现改动（非纯文案与注释）先读 `code-quality.md`”。原链路只有子代理经 `developer-role.md` 拿到该链接，主代理在 normal 亲自写代码时不读判据，质量偏差只能等审查阶段以 P3 暴露，属返工成本。
- **信号① 消歧**：`routing.md` 判定依据补一句“修 Bug 通常改变既有行为，作者新增的回归测试不改变本条判定，但上列豁免仍优先”。这是既有条款（“作者本次新增的测试不算”）的直接推论，不新增分支；此前从未对外可观测的路径仍按既有豁免处理——不改变①的判定要素，只关闭“修 Bug 不改行为因而不命中①”这一种宽松读法。
- **记账去双源**：`orchestration.md` 的 normal→full 继承轮次改为指向 `routing.md#normal-流程`，只保留“不能按单次只读 review 重记零”与剩余轮次约束这两条本模块独有内容；去重后的口径由 `design-gate` 状态机覆盖。
- **新增总量棘轮（`check.sh` §10）**：`SKILL.md` + `references/*.md` 合计不得超过 131723 字节，按 `tr -d '\r'` 计数以避免 CRLF 工作区误报；新增规则必须同时删减等量正文，扩容需在本节说明理由后上调上限。基线含本版新增（约 220 字节），之后只在删除正文后向下重设；`MAX_TOTAL` 与本节数值双写，改动须同步。
- **quick 的计量改为按需**：`SKILL.md` quick 核心的报告项不再要求单行计量，`routing.md` 计量节限定为 normal 与 full（quick 默认不写，用户要求时补）。依据 2026-10-27 四场景回放里 B01 的表现：入口写“单行计量（见 routing 的计量节）”会把 quick 拉去读 `routing.md`，与“普通 quick 只需本页”互斥；而以读取事件为准的宿主（pi 的 `role_metric.py`、Codex 的 `analyze_events.py`）本就不依赖报告里的计量行，quick 不写不损失可测性；无事件层的宿主（本轮 `antigravity`）quick 的 `模块` 记未采集（记录见 [behavior-replay](tests/experiments/behavior-replay/2026-10-27/README.md)）。
- **回归锁**：`design-gate` 文档契约新增一条（`routing.md` 的 `## normal 流程` 段须含 `code-quality.md`），并把原本锁在 `SKILL.md` 的“quick 核心指向计量节”改为“计量降为按需”、给计量节追加适用档位断言，文档契约总数 20 → 21，`tests/README.md` 与 `design-gate/README.md` 的组数同步更正（此前 1.3.0 新增 6 条时未更新）。
- **quick 的停止条件写进入口**：第 4 条从“达到上限停止”改为“失败先取证，三次仍失败即停”（数字进入口，细则仍以 `routing.md#尝试预算` 为准）；`check.sh` §9 把 `AGENTS.md`、`routing.md`、`SKILL.md` 三处上限做同值校验，`DOC_CONTRACT` 锁住该措辞。
- **B01 判据对齐入口自问句**：`b01.py` 允许 quick 读 `SKILL.md` 与 `routing.md`，其余 reference 仍判超载（入口第 15 行的自问句与第 4 条的尝试预算都要求读 routing）；检查名改为 `quick loaded no other references`，`test_cli` 与 `tests/behavior.md` 同步。
- **文档契约可证伪**：断言改为按小节作用域求值（默认截到下一个行首 `## `、围栏内伪标题不计，跨节断言显式 `scope: file`），新增可选 `forbidden` 锁废弃措辞（已用于旧计量句与“适用所有档位”），并加 5 组机制自检：句子搬到邻节、旧新措辞并存、normal 契约移节、围栏内锚点越界都必须判负，文件作用域允许跨节搬动。`b01` 另补两条用例：读 `references/routing.md` 必须通过、读其他 reference 必须判负（`module_access` 的键带 `references/` 前缀）。
- **未采纳项及依据**（不另建文档）：入口为 quick 塞写法基因——入口只剩 9 字节，且会与 `code-quality.md` 的“唯一细则源”构成无门禁兜底的散文级双源；信号①豁免——`swebench` 各臂禁改测试，routing-replay 无法验证“作者先写失败测试”，豁免缺少可执行校准车辆；运行时记账脚本——只读审查与回放均未出现记账失败样本，脚本也无法强制模型调用。

验证：本机 macOS、无 PyYAML（走降级分支）。`check.sh` rc=0，含 §10（`规则文档总字节: 131723 (棘轮上限: 131723)`）；§10 做了三次负向验证——向 `routing.md` 追加 3 字节后报 `Error: 规则文档总量超过棘轮 (131726 > 131723)` 且 rc=1；把 `MAX_TOTAL` 改为 131736 后报 `Error: MAX_TOTAL=131736 未出现在 CHANGELOG` 且 rc=1；把计量节的适用档位改回“适用所有档位”后 `design-gate` 报 `FAIL: routing.md 的计量小节给出字段键序、缺省写法与角色分段 … 缺 ['适用 normal 与 full', 'quick 默认不写']` 且 rc=1；三处恢复后复跑全绿。新增文档契约同样做了负向验证——删除 normal 第 2 条的 `code-quality.md` 链接后 `design-gate` 报 `FAIL: routing normal 流程要求实现改动先读代码质量判据` 且 rc=1；把作用域改回固定 1200 字符窗口后，机制自检报 `FAIL: 计量句移到邻节必须判负 -> 预期判负，实际 通过`，两处恢复后复跑全绿。其余七项：`design-gate` 77/77、`audit-overflow` 16/16、`public-contract-callers` PASS（11 个伪修复均被拒）、`version-gate` 通过（PyYAML 分支未跑）、`test_role_metric` 10 项、`agent_runner` 43 项（114.8s）、`swebench` 56 项（54.6s）。独立审查两轮（同模型不同实例，跨供应商未启用：宿主无可枚举的模型清单）：首轮 2×P2 + 5×P3（信号①新句与既有豁免可两面取证、新义务无回归锁、CHANGELOG 定性与双写、棘轮覆盖面与 CRLF、首版基线未披露），复审 1×P2 + 4×P3（README 组数枚举未同步、CHANGELOG 措辞反向、基线无法机械比对、§2 CRLF 不一致、多重覆盖定性与事实不符）；除「入口按需加载表未反映 normal 义务」一项因 4096 字节硬顶未采纳外，其余均按一行级改动处理后关闭，未开第三轮。

未验证：本版发布时未做真实模型回放。之后另做了一轮四场景回放（`antigravity/gemini-3.8-flash`、单次、宿主为非隔离的 pi 子代理），B67 实施 8/8、B67-review 8/9、B01 10/11（另 1 项无事件流、未采集）、public-contract-callers 8/8（另 2 项未采集或需可委派宿主）；改动后又复跑了一次 B01：11/11 有效（`quick loaded no other references` 仍无事件流、未采集），只改目标文件、档位 quick、末行自查、未写计量行，记录见 [behavior-replay/2026-10-27](tests/experiments/behavior-replay/2026-10-27/README.md)。normal 实现期读判据是否真的减少返工，仍需 B67 式 A/B 回放（有/无该指向）；信号① 新句是否仍会被误读为“修 Bug 必然命中”，只能由档位声明回放观察；quick 计量降为按需后 B01 是否仍读到 `routing.md`（quick 核心第 4 条仍链 `routing.md#尝试预算`），需在下一批同宿主复跑中确认；“超越性”结论仍需 baseline / checklist / easydev 三臂对比，现有 B 场景验收由 easydev 自身规则导出，不能用于竞品对比。

## 1.3.1 更新说明（2026-10-27）

为 1.3.0 的 B67（代码质量判据与 P3 口径）补可复跑的载体：新增 `b67` 与 `b67-review` 两个 `agent_runner` 场景及离线用例，登记行为规约与维护对应关系；不改判据内容、档位判定、预算与状态机。

- **新增 `b67`（实施侧）**：夹具含入口校验、吞掉写盘失败的 `service.save()`、`codegen.py` 生成的 `src/generated/schema.py` 与 `unittest` 基线。验收只看可观察结果——写盘失败必须抛到调用方、入口校验仍拒绝空 `order_id`、正常路径仍落库、生成文件未被手改、改动不出 `src/`+`tests/`+`codegen.py`+`README.md`、测试套件通过，以及**把 `service.py` 换回吞异常原文后测试必须失败**（回归测试真能拦住回退）。
- **新增 `b67-review`（审查侧）**：同一夹具加两个对照样本——`src/report.py` 的四层嵌套与三处重复回退行（代价型），`src/export.py` 的两空格缩进、驼峰循环变量与混用引号（纯风格）。验收要求报出前者且严重度不高于 P2（P2/P3）、不为后者产生任何发现；只读与运行时 Skill 未变同 1.2.x 的审查场景。
- **离线用例**：`test_cli.py` 增 5 个用例（两场景各一通过支路，实施侧三个负例：失败仍被吞、手改生成文件、测试不能失败；审查侧三个负例：只报纯风格、缺发现块、只读违约），替身只执行 `mutations`，不调用真实模型。
- **文档**：`agent_runner/README.md` 补两行场景说明，`tests/README.md` 与 `tests/behavior.md` 登记 B67 的两侧载体，并把 B67 行从“仅文字规约”改为“判拒口径可复跑、真实回放未执行”。

验证：本机 macOS、无 PyYAML。`check.sh` rc=0：行尾空白、两处 frontmatter、四处版本 1.3.1、链接与锚点、§9 两道门禁。其余七项：`agent_runner` 41 项（新增 5 项，127.3s；两项新负例分别验证「失败仍被吞」「测试不能失败」）、`design-gate` 71/71、`audit-overflow` 16/16、`version-gate` 通过（PyYAML 分支未跑）、`test_role_metric` 10 项、`swebench` 56 项（63.1s）、`public-contract-callers` PASS（11 个伪修复均被拒）。两个新场景另做了反向验证：把 `b67` 的「回退后测试必须失败」改成恒真，负例 `test cannot fail` 报 `AssertionError: 0 != 1`；把 `b67-review` 的「纯风格不报」改成恒真，`style only` 负例失败；两处恢复后 5 项全过。

未验证：两个场景的真实宿主回放尚未执行——本机沙箱内 `codex exec` 在初始化阶段即被拒（`failed to initialize in-process app-server client: Operation not permitted`，rc=1），替身只验证判拒口径，不代表真实模型会遵守；探针依赖夹具契约（`save(order_id, total)` 签名与 `DB` 模块常量），日后改契约需同步夹具与探针。B67 已做一轮真实回放（Codex 宿主 `gpt-6.1-sol` high，两侧各一次）：实施侧 8/8、审查侧 9/9 通过，记录见 `tests/experiments/b67-replay/2026-10-27/`；单次、单模型只是线索，不构成回归保护，runner 的 `events.jsonl` 与 `valid_invocation` 未采集。

## 1.3.0 更新说明（2026-10-27）

按“跨任务规则留在全局、编码细则随技能分发”的分层重新划定 `AGENTS.md` 与 `easydev/` 的边界：新增代码质量判据模块并作为唯一细则源，全局 §5 只保留四条底线，补两道防漂移门禁；不改变档位判定、预算数值、状态机与审查轮数。

- **新增 `references/code-quality.md`**：承接代码质量判据（克制复杂度、边界校验、结构与抽象、控制流、类型与异常、同步与异步、依赖方向、术语、注释、生成文件），每条配正例与反例，并声明为唯一细则源；`execution.md`、`review.md`、`developer-role.md`、`reviewer-role.md` 改为链接引用，`execution.md` 里重复抄写的命名、职责与注释细则删除（保留“移动/合并代码保留原错误处理与校验”与“不得削减”清单）。
- **入口接入且未破字节上限**：`SKILL.md` 按需加载表把原本单模块的两行合并（execution 与 verification 合为“复杂验证、性能；实操产品”），并把 code-quality 并入原本单模块的 review 行（“代码质量；review、audit、full 审查”），不新增行、不压缩正文措辞；quick 第 3 条“只在新改动、失败或未决疑点使证据失效时重跑”等判定条件保持原样（quick 不读 execution，判定条件必须留在入口）。入口 4071 → 4087 字节，未动“三自问”、高风险边界枚举与 design-gate 文档契约断言的锚点。
- **全局 §5 压成四条底线**：授权/资金/数据丢失/敏感信息不适用“几乎不会发生”的豁免、不删减必要的错误处理与资源清理、入口只校验一次且不可信结构化输入用 schema 校验库、生成文件不手改；其余判据随技能分发。“外部参考（开源实现借鉴）”从全局迁入 `execution.md`「最小充分实现」作为独立条目。
- **审查口径**：明显违反代码质量判据、且实际增加理解或修改成本的写法可报 P3，须指明位置和代价（`review.md`「过滤噪音与归因」与 `reviewer-role.md` 第 2 条）；`review.md` 把 `AGENTS.md` 当“审查数据”举例的用法保留。
- **新增两道门禁（`check.sh` §9）**：`AGENTS.md` 与 `routing.md`「尝试预算」的修复次数上限必须同值，措辞变动导致提取失败时直接报错；`easydev/` 除 `CHANGELOG.md` 外不得引用“全局规则”。
- **文档契约与行为规约**：`design-gate/verify.py` 的 DOC_CONTRACT 新增 6 条断言（code-quality 为唯一细则源与关键判据、注释与生成文件判据、execution 的链接与外部参考、review 与 reviewer-role 的 P3 口径、developer-role 的链接）；`tests/behavior.md` 新增 B67 文字场景与维护对应关系，注明无离线载体、未做真实回放。
- **腾挪顺序修正**：`README` 的入口字节腾挪顺序把“合并按需加载表里相近的行”提到最前，因为“计量细节移进 routing”在 1.2.x 已用完（入口只剩指向 routing 计量节的一句）。

验证：本机 macOS、无 PyYAML（走降级分支）。`check.sh` rc=0：行尾空白、两处 frontmatter、四处版本 1.3.0、链接与标题锚点（含新增的 `code-quality.md` 与 `#测试硬约束`）、README bash 块 `bash -n`、示例字段契约、规则文件 frontmatter，以及新增 §9 两条（`OK: 修复次数上限一致 -> 3`、`OK: easydev 未引用“全局规则”（CHANGELOG 历史记录除外）`）；入口 `SKILL.md` 4087 字节（上限 4096）。新增门禁做了负向验证：把 `AGENTS.md` 的“累计修复 3 次”改成 4 次 → §9 报 `Error: 修复次数上限不一致：AGENTS.md=4，routing.md=3` 且 rc=1；把 `routing.md` 的“最多三次”改成四次 → 报 `AGENTS.md=3，routing.md=4`；在 `execution.md` 临时写入“见全局规则第 5 节” → 报 `Error: easydev 不得引用“全局规则”`；三处恢复后复跑全绿。新增文档契约同样做了负向验证：把 `review.md` 的“须指明位置和代价”改成“须指明位置”后，design-gate 报 `FAIL: review 给出代码质量 P3 口径 -> easydev/references/review.md 的「过滤噪音与归因」附近缺 [位置和代价]`（rc=1），恢复后 71/71。`scripts/smoke-install.sh` 以 `SMOKE_TMP=$PWD/.smoke-tmp bash scripts/smoke-install.sh` 跑通（rc=0，7 个场景全过，含场景 7 的 easydev 文件集），运行后删除该临时目录。其余七项：version-gate 通过（PyYAML 分支未跑）、design-gate 71/71、audit-overflow 16/16、public-contract-callers PASS（11 个伪修复均被拒）、`test_role_metric` 10 项、agent_runner 36 项（110.1s）、swebench 56 项（63.1s）。

未验证：本轮没有真实模型回放，`code-quality.md` 的判据与新增的 P3 口径只有文档契约与既有离线载体，没有回放样本；B67 仍是文字规约，静态契约只保证引用与口径存在，不证明模型遵守；新一轮全局规则尚未发布到运行时副本，需用户在自己终端执行 `scripts/sync-agent-rules.sh --check/--apply/--check`，发布前宿主仍按旧规则运行。本轮未做独立审查，未提交、未推送。

## 1.2.9 更新说明（2026-10-27）

复评模型组后重写 README 的示例分配：升级阶段开发从 `grok-4.7` 改为 `gemini-4-argon`，审查从 Antigravity 的 `claude-sonnet-5-5` 改为同一 `gpt-6.1-sol` 的 `max` 档；不改档位判定、预算、字段契约或状态机。

- **升级阶段开发换 `gemini-4-argon`**：1.2.8 用 AA Agentic 58.4 与 SWE-bench Pro 74.5 证明 `grok-4.7` 强于阶段 0，但阶段 0 开发是按 DeepSWE 选的（77.0 对 Grok 的 71.0，Grok 反而更低），审查是按 Bug Hunt 选的（high 档 19.3，为可调用模型最低）。Argon 是唯一两条实现指标都不低于阶段 0 的候选：DeepSWE 77.9、SWE-bench Pro 81.5（估算）对 62.7，AA Intelligence 39.5 → 52.6，Bug Hunt high 40.7。
- **升级阶段审查改为档位升级**：1.2.8 的理由是 Sonnet 5.5 的 Bug Hunt 51.3「当前可得最高」，但该行是 Claude Code 的 `max` 档；1.2.9 的宿主（Antigravity）只到 `high`，同档实测 34.3，低于 Sol high 的 36.5，所谓升级拿不到。改为同一 Sol 的 `max`（44.3 对 36.5），代价约 1.8× 成本与 2× 墙上时间，且只在一次修复失败后触发。
- **副作用口径**：示例段补两条判据说明——`gpt-6-astra` 的 max 45.0 与 Sol 的 44.3 之差在重跑噪声内（同配置重跑中位偏移 1.8 分）却要 5× credits 与 5–45 条/5h；`antigravity/claude-opus-5-5`（SWE-bench Pro 89.9 实测最强）额度只够偶尔手动调用，按对话显式指定一次性覆盖，不进 `upgrades`。
- **可比性口径**：说明同一档位才有可比性（同模型 `high` 与 `max` 可差 7–18 分），并补“Codex 侧可到 max，但请求值是否生效要用实际返回值核对”。示例仍是唯一一份、仍不自动加载，仍保留“换宿主必须逐字替换 ID”的说明。

验证：本机 macOS、无 PyYAML（走降级分支）。`check.sh` rc=0：行尾空白、两处 frontmatter、四处版本 1.2.9、链接与标题锚点、README bash 块 `bash -n`、§6 跨供应商升级契约（`OK: easydev/README.md 内联示例符合字段契约与跨供应商升级示例`）、§8 规则 frontmatter。契约本身未改，但做了负向验证：把 `upgrades[1].developer` 临时改回 `opencode-go/grok-4.7`（与阶段 0 同供应商）后，§6 报 `Error: 内联示例 upgrades[1] 未更换任何一个角色的供应商，无法演示跨供应商升级`；恢复后复跑全绿。其余七项：version-gate 通过（PyYAML 分支未跑）、design-gate 65/65、audit-overflow 16/16、public-contract-callers PASS（11 个伪修复均被拒）、`test_role_metric` 10 项、agent_runner 36 项（112.2s）、swebench 56 项（64.2s）。`scripts/smoke-install.sh` 未完成：沙箱禁止在 `/var/folders` 下 `mktemp`，建临时 `HOME` 时即失败（rc=1）。

未验证：本轮没有重跑任何基准，判据全部来自 2026-10-26/27 的公开榜单（Bug Hunt Bench、DeepSWE、SWE-bench Pro、AA Index）与本机可调用清单；没有对 `codex/gpt-6.1-sol` 的 `max` 做生效性探针（避免消耗 Codex 额度），所以“请求 max”是否真被服务端接受仍未确认；本轮未做独立审查。用户级 `~/.config/easydev/models.json` 按用户授权另行写入，内容与本节示例一致（已比对相等），不随仓库分发。

## 1.2.8 更新说明（2026-10-27）

把 README 的模型配置示例从占位符换成一组建模依据可查的具体分配，并同步 `check.sh` 的示例契约与 `models.md` 的引用口径；不改档位判定、预算或状态机。

- **README 示例换成具体分配**：`<provider-a>/<model-a>` 占位形状改为开发 `deepseek-v4.1-flash`、审查与设计 `gpt-6.1-sol`、升级阶段开发 `grok-4.7` 加审查 `claude-sonnet-5-5`，并补判据：本仓库 182 次子代理运行的实测用量与耗时、Bug Hunt Bench / DeepSWE / SWE-bench Pro / AA Agentic 等公开基准、以及路由可用性（`zed/*` 实测 403、`gemini-4-argon` 订阅额度稀缺且 Bug Hunt 40.7 低于 `gpt-6.1-sol` 的 44.3）。示例仍是唯一一份、仍不自动加载，仍保留「换宿主必须替换 ID，照抄会暂停报受阻」的说明。
- **示例契约从「upgrades 再次对调」改成「跨供应商升级」**：`check.sh` §6 不再要求升级阶段把开发与审查对调，改为要求升级阶段至少更换一个角色的供应商，且两级各自的开发与审查都分属不同供应商。实际要约束的是「实现与审查不同供应商」，对调原本只是其中一种写法；旧断言会把更有效的升级组合判失败。
- **`models.md` 引用口径同步**：不再称示例 ID 为占位值，改为「来自维护者宿主，换宿主须逐字替换为本机支持的完整 ID」。

验证：本机 macOS、无 PyYAML（走降级分支）。`check.sh` rc=0：行尾空白、两处 frontmatter、四处版本 1.2.8、链接与标题锚点（含根 `README.md`、`agent-rules/**/*.md`）、README bash 块 `bash -n`、§6 跨供应商升级契约（`OK: easydev/README.md 内联示例符合字段契约与跨供应商升级示例`）、§8 规则 frontmatter。契约改动做了负向验证：临时把示例升级阶段的审查改回 `codex/gpt-6.1-sol`（与阶段 0 同供应商），§6 报 `Error: 内联示例 upgrades[1] 未更换任何一个角色的供应商，无法演示跨供应商升级`；恢复后复跑全绿。其余七项：version-gate 通过（PyYAML 分支未跑）、design-gate 65/65、audit-overflow 16/16、public-contract-callers PASS（11 个伪修复均被拒）、`test_role_metric` 10 项、agent_runner 36 项（111.7s）、swebench 56 项（62.0s）。`scripts/smoke-install.sh` 未运行：沙箱禁止在 `/var/folders` 下 `mktemp`，脚本建临时 `HOME` 时即失败；它读的是 `easythink/README.md`，对 easydev 只断言 SKILL.md 版本与 README 一致（已由 §3 覆盖）。

未验证：示例里的四个模型只在单次调用与公开基准上验过，没有真实任务成绩；本轮未做独立审查。用户级 `~/.config/easydev/models.json` 按用户授权另行写入，内容与本节示例一致（已比对相等），不随仓库分发。

只修一处锚点、补齐交付前的完整检查口径、给入口腾字节并恢复高风险枚举、扩展 `check.sh` 的覆盖面，并补全局规则的分发说明；不改档位判定、预算或状态机。

- **修复 `execution.md` 的锚点**：`references/execution.md` 的「quick 已完成」把「交付前三项微自检」链到
  `../SKILL.md#开始工作`，而该清单实际在「quick 执行核心」一节。锚点改为 `#quick-执行核心`，链接不再落到
  错误章节（`check.sh` §4 只校验锚点存在，看不出这种指错节）。
- **交付前的完整检查三处对齐**：`SKILL.md` 的 quick 第 3 条压成一句「交付前跑完整检查」（不再重复列举与用户决定口径）；
  `references/developer-role.md` 补「交付前跑项目规定的完整检查（lint + 类型 + 测试），耗时过长或需用户决定时
  回报主代理（不直接向用户提问）」；`references/execution.md` 把「交付仍须完成完整检查」改为「最终交付前完成
  一次项目规定的完整检查」，同版本同环境的既有证据（含子代理交接结果）直接复用、不重复跑全量，由主代理交
  用户决定耗时。
- **入口恢复高风险枚举、`check.sh` 扩大覆盖面**：`SKILL.md` 入口恢复 8 类高风险边界枚举（鉴权、支付、敏感
  数据、并发与破坏性数据操作、密码学与随机数、CI/构建/IaC/部署权限、依赖与供应链、生产日志与遥测；紧凑一句，
  详情仍以 `references/routing.md` 的「高风险边界」表为准），quick 第 2 条改为新增功能与 Bug 记红绿（先失败
  后通过），第 4 条去掉「切路径或换实例不重置」，入口 4071 字节（上限 4096）。`scripts/check.sh` §4 覆盖
  `AGENTS.md`、`agent-rules/**/*.md` 与根 `README.md`，并跳过 `file://` 链接（`agent-rules/antigravity.md` 的
  示例不是相对链接）；§5 把根 `README.md` 的 bash 块纳入 `bash -n`。

验证：八项检查全绿（`check.sh` rc=0：四处版本 1.2.7、SKILL 4071 字节（上限 4096，余量 25）、链接与
标题锚点（含本轮新纳入的 `AGENTS.md`、`agent-rules/**/*.md` 与根 `README.md`）、内联示例字段契约；
design-gate 65/65、version-gate 通过、public-contract-callers 通过（11 个伪修复均被拒）、audit-overflow
16/16、`test_role_metric` 10 项、agent_runner 36 项、swebench 56 项）。本机未装 PyYAML，`check.sh` 与
version-gate 均走降级分支，PyYAML 分支未在本机执行。

未验证：本轮只改文案、锚点、门禁覆盖面与版本号，未做独立审查（审查后置）；入口字节减少与措辞压缩不证明 token、
费用或耗时收益；`scripts/smoke-install.sh`（安装目录断链与版本同步）未运行。

## 1.2.6 更新说明（2026-10-26）

只收口示例的分发口径，不改档位判定、预算或状态机。

- **删除 `examples/`，示例只留在 README**：`models.cross-provider.json` 与 `examples/README.md`
  移除，README 的「模型配置」一节成为唯一示例来源；其中仍成立的两点（ID 是占位值、不另放全
  `inherit` 示例的理由、示例不自动加载）并入该节。
- **安装契约不再被绕过**：`references/models.md` 原先把形状示例链到 `../examples/…`，而安装集
  只有 `SKILL.md`、`references/`、`agents/`——已发布安装里这条链接一直是断的（1.2.4 同样如此）。
  现改为指向仓库 README 的模型配置一节，与 `setup.md` 的既有写法一致；安装集不变。
- **`check.sh` §6 改为校验内联示例**：原来要求 `easydev/examples/*.json` 存在且与 README 内联块
  一致；目录删除后改为直接校验 README 的 json 代码块，按 `references/models.md` 的字段节查角色
  字段（只允许 `model`/`reasoning_effort`）、深度枚举、`model` 的供应商与模型名都非空，以及
  `upgrades` 的结构（最多两项、每项含完整 developer 与 reviewer、不含 designer、每阶段至少一项
  与上一阶段不同）。示例是唯一事实源，另按它自己声明的形状断言：基础开发与审查分属不同供应商、
  首个升级阶段把两者对调。门禁不再依赖 `examples/`。

验证：九项检查全绿（`check.sh` rc=0：四处版本 1.2.6、SKILL 字节、链接锚点、内联示例字段契约；
design-gate 65/65、version-gate、audit-overflow、public-contract-callers、`role_metric`、agent_runner
36 项、swebench 55 项）。§6 另做 10 个变异——`/`、`/model`、`provider/`、纯空白段、`upgrades: []`、
基础角色同供应商、升级未对调、`inherit` 作基础、缺 `reasoning_effort`、`upgrades: [null]`——全部按
预期非零退出并指向具体分支。独立审查 3 轮（`codex/gpt-6.1-sol:high`，只读）：首轮 1×P2（§6 只验
`model` 带前缀），修复后定向复审 2×P2（空供应商/空模型名放行、通用 schema 未接替跨供应商对调的
示例语义），再修后第 3 轮零发现；`r=3/5`、`d=0/3`（单文件脚本、无对外契约或持久数据，设计门控未
命中）。

未验证：本地安装目录（`~/.agents/skills/easydev`）的断链靠脚本离线复查，门禁只校验仓库内相对
链接，看不见「仓库有、安装没有」这类差异；`scripts/smoke-install.sh` 在本机被沙箱拒绝
（`mktemp: Operation not permitted`）未运行；审查者只做静态只读复核，未独立执行命令。

## 1.2.5 更新说明（2026-10-26）

只动判定载体与一处报告契约，不改档位判定、预算或状态机。

- **确定发现的机器格式下沉到审查角色与交接**：`reviewer-role.md` 要求每条发现以 `**[P级] 简短标题**` 起头，`routing.md` 的 normal 分派要求把该格式写进交接。依据是同日三次真实 pi 回放：r1/r2 的 reviewer 都在散文里报出真实 P1（Decimal 精度、half-even 舍入），而规范发现块出现 0 次，事件归因解析为空——「有实据的升级」被读成「无证据升级」。normal/full 的定向审查不强制读 `review.md`，格式契约原先只写在那里，reviewer 看不到。
- **b61 判分按 `behavior.md` 的 B61 三支分支化**（`agent_runner/scenarios/b61.py`）：终态 normal 时独立审查 1–2 次、且不得出现结构化 P0/P1（首轮报阻断项必须转 full）；转 full 时要求初始档位声明、升级前已有完成的审查报告、引用结构化 P0/P1/P2、`r≤5`/`d≤3`、不得从 quick 起跳。原先只认最理想一支，「按规升级」与「违规升级」判成同一种失败，也回答不了「normal 那次审查值不值」。
- **任务文本钉死两处规格**：数值域（金额与 `discount` 按 `float`，不要求 `Decimal`/`Fraction` 精度）与折扣方向（`discount` 是减免比例，实付 `1 - discount`）。前者避免无界精度争议让升级链失去收口点；后者避免「减免 vs 按比例收费」两种读法各判一次——r2 即因此报废。
- **归因接受英文路径声明**（`swebench/analyze_events.py`）：`normal path`、`taking the normal route`、`moved to the full route`、`escalated to full` 与中文「分流/判定为」都算声明；裸 `full path`（文件路径语境）与英文否定（`not switching to full`）不误判。依据是 r3 回放：模型写 `normal path` + 「分流 **normal**」，中文正则取不到，判分因此依赖措辞语言。
- **发现只从独立成行的严重度起头解析**（`swebench/analyze_events.py`）：交接或报告里复述模板（"每条以 `**[P0]/[P1]/[P2]/[P3] 简短标题**` 起头"）、以及正文行内的 `[P1]` 提及都不再算发现。依据是 r3 回放：主代理把模板写进交接，归因把它当了一条 P0。
- 文档同步：`tests/README.md`、`agent_runner/README.md`、`behavior.md` 记录新判分口径，并声明三条限制（散文发现不可见、升级证据不做契约语义判定、数值域与方向是 fixture 限制）。

验证：macOS 同一快照，`check.sh` rc=0（四处版本 1.2.5、SKILL 字节、链接锚点、JSON 示例），design-gate 65/65、version-gate、audit-overflow、public-contract-callers、`role_metric`、agent_runner 36 项、swebench 55 项全绿。真实回放三次（pi 宿主，`opencode-go/deepseek-v4.1-flash:high`，普通会话，非子代理）：r1 首轮 P1 转 full 走满 `r=4/5` 后报部分完成；r2 首轮 P1 转 full、`r=2/5` 交付，因方向歧义行为判负；r3 行为首次通过（`ok`），但把首轮 P1 留在 normal 内修完即交付——归因看不到散文发现，判分全绿，属判分盲区，已补 `normal delivery has no blocking finding`。

未验证：`scripts/smoke-install.sh` 在本机被沙箱拒绝（`mktemp: mkdtemp failed … Operation not permitted`），未通过；新检查未在真实回放中触发（本轮发现是散文）；单次运行无方差；「局部/非局部 P2」与「必关项是否已关闭」仍是语义判断，未自动化。

## 1.2.4 更新说明（2026-10-26）

只做一处改动：让自判 quick 的模型在动手前看得见第二视角信号，并按 `easydev/README.md` 已定的腾挪顺序给入口腾出空间。不改判定条件、预算、状态机。

- 入口增加一句自问（`SKILL.md` 的「入口与任务分流」）：是否 ≥2 独立调用方依赖、是否改共享契约且有未改使用方、是否没有改前已有检查能拦住回归；任一成立先读 `routing.md` 再定档。依据是 1.2.3 的真实 pi 运行：实现实例自报 quick 且只读了 `developer-role.md`——信号定义写在 `routing.md`，而 quick 按规则只需本页，判据对它不可见。
- 计量单行模板与缺省写法（未读记 `无`、未进 full 记 0、未知写原因）从入口移进 `routing.md` 的计量节，入口只留「单行计量（见 routing 的计量节）」。这是 README 腾挪顺序的第一步；不压缩其他措辞、不放宽 4096 上限。入口 4044 → 4091 字节。
- 文档契约跟随唯一事实源：`SKILL.md` 的断言改为指向 routing 计量节，字段键序与缺省写法的断言移到该节（新增锁定 `审查=<N>；d=<x>/3；r=<y>/5` 键序），组数仍为 14；`orchestration.md` 的旧指向一并改为 routing 计量节。
- 同一待交付快照另含上一轮已确认的模型配置示例修正（`models.md` 与 `examples/` 的 ID 改占位值），不改变运行规则。
- 维护工具：`role_metric.py` 在子代理只用无路径参数的工具（如外部委派）时给一行提示，避免把「事件层看不见」读成「没读」；`tests/README.md` 补两条未覆盖说明。不改变运行规则。

验证：同一快照（HEAD `dc35d05` 加未提交改动），macOS：`check.sh` rc=0（`SKILL.md` 4091 字节、四处版本 1.2.4、链接与锚点、JSON 示例）、design-gate 65/65、version-gate 通过、audit-overflow 16/16、public-contract-callers PASS、`role_metric` 10 项。独立审查两轮（`magpie/codex/gpt-6.1-sol:high`，只读）：首轮报 3×P2（入口「现成检查」缺「改动前已存在」限定、`orchestration.md` 仍把入口当计量来源、本段未交代同一快照的示例占位值修正）；定向复审确认前两项与断言强度已关闭，另指本段引了旧自问句与旧字节数，已按实际值同步（纯报告文本，未再开第三轮）。

未验证：判据是否真的让模型改走 normal——需要能派生子代理的 pi 实例重跑 b61，本轮未执行；`role_metric --path` 只能证明读/未读，不证明判据被遵守。

## 1.2.3 更新说明（2026-10-26）

运行时规则文本未变；本版只补齐 1.2.2 留下的三个「缺真实载体」缺口里的可执行部分，不新增流程、预算或阈值。

- **按角色计量**（`tests/experiments/role_metric.py`）：从 pi 会话目录按实例归因——主代理读 `<session>/*.jsonl` 的 `toolCall.arguments`，子代理读 `subagent-artifacts/*_meta.json`（实际模型、usage、耗时）与 `*_transcript.jsonl` 的 `tool_start.argsPayload`。`--path` 过滤直接回答「哪个角色读了修复点的直接调用方」，`--json` 供批量收集。它同时能核对交付报告里的 `计量：模块=main:…；dev:…；reviewer:…` 与真实读取是否一致。
- **normal 微闭环观测场景**（`agent_runner` 的 `b61`）：夹具里 `total` 被 `invoice`、`receipt` 两个调用方依赖且调用方没有测试（命中信号①②），断言路由必须是 normal、独立审查 1–2 次、必须带回报告、不得宣称 full 闭环、只改 `src/cart.py`；行为验收覆盖折扣四舍五入、非法折扣与调用方展示格式。复审是否读到直接调用方只写进 detail——事件层无法把文件读取稳定归属到 reviewer 线程，这一点在 `tests/README.md` 的未覆盖清单里同样声明。
- **档位对照臂**（`swebench/run.py`）：新增 `easydev-quick` / `easydev-normal` / `easydev-full`，prompt 明写「用户已指定档位，不要改用 auto 重新分流」；默认三臂与 auto prompt 保持不变，未知臂在读取实验资产前即被拒绝。`tests/experiments/routing-replay/README.md` 补上对应命令：先用 quick × normal 两臂回答「normal 值不值」，需要门控数据再加 full。
- 替身新增 `reviewers` 列表（模拟初审 + 定向复审两轮），`reviewer` 单数形式保持兼容；`tests/README.md` 的运行清单增至七项，CI 矩阵增至九项，`role_metric` 单列一项。
- 版本 1.2.3（根 README、`easydev/README.md`、`SKILL.md` metadata、本文件）。

验证：check.sh rc=0（`SKILL.md` 4044 字节、四处版本 1.2.3；行尾空白扫描只覆盖已跟踪文件，本次新文件在 `git add` 前由手工 grep 核对）、design-gate 65/65、version-gate / audit-overflow / public-contract-callers / smoke-install rc=0、role_metric 8 项、agent_runner 33 项（含 4 个 b61 用例）、swebench 55 项（含新增 6 个档位臂用例），`git diff --check` 通过。

独立审查第 1 轮（只读）报 2×P1 + 7×P2 + 10×P3，已按最小改动逐条处理：P1 为 `role_metric` 把不同会话的子代理产物混算（改为按主会话 id 过滤、新增 `--session`、表头打印所选会话）、`b61` 用裸子串判「宣称 full 闭环」（改为带否定窗口的匹配，`不称 full 闭环` 不再误判）；P2 为四舍五入缺区分性断言（补 `total([(1.0,1)], 0.333) == 0.67`）、两轮审查只回一份报告仍通过（比 `reviewer_completed_instances`）、缺「未离开 normal」检查（加 `upgraded_to_full`）、b61 文档把信号①写成成立（改为「②成立、①可主张」）、复放命令漏 `--tasks` 且并发与「顺序固定」矛盾、`tests/behavior.md` 未同步 B61 载体、本段缺审查状态。P3 中与正确性相关的（`--path` 绝对/相对写法、非对象 meta、交接 prompt 不计入主代理读取、manifest 臂字段、目录树顺序、替身文档）一并修正。第 2 轮定向复审确认上述各项全部关闭，另报 2×P2 + 7×P3：`route is normal` 只挡 `full` 升级、漏掉 `full-local`/`full-staged` 终态；`claims_full_closed_loop` 的否定窗口过宽，会把「无需再确认，full 闭环完成」判成合规。两条已改为「终态必须是 normal 或未声明」与「否定词 + 宣称动词紧邻」，并各补一条应失败用例；P3 同步了 `--session` 接受裸文件名、`--path` 子串匹配的限制写进未覆盖清单、实验目录说明与复放聚合顺序。

未验证：`b61`、档位臂与 `role_metric --path` 都需要真实宿主才能产生有意义的观测值——离线载体只证明「给定这些事件时判定与归因正确」，不证明模型会走 normal、会复审调用方，也不证明被强制的档位臂真的照做。SWE-bench 的三档复放仍未执行（缺 Lite 元数据、源码 tar 与 venv）。

## 1.2.2 更新说明（2026-10-26）

按 1.2.1 的第二轮评审意见做六处校准。本轮同时改了 1 条可执行断言、信号①的判定文本并新增协议文档，超出本版新增的「纯措辞补丁」捷径范围；只跑一轮 normal 独立审查加一次定向复审，属授权与成本下的例外，不作为后续措辞补丁的分类先例。

- 计量字段按角色分段：`模块=main:…；dev:…；reviewer:…`，写实际读取的 references 文件名（区别于门控的「模块」=具名包/服务），未参与或未读的角色写 `无`；子代理自己加载的也计入；宿主能提供读取事件时以读取事件为准，模型自述只作兜底。`SKILL.md` 只留「按角色分段」；格式与分段规则只写在 `routing.md` 新增的「计量」小节（适用全档位——原先挂在 normal 流程下，容易被读成只管 normal，入口腾挪时也无处可找），`orchestration.md` 指向该节、不复制。原口径只记主代理，normal/full 的真实读取量被系统性低估。
- 信号①的豁免补可核对前提：仅新增此前不存在的行为或纯文案/注释改动不命中本条，但期望值仍须追溯到需求或独立契约并写出位置，只写「按实现行为」的按命中本条处理——新增行为恰恰是作者测试最容易与实现共享误解的地方。
- 入口超限的腾挪顺序写进 `easydev/README.md`：先移计量与报告格式细节进 routing，再把入口的高风险边界枚举压成一句并指向 routing，最后才考虑把按需加载表移进 routing；不放宽上限，不临时压缩措辞。当前入口 4044 字节。
- 维护成本约定（同段）：只改措辞、计数或错别字的补丁最多跑一轮 normal 独立审查；这一轮报 P0/P1、或改动实际超出措辞与计数范围时，按 routing 的转 full 规则执行。依据是 1.2.1 的 `r=1`–`r=4`：后三轮报的基本是 P3 文案、计数与错别字。
- 新增 `tests/experiments/routing-replay/README.md` 复放协议：每批 3 个真实任务 × quick/normal/full 各一次（9 次），任务须有执行者不可见的独立验收（口径引 `review-ablation` 的任务与预算）；记录 9 个字段，核心指标是「quick 档没有任何机制能发现的缺陷」；判读规则预先约定——信号②需在**两个及以上命中信号②的任务**上零确定发现、且缺陷在 quick 下同样未暴露，并跨批同向，才考虑收紧；分流允许 quick 的任务若漏掉后续才发现的 P1/P2，即构成不得放宽信号①的否证；门控不在评估范围内（已有 design-gate 载体）；单批 3×3 只能否证。只定义口径，不含结果与可执行检查，不进 CI。
- 文档契约 13 → 14 组（新增「计量按角色分段并以读取事件为准」；组按枚举项计，另有 1 条「门控仍为五项」断言）；版本 1.2.2（三处版本来源与本文件）。

验证：同一快照（HEAD `9ea10ad` 加未提交改动），macOS：`check.sh` rc=0（行尾空白、`SKILL.md` 4044 字节、四处版本 1.2.2、链接与锚点、README bash 块、JSON 示例）、`design-gate` 65/65、`version-gate`/`audit-overflow`/`public-contract-callers`/`smoke-install` rc=0、`agent_runner` 29 OK、`swebench` 49 OK（日志 `/tmp/easydev-verify.1.2.2` 与 `-fix`）。独立审查按 normal 上限跑两轮（只读）：首轮报 6×P2 + 8×P3（维护约定缺转 full 兜底、本轮自评越界、信号①前提不可核对、复放协议的任务档位/判读分母/暴露口径）；定向复审确认 6 项中 5 项关闭，余项为 CHANGELOG 定性与 4 项 P3（协议规则恒真、记录基准取错、术语双义、摘要漂移），均按一行级改动处理后不再开第三轮。未验证：PyYAML 分支、Linux runner、角色分段的真实读取事件来源、`routing-replay` 协议本身（需真实宿主，未执行）；`routing-replay/README.md` 当前未被 Git 索引，`check.sh` 的行尾空白扫描要等 `git add` 后才覆盖该文件（本次已手工核对）。

## 1.2.1 更新说明（2026-10-26）

对 1.2.0 新增规则带出的四处规则缺陷做最小修正，只改文本与断言，不新增流程或账目。

- 定向复审范围补直接调用方：`reviewer-role.md` 的最小规程改为「另核对该修复所在位置（函数、常量或配置项的读取方）的直接调用方与相关测试」，不扩查其余路径，但调用方暴露疑似 P0/P1 时按第 3 条追踪调用链并转 full。原范围会把局部修复带进调用方的新缺陷留成盲区。
- 信号①加可判定的「现成」口径：改动前已存在且仍有效的检查，作者本次新增的测试不算；同段尾句「检查完备」改为「改动前已有能拦截回归的检查」，堵住改完补测自证 quick 的旁路；并写明仅新增此前不存在的行为、纯展示文案与注释改动不命中本条，使 B01/B18 的 quick 判定不被误读。该口径使补测不再充当第二视角，normal 占比可能上升（见 1.2.2 的复放协议）。
- 门控第 5 项与 `routing.md` 对齐：判定后果（只改注释、展示文案、错误提示或不含敏感字段的日志措辞且未改变该行所列影响，不因此进入 full；已在 full 仍按 full 施工与独立审查）只在权威的 `routing.md` 高风险边界表下写一次，`design.md` 括注只链接；`design.md`「不启用」段不再把该类任务列为进入 full 的成因。B64 第三组、B61 证据要求同步。
- 计量单行字段改为自描述格式（`模块`/`审查`/`d`/`r`，未读记 `无`，未进 full 记 0），不再用「规则读取数」把 4 KB 入口与 16 KB 编排都记为 1；角色分段见 1.2.2。
- 未采纳：入口上限放宽到 5 KB。推迟：收紧信号②、用真实任务统计档位分布——两者缺真实宿主数据，按推测改阈值会重演 1.0.0 门控剪枝被推翻的过程，数据方案见 1.2.2。
- 版本 1.2.1；文档契约 9 → 13 组。

验证：同一快照下 `check.sh` rc=0、`design-gate` 64/64、其余六项检查 rc=0 或 OK（日志 `/tmp/easydev-verify.1.2.1-final3`）。独立审查 `r=1`–`r=4`：r=1 报 2×P2+6×P3、r=2 报 1×P2+4×P3、r=3 与 r=4 各报 4×P3，全部关闭。未验证：PyYAML 分支、Linux runner、真实宿主回放（「复审扩到直接调用方」与计量格式当时只有文本与契约证据）。

## 1.2.0 更新说明（2026-10-26）

- normal 增加局部修复微闭环：首轮仅报局部 P2（判定条件见 `references/routing.md`「normal 流程」的「局部 P2 判定」四条判据）时，允许一次局部修复并由 reviewer 定向复审，normal 内独立审查至多 2 次；无法确证局部性一律按非局部处理。P0/P1、非局部 P2 与验收缺口仍在首轮后立即转 full。
- normal 转 full 按实际已完成的独立审查记轮：首轮后转 full 记 `r=1/5`（余四轮），局部 P2 修复后复审仍未关闭或现新缺陷转 full 记 `r=2/5`（余三轮），两者均满足为终审与复审预留两轮；修复尝试与已用审查完整继承，不重置。
- 入口把「需要第二视角」改为可观察的第二视角信号（①没有现成检查能拦截回归；②被两个及以上独立调用方或消费方依赖；③改动共享契约且存在本次未改动的使用方；④用户明确要求独立审查），并完整内联七类高风险边界（补齐破坏性数据操作、生产日志与遥测、CI/构建/IaC），改为「涉及一律读 routing」；补 normal 为实现路径而非聊天长度档的消歧句。入口为 3980 字节（上限 4096，余量 116）。
- 设计门控第 5 项加可观察限定：触及高风险边界但改动不改变该边界的行为、决策或数据流（纯注释、展示文案、错误提示或不含敏感字段的日志措辞）时不启用，仍按 full 执行施工审查；「不启用」段落同步，并补敏感字段记录、错误码/状态码/异常类型变化、非只读副作用等反例。1.0.0 条目中「门控第 5 项不设复杂度剪枝」的取舍由本版按用户评审意见替换。
- 「同一问题最多三次修复尝试」收敛为 `references/routing.md#尝试预算` 单一事实源，`SKILL.md` 与 `README.md` 改为链接引用。
- 交付报告统一单行计量字段：`SKILL.md` 的 quick 核心给出 `计量：` 字面前缀与字段顺序（规则读取数、独立审查数、`d`、`r`），未知写未知原因；本文件不再另定分隔符或数值模板，quick 无需读 references 即可获知字段要求。字段用于事后静态对比，不承诺 token、耗时或费用收益。
- 版本升至 1.2.0（根 README 版本表、`easydev/README.md`、`SKILL.md` metadata、本文件）。
- `tests/cases/design-gate` 同步状态机：解耦 `_is_local_p2` 与 `_is_immediate_full`，`finish()` 覆盖「首轮无缺陷」与「局部 P2 复审通过」两种完成分支，第三次审查直接抛错；门控 fixture 增加「高风险但未改变行为」一组，文档契约增加尝试预算链接、第二视角信号与局部 P2 判定条件三项断言。

验证：本机 macOS arm64、Python 3.9.6（无 PyYAML），工作区为 HEAD `c4fa2ae` 加本次未提交改动构成的同一快照，先跑两项快速门禁，其余五项同批并发、各自独立日志（`/tmp/easydev-verify.1.2.0-final`）。评审后修复四项时重跑受影响四项，日志为 `/tmp/easydev-verify.1.2.0-fix`；`public-contract-callers`（自带隔离快照）、agent_runner 与 swebench（断言目标是模块访问事件与补丁结果，不比对被改的说明性文字）未被本轮改动触及，沿用上轮结果。`bash scripts/check.sh` rc=0（行尾空白与冲突标记、`SKILL.md` 3980 字节、四处版本 1.2.0、相对链接与锚点、README bash 块、JSON 示例）；`design-gate/run.sh` rc=0（60/60，含新增局部 P2 修复、双审查上限、`r=2` 转 full、门控第 5 项限定与三项文档契约）；`version-gate/run.sh` rc=0；`audit-overflow/run.sh` rc=0（16/16）；`public-contract-callers/run.sh` rc=0；agent_runner 29 项 OK；swebench 49 项 OK。离线载体只验证状态机与规则判定，不证明真实模型会自发执行局部修复与定向复审；该遵守度仍需真实宿主行为回放。未验证：PyYAML 分支本机未安装故跳过（CI 覆盖），Linux runner 未跑，B61 的 normal 微闭环无真实回放记录。

## 1.1.0 更新说明（2026-10-26）

- 入口压缩为不超过 4 KiB 的路由与 quick 核心，增加维护门禁；测试细则与编排下沉，删除聊天表达三档。
- 新增 normal 一次独立定向审查、短开发/审查角色文件；必关发现转 full，已有预算持续累计。
- 新增有证据的流程降级；补充高风险触发条件、P0/P1 独立关闭依据、能力不足时的自查与审查包。不同实例继承上下文须披露隔离限制。
- 模型配置支持项目级来源，对话指定文件优先，仅在实际分派或配置请求时读取；示例以 inherit 起步，不固化模型版本。
- audit 增加分片覆盖与跨片接口汇总，正文 20 条上限不截断完整发现记录；并行说明按工作量触发。
- 事件归因支持 normal 及其升降级，并区分真正的 reviewer 交接与开发者提及、否定或禁读；新增 normal 状态转换与 audit 23 条溢出的确定性载体。CI 将八项独立检查改为 fail-fast=false 的矩阵任务。离线检查不能证明真实模型质量或性能收益。

## 1.0.0 更新说明（2026-10-24）

- 新增 `references/design.md`：`full` 的独立设计环节。门控启用（跨模块、公共契约或持久数据、多阶段、多方案、高风险边界），未命中沿用原流程并记录不启用理由；设计须落盘为任务目录 `design.md` 并冻结送审；`d/3` 独立预算，未收敛不进施工；施工中发现契约不可行时升版（`v(N+1)`）重走设计审查。
- 新增 `designer` 角色（`references/models.md`、`references/setup.md`、`SKILL.md`、`README.md`、`examples/models.cross-provider.json`）：默认 `inherit` 时由主代理执行设计；配置了具体模型但不可用时暂停设计分派并报受阻，不回落主代理、不用继承模型替代；`upgrades` 不含 `designer`，升级只换开发与审查。
- 预算改为两本账：设计审查 `d/3`、施工阶段正式审核 `r/5`（原 `r/4`），互不占用，合计上限 8 轮；`references/orchestration.md`、`candidates.md`、`review.md`、`design.md`、`SKILL.md` 与 `tests/behavior.md` 口径同步。
- 设计审查硬约束：每条发现须指出「按此设计，在什么输入、状态或执行顺序下会产生什么错误」，指不出可推导失败路径的只能记为建议或待验证风险，不得定级 P0–P3（`references/review.md`、`references/design.md`）。
- 实例隔离：设计者、实现者、审查者两两不同实例；`designer` 为 `inherit` 时主代理可执行设计，但施工必须委派独立开发者，报告标注「设计者=主代理」。
- `tests/behavior.md` 新增 B60（独立设计环节）文字规约与维护对应关系，B47/B51 的轮次口径改为 `d/3` 与 `r/5`；B60 当前无执行载体。
- **深度审计后修复（5×P1 / 7×P2 / 4×P3）**：
  - P1：`design.md` 补无委派能力兜底（不再与 `SKILL.md` 的“无子代理时完成实现与自查”相互死锁），并把“模型不可用”与“宿主无委派能力”两种成因分开；`orchestration.md` 的 review/audit 转修复按问题归属记 `d=1` 或 `r=1`（不再硬编码 `r=1`）；施工期设计回退的轮次核对与现场保留规则；作者争议覆盖设计者并补 `d=3/3` 末轮归宿；`candidates.md` 与 `models.md` 的候选作者改为取 `designer`。
  - P2：`baseline-safety.md` 收录 `design.md` 与 `design.vN.md` 并声明不得按 `spike/` 清理；候选评审按 `full` 与纯 `design` 双轨记账（纯设计路径不再引用未初始化的 `d/3`）；补前序 `design`/`spike` 转 `full` 的承接复用规则；统一主代理兼任写入者的触发条件（改由“主代理是否承担设计者”决定）；`candidates.md` 记录模板删除 `或 r/5` 残留；施工前停止条件补“范围内 P2”。
  - P3：`v2` 硬编码改为 `v(N+1)` 并要求被替换版本可回查；B60 对照四改为可观测的诱导指令与具体证据项；B27 标明仅适用于未启用设计环节的局部任务；报告字段补冻结版本与设计者标注。
  - 不采纳 1 项：门控第 5 项对局部高风险修复不设复杂度剪枝。该项是用户第一轮决策逐项确认的门控清单之一，前一版审查也要求“命中任一项即启用”，化简会重开该结论；保留为明确取舍，改由用户单独决定。
  - 待验证 1 项：老版外部 `models.json` 缺 `designer` 键的兼容性，已在 `models.md` 明确“省略即合法缺省、不得按缺字段判无效”。

验证：本机 macOS、Python 3.9（无 PyYAML），工作区为 HEAD `2e59900` 加本次未提交改动构成的同一快照，四项检查同批并发、各自独立日志与自身起止时间。`check.sh`（1792848090.6701–1792848091.2115，rc=0）：行尾空白、`SKILL.md` frontmatter、版本一致性 1.0.0（根 README / `easydev/README.md` / `SKILL.md` / `CHANGELOG.md`）、链接与跳转锚点（含新增 `design.md` 的六个被引锚点：启用门控、设计审查、预算、实例隔离、设计产物、收敛与施工闸）、README bash 代码块语法、`examples/models.cross-provider.json` 与 README 内联示例一致均通过。`version-gate/run.sh`（1792848090.6703–1792848091.2474，rc=0）；`public-contract-callers/run.sh`（1792848090.6705–1792848091.9663，rc=0）。

`smoke-install.sh` 首次同批执行时因 `SMOKE_TMP` 落在仓库内，快照 tar 把自身递归打进 `$SMOKE_TMP/src`，生成约 1000 层深的目录树并超时。该失败来自调用方参数错误（脚本未改动，“`SMOKE_TMP` 需指向仓库外一次性目录”本就是脚本与本文前条的约定），不归为代码失败、不耗修复预算；残留目录在仓库外用相对路径增量删除（35342 项，38.5s），仓库回到 13 改 1 新增的干净状态。改用仓库外 `SMOKE_TMP` 重跑 `smoke-install.sh` rc=0，7 个场景全通过，含新增 `references/design.md` 后源与目标的 references 数量一致断言、easydev 文件集不装维护文件、skills 根目录只含两个技能。

独立审查（`r/5`，占用 2 轮，均为只读静态）：第一轮由独立实例审出 1 项 P1（`r/4` 旧口径仍在处置矩阵末列、`easydev/README.md` 与 B09/B10/B25/B27，会让代理在 `r=4/5` 误当末轮停修）与 4 项 P2（`design.md` 不启用清单与门控对同一类 full 任务结论相反且允许退回 quick 核心；`easydev/README.md` 的 `designer` 与内置默认值陈述未同步；`d/3` 内候选与设计审查无配额规则且残留无定义术语“前置设计审查轮次”；`designer` 不可用时任务整体状态未定义），均已关闭。第二轮定向复审确认 5 项全关闭、无 P0/P1/P2，另提 4 项 P3（“full 核心”为无定义术语、`d/3` 预留句二义、B60 对照一未写进入 `full` 的理由、本段锚点计数不准），已按复审建议随手修正，不再开新一轮。第一轮续跑因宿主无法恢复该子代理上下文，第二轮改用新实例。

深度审计（两轮审计之后的独立环节）：用 `gemini-3.8-flash`（Antigravity，thinkingLevel=high，已从 session.jsonl 核实实际模型与深度）按两个互不重叠的维度同批发起——A 角色/预算/停止条件语义一致性，B 门控可判定性/路径交互/失效模式。合并去重后得到 5×P1、7×P2、4×P3、1 项待验证、1 项排除，全部经主代理逐条取证核实。P1 集中于跨文件语义与状态机死锁（无能力宿主死锁、转授权记账归属、施工期设计回退未定义、作者争议未覆盖设计者、候选作者旁路 `designer`），均属 `check.sh` 天然看不见的一类；前一版两轮审查因范围收窄未暴露。审计本身作为修复设计与修复前证据，按 `orchestration.md` 的转授权规则不占正式轮次。

修复后按 `r/5` 走两轮：`r=1` 独立审查确认 12 项关闭、4 项未闭，并新增 3×P2 + 1×P3（`r=3/5` 剩余两轮被误判为不足、施工期第三轮设计审查通过仍被无条件停止、候选第二轮复核仍无条件记 `d/3`、发布说明残留固定 `v2`）；修正后 `r=2` 定向复核确认 4 项全部关闭、未发现新增确定缺陷，判定本批修复可标记完成。两轮实际模型为 `codex/gpt-6.1-sol:high`，与外部配置的 `reviewer` 一致，已从子代理 meta 核对。

第三轮深度审计（三个新维度：端到端状态机、跨文件引用网、可误读面；`opencode-go/deepseek-v4.1-flash:max`）又得 2×P1、13×P2、8×P3，无排除项。两个 P1 同根：门控第 3 项「需要交接」在 `full` 必委派 reviewer 的前提下恒真，使「不启用」分支不可达（等于退化为全量关卡，与 `planning.md`「不加批准关卡」冲突）；以及 `designer` 为独立实例时两个分句互相授权，可静默绕过非 `inherit` 的 `developer` 配置。修复把门控五项全部改写为可观察条件（模块=具名包/服务、层=接口/协议/数据归属边界、多阶段=≥2 实施实例接力或 ≥2 独立验收阶段、多方案=候选两条启用条件同时满足），并补齐：升级进入（quick/debug/review 已开工）的施工闸入口；受阻轮不视为缺陷已关闭；重审前置对「实现者发现」与「施工审核发现」共用；账本模板的门控判定与冻结时间字段；`design.md` 文件内版本标识与仅允许不改契约的修补；硬约束降级「建议」的落地通道；`examples/README.md` 默认值补 `designer`；计划另存 `plan.md`；阶段增量审查合批上限；临时限流归环境故障；用户禁止委派归入无能力兜底并明确该模式不记 `d`/`r`。待验证 1 项（候选 2 轮 + 设计审查 1 轮的配额在多方案任务上的实际可行性）登记未修。

该批修复后按 `r/5` 走两轮：`r=1` 确认 20 项关闭、3 项未闭（实例隔离细则未附配置条件仍可绕过非 `inherit` 的 `developer`；`designer=inherit` 时要求主代理产出全部候选与「候选作者互相隔离」无法同时满足；用户显式要求候选对比时 full 未启用设计环节却仍被要求记 `d/3`）；修正后 `r=2` 确认 3 项全关、未发现新增确定缺陷，判定本批完成。两轮实际模型 `codex/gpt-6.1-sol:high`，与外部配置的 `reviewer` 一致。

遗留验证项的处理：

- **B60 新增可执行载体** `tests/cases/design-gate/`（`gate_model.py` + `verify.py`，45 项全通过，已入 CI）。它把 `design.md` 的确定性部分变成可执行断面：门控五项 12 组 fixture（含「只交给独立审查者不构成多阶段」的可达成性回归）、`d/3` 与 `r/5` 记账与上限、受阻轮不算关闭、剩余 `r` 不足两轮禁止单开设计重审、阶段增量审查最多 3 轮、施工期 `d` 用尽停止。
- **宿主能力作为外部边界用替身覆盖**（8 组）：`inherit` ± 委派能力、独立 `designer` × `developer` 是否 `inherit` × 是否升级阶段、`designer` 模型不可用、用户禁止委派、宿主强制继承上下文；另有两组受阻处置（默认停闸 vs 用户显式豁免仍不得声称闭环）。被测对象是「给定这些输入时规则要求做什么」，**不**证明模型会照做。
- **文档契约纳入回归**（6 组）：账本模板字段、报告段字段、候选第 2 轮留给设计审查、重写不计轮、按需加载表链接、门控仍为五项。把审计里靠人眼发现的字段缺失与口径漂移变成可执行断言。
- **载体当场咬出两条文档歧义并已修正**：一是「升版重审消耗 `d` 轮」会被读成重写与重审各计一轮（一次升版吃两轮），现明确「重写本身不计轮，消耗 `d` 的是重写后的那次设计审查」；二是候选两轮 + 设计审查无余量会给出一份看起来正常、实际停闸的分配，现明确「设计审查尚未进行时候选最多用 1 轮，第 2 轮须用户 opt-in 并接受无升版余量」。
- **“候选 2 轮 + 设计审查 1 轮是否让多方案任务几乎必然停闸”已由枚举回答**：默认分配下（候选 1 轮）六种组合全部闭环完成；只有用户 opt-in 用满 2 轮且设计审查报缺陷才停止，且为显式停止而非静默失败。该问号关闭。
- **真实宿主回放已做一次**（`designer=inherit`、`reviewer=codex/gpt-6.1-sol:high`）：门控命中 3 项 → `design.md` v1 + `ledger.md` 模板字段全部可填 → 真实分派的设计审查给出 1 项带可推导失败路径的 P1 与 3 项明确不定级的建议 → 施工闸报「仍有 1 项未关闭，不进入施工」→ v2 重写不计轮、重审 `d=2`、收敛后 `d=2`、`r=2` 闭环完成，与载体预测一致。未覆盖：受阻轮的产生条件（需宿主反复失败）、真实无委派宿主上的兜底实跑、实例隔离的实现、真实数据库与迁移行为；模型是否遵守规则只能靠真实回放，替身不能证明。

未验证：B60 为文字规约、无执行载体；PyYAML 分支未安装故跳过（留待 CI）；Linux runner 未跑；未在真实宿主上回放设计环节的分派、暂停与 `d/3`、`r/5` 记账。

## 0.15.0 更新说明（2026-10-24）

- 加固测试并行约束，关闭四类绕过（`SKILL.md` 测试硬约束）：一是自述“宿主/runner 不支持并发”或“上限为 1”即可串行的能力自述；二是把串行包装（`;`、`for`/`while` 循环、`xargs -P1`、逐项等待脚本）当作同批提交的伪批次；三是由单一包装器统一打印起止时间伪造重叠；四是把互不相关的检查并入一个“共享状态冲突组”。现要求先写本批清单与具体上限再同批发起，重叠以各检查自身的进程（PID、独立起止时间）或工具事件为证（总墙钟明显短于各项耗时之和只作重叠旁证，长于总和不能单独证明串行），能力限制须附工具说明、配置/用户额度或带硬超时的最小探测（自行选择的 `-n 1` 与核数不算证据），冲突组须最小且先尝试廉价隔离。
- `references/orchestration.md` 新增「并行分派」：只读调查、按模块或维度拆分的审查、候选交叉评估、交给子代理的独立检查须同批发起；共享快照、仓库或模型不构成串行理由，单一写入者只约束写入；串行仅限写冲突、真实依赖、具体实例额度与宿主确实不支持并发，并发以子代理自身起止事件为证，批次清单写入交接或账本。
- `references/execution.md` 的验证调度同步：发起前写批次清单，声明能力限制前先做最小探测，串行只保留最小冲突组，证据要求补各检查自身的重叠凭据。
- `tests/behavior.md` 新增文字场景 B58（子代理独立分派并行）与 B59（并行绕过的识别），并把分派规则纳入维护对应关系；两项暂无执行载体，未执行时不宣称已受回归保护。
- `tests/README.md` 与 `easydev/README.md` 同步：维护检查须同批并发，串行包装、未探测的能力自述与伪冲突组不算并行。

验证：本机 macOS、10 核，检查在冻结快照上同批并发、分别保存日志（`/tmp/easydev-verify.hjWHGv`，各检查自身区间与 PID 记在 `.meta`）。第一批六项（`check.sh`、`public-contract-callers/run.sh`、`version-gate/run.sh`、agent_runner、SWE-bench、`smoke-install.sh`）最早开始到最晚结束 81.8s，各检查自身区间之和 146.4s；agent_runner 29 项 OK（81.8s）、SWE-bench 48 项 OK（62.8s）、public-contract-callers 与 version-gate 退出 0。该批 PID 字段全为 18730（bash 3.2 下 `$$` 在子 shell 中不独立），只能作区间旁证；`check.sh` 退出 1 属预期中间态（根 README 与 `CHANGELOG.md` 尚未同步 0.15.0），`smoke-install.sh` 因工具沙箱拒绝 TMPDIR 内 `mktemp -d` 失败，属环境故障，改 `SMOKE_TMP` 指向可用临时目录后通过。第二批把 `check.sh`、`version-gate`、`smoke-install`（`SMOKE_TMP`）同批并发重跑，三项各由独立进程记录起止（PID 22144/22145/22146，区间均在 603.56–605.62s），均退出 0。第三批在版本同步后的文本上单跑 `check.sh`（PID 22730）退出 0。第四批在按独立审查两项 P2 修复后的文本上同批并发重跑 `check.sh` 与 `version-gate`（PID 23856/23857），均退出 0，`git diff --check` 通过。第五批在按复审意见区分额度 1 与 2–3 的措辞修正后同批重跑同样两项（PID 24533/24534），均退出 0。条文与验证日志同目录可回查；本段写入后未再改规则文本。

未验证项：本机未安装 PyYAML，版本提取的 PyYAML 分支未跑；B58/B59 目前只有文字规约，本轮未做真实模型行为回归，也未在 Linux CI 上运行；规则仍依赖执行者诚实取证，只降低绕过概率，不构成强制机制。

## 0.14.5 更新说明（2026-10-24）

- SWE-bench runner 的补丁应用失败不再被结果读取器丢弃（P2）：`protocol.is_valid_result` 只对一种终态放宽 `test_contract_consistent` 与 `p2p_evidence`——`resolved=false`、`f2p_pass=false`、`patch_applied=false`、`test_statuses={}`、`f2p_status` 全为 `MISSING`、`p2p_regressions=[]`；其它形状（含伪造 resolved 或携带测试结果）仍严格拒绝。`grade_patch` 的 test patch 失败分支同样报 `patch_applied=false`，因此两种补丁失败共享这一终态：`run` 的下次调度视为 complete（不重复调用模型），`arm_summary.patch_apply_failures` 真实计数。
- 事件归因的 `receiver_thread_ids: null` 按「等同缺失」处理（读取处 `or []`），`analyze_events.py` 不再因 `TypeError` 崩溃；畸形行仍计入统计且不中断（P2）。
- 保护模式的软链拒绝收敛到 `restore_protected_modes` 入口（与 `lock_protected_paths` 共用）：软链记录在读取与 chmod 之前就被拒绝，不再先改目标里的 mode-0 路径（P2）。
- `lock_protected_paths` 的 chmod 循环中途失败时立即恢复本次已锁定的路径并保留原异常；`protected-modes.json` 仍保留供下次恢复（P2）。
- B01 场景删除源码精确字符串条件 `'BUTTON_LABEL = "保存"' in content`，只以运行时导入结果判定；prompt 未规定引号与空格，单引号/无空格等合法变体通过，文案未改的负例仍失败（P2）。
- 复审 N1：B01 的运行时核对保留 `if BUTTON_LABEL != "保存": sys.exit(2)` 显式比较，并捕获导入、取值与比较期间的 `SystemExit`，统一以非零码失败。每次 `evaluate` 用 `secrets.token_hex` 生成随机完成标记，经子进程环境变量传入；通过条件为「退出码 0 且 stdout 精确等于本次标记」，不去除首尾空白。表驱动 CLI 回归拒绝硬编码 `print("ok")` 后提前退出、只打印标记、打印标记但文案未改、直接退出与多余 stdout；`PYTHONOPTIMIZE=1` 下文案未改仍失败，单引号/无空格与正常改动通过（P2）。
- `markdown_report` 新增「PASS_TO_PASS 排除」小节：按已校验的 `task-baselines` 披露每任务 eligible/excluded P2P 数量与排除状态原因，补齐 `tests/experiments/swebench/README.md` 的披露要求（P3）。
- 复审 N3：该小节的表格单元格在渲染前转义 `|` 并把换行折叠为空格，含 `|` 的合法参数化 nodeid（如 `tests/test_parser.py::test_pattern[a|b]`）不再截断 GFM 表格，完整名称与状态仍出现在报告里（P3）。
- 新增 `scripts/skill_version.py` 作为唯一版本提取实现：有 PyYAML 时用拒绝重复键的 loader；无 PyYAML 时用严格行匹配（`version` 必须直接隶属 `metadata`、拒绝重复、忽略块标量内容，不把其他映射下的 `version` 当作 `metadata.version`）。`scripts/check.sh` 与 `scripts/smoke-install.sh` 都调用它，提取与比较使用同一次解析结果（P3）。
- 新增 `easydev/tests/cases/version-gate/run.sh`：在临时目录构造九组 fixture（合法、`description` 内含 `version:` 但 `metadata.version` 合法、重复 `version` 键、`version` 错位到其他映射、只在 `description` 里出现 `version`、带引号的重复 `version` 键、双引号转义后的重复 `version` 键、值内嵌套引号、未闭合引号），断言前两组通过、其余被拒绝，并对降级分支与 PyYAML 分支（已安装时）分别执行；CI 增加该步骤（P3）。
- 复审 N2：`scripts/skill_version.py` 的降级分支支持单/双引号子键，解引号后检测重复 `version`，并拒绝无法解释的 2 空格映射行（注释、序列项除外，更深缩进跳过）。严格子集不解释双引号键中的反斜杠转义，遇到即明确报错，避免把 `"vers\u0069on"` 静默当成无关键；单引号键保持字面语义。版本值只接受恰好一对正确闭合的同类引号，解引号后仍含引号或未闭合即拒绝，其余按 `x.y.z` 校验。新增转义重复键 fixture 在降级分支被拒绝，PyYAML 分支仍由 StrictLoader 按解码后的重复键拒绝，未引入新依赖（P3）。
- 安装冒烟测试的快照 `git init/add/commit` 统一加 `-c commit.gpgsign=false -c tag.gpgsign=false`：维护者全局 `commit.gpgsign`/`gpg.program` 配置不再造成假失败，也不调用签名程序（P3）。
- 0.14.4 条目里的本机绝对路径改写为「macOS 本机，仓库根目录」表述（P3）。

验证：阶段 1 先扩展 B01 表驱动 CLI 负例与转义重复键 fixture，在未改实现时确认红测：B01 有三项子例误通过（伪造标记后 `SystemExit(0)`、伪造标记后 `os._exit(0)`、完成标记前多余空行），版本降级分支错误输出 `0.14.5`；修复后同一组回归全部通过，单引号版本键与单引号内字面反斜杠的 CLI 检查也保持通过。交付检查在 macOS、Python 3.9 同批并行执行，分别保存日志：`bash scripts/check.sh`、`bash easydev/tests/cases/public-contract-callers/run.sh`、`bash easydev/tests/cases/version-gate/run.sh`、`bash scripts/smoke-install.sh`、agent_runner unittest（29 项）与 SWE-bench unittest（48 项）均退出 0；版本降级分支九组 fixture 为两组通过、七组拒绝。`git status --short --untracked-files=all` 与暂存区核对确认未暂存，`git diff --check` 通过。安装冒烟本轮裸 `mktemp -d` 预检通过，直接执行原命令；此前全局签名配置的隔离验证保留为前两轮证据。

未验证项：本机未安装 PyYAML，`skill_version.py` 与版本门禁的 PyYAML 分支留待已安装 PyYAML 的 CI 执行；本轮未在 Linux runner 上运行，`ubuntu-latest` 的 `/proc` 进程枚举差异仍待 CI 补充。本轮只完成实现与离线验证，独立复审由主代理继续调度；真实 Codex CLI、真实 pytest 与真实 SWE-bench 数据未运行，测试只用替身验证验收机制。随机完成标记用于拒绝硬编码输出，仍不能替代执行任意不可信代码所需的系统级隔离。

## 0.14.4 更新说明（2026-10-24）

- 修复深度审计确认的运行时规则冲突：`references/planning.md` 的「继续实施」补上不适用于「必须提问」情形的限定，消除可逆低成本默认与可见行为选择的重叠；`references/worktree.md` 收尾先核对授权基线（已获明确集成授权时按授权执行，推送、PR 与发布仍需明确授权），清理回退改为按路径 `git worktree remove`，不再附带仓库级 `git worktree prune`。
- CI 补上三套离线维护检查（`easydev/tests/cases/public-contract-callers/run.sh`、agent_runner 与 SWE-bench 单元测试）：此前 CI 只跑 `scripts/check.sh` 与 `scripts/smoke-install.sh`，而 `tests/` 下文档已按 CI 会运行它们来陈述，Linux `/proc` 差异也指望 CI 补充。
- 修正 `tests/README.md` 两处失实与漂移：固定补丁复验的红绿两段仍运行夹具自带的 `check_orders.py`（不依赖它的是独立验收 `acceptance.py`）；目录树补上 `audit-0.12.1.md` 与 `agent_runner/test_process_control.py`。

验证：在 macOS 本机，仓库根目录按序实跑：`bash scripts/check.sh` 退出 0（版本一致性 0.14.4、链接与锚点、README bash 代码块、模型配置示例均 OK）；`bash easydev/tests/cases/public-contract-callers/run.sh` 退出 0（红/绿/独立验收通过，11 类伪修复被拒绝，无 `__pycache__` 残留）；`cd easydev && python3 -m unittest discover -s tests/experiments/agent_runner -p 'test_*.py'` 退出 0，28 项通过（66.9s）；`cd easydev && python3 -m unittest discover -s tests/experiments/swebench -p 'test_*.py'` 退出 0，43 项通过（55.6s）；`ruby -ryaml -e 'YAML.load_file(".github/workflows/checks.yml")'` 退出 0；`git status --short --untracked-files=all` 列出 8 个已修改文件且无未跟踪文件，`git diff --check` 退出 0。新增的三步作业未在 Linux runner 上实跑，本机无 PyYAML、CI 的 PyYAML 分支也未实跑；`ubuntu-latest` 上的 `/proc` 进程枚举差异仍待 CI 结果补充，本机验证只代表 macOS。维护规约要求的 B40/B49/B46 场景本轮未做决策回放或真实行为验证，只完成静态原文对照；这三项在补齐回放前不计为已通过，按用户决定先记录、暂缓补做。

## 0.14.3 更新说明（2026-10-24）

- 按 0.14.2 的深度审计补齐门禁：根 `README.md` 的 EasyDev 版本行纳入 easydev 组（该行此前仍停在 0.14.0）；`SKILL.md` frontmatter 校验扩展到 easydev（体量不设上限，沿用无政策状态）；新增"技能目录必须登记版本来源"的覆盖断言；版本问题改为一次列全，不再在首个失败项目处退出。
- 新增模型配置示例校验：`easydev/examples/*.json` 必须可解析，且 `README.md` 内联示例与 `models.cross-provider.json` 必须一致（对应此前只在发布清单里声明的检查）。
- 降级 frontmatter 校验修正假阳性：块标量（`>`/`|`）内的 `": "` 是字面文本，只有内联 description 才拒绝；此前会让 easydev 这类含英文冒号的块标量在本机（无 PyYAML）误报。
- `scripts/smoke-install.sh` 增加 easydev 场景：按其 README 提示词的文件集（SKILL.md + references/ + agents/）落盘，断言不含维护文件、references 数量一致、name 正确，并校验 skills 根目录同时容纳两个技能。
- `README.md` 的外部边界枚举与 `SKILL.md` 对齐（网络、命令行工具、时钟、环境能力等）。

验证：`bash -n` 与 `bash scripts/check.sh` 通过；8 项负例（根 README 版本、easydev 版本不一致、frontmatter 缺 version、name 不符、非法 JSON、README 示例漂移、未登记技能目录、easythinking 回归）均被拦截，两个项目同时不一致时一次列全；`SMOKE_TMP` 指定的临时 HOME 下 `bash scripts/smoke-install.sh` 通过（含新增 easydev 场景）；两个 `SKILL.md` 的 frontmatter 经 Ruby YAML 解析通过，本机无 PyYAML，CI 的 PyYAML 分支据此推定、未实跑。

## 0.14.2 更新说明（2026-10-24）

- 门禁补上 easydev：`scripts/check.sh` 的版本一致性改为按子项目分组（easythinking 与 easydev 各自内部一致，不再要求跨项目同版本），新增 `easydev/README.md`、`easydev/SKILL.md`、`easydev/CHANGELOG.md` 三处版本提取；链接与锚点检查覆盖 `easydev/**/*.md`，bash 代码块语法检查同覆盖两个 README。
- `README.md` 补上实现已有但此前未写明的约束：升级换角色后须为接管开发的模型重建独立审查实例、`ultra` 仅按明确要求启用、验证硬约束摘要，以及提交/发布/部署需显式授权（项目规则不能替代）。只改说明与门禁，运行规则未变。

验证：`bash scripts/check.sh` 通过，并用临时改坏版本号与链接的反例确认新增覆盖会失败；easydev 仅改 `SKILL.md` 版本号，其余运行时文件未变。

## 0.14.1 更新说明（2026-10-24）

- 删除 `examples/models.default.json`：它与内置 `inherit` 默认值语义重复，且无自动校验可防漂移；默认结构改由 `references/models.md` 描述，示例目录只保留能力演示性的 `models.cross-provider.json`。同步更新 `README.md`、`references/models.md` 与 `examples/README.md`。
- `examples/README.md` 的配置优先级不再写模糊的 `easydev/models.json`，改为完整路径（macOS/Linux `~/.config/easydev/models.json`、Windows `%APPDATA%/easydev/models.json` 及回退），与 `references/models.md` 保持一致。
- 跨供应商示例改用当前可用的完整模型 ID：开发从 `opencode-go/deepseek-v4.1-flash` 起步，两档升级分别由 `opencode-go/deepseek-v4-pro` 与 `codex/gpt-6.1-sol` 互换开发/审查角色，深度统一 `high`；`README.md` 的内联示例与阶段表同步为同一组合，避免两处漂移。

验证：`examples/` 下 JSON 解析通过；文档相对链接与锚点、版本号（`SKILL.md`、`README.md`、本文件）一致性检查通过；未执行真实 Agent 行为回归。

## 0.14.0 更新说明（2026-10-24）

- 复核 0.13.0 技能内容审计，修复候选评审第二轮缺少委派额度的问题：新增实例上限为 `N + 2N + 2`，为第二轮保留最多两个复核实例；优先复用符合作者隔离条件的评估者，分别记录实例数与审核轮次，明确该额度不包含后续开发和最终审查。
- 澄清审查强度的选择与高风险下限的应用顺序、配置异常只暂停依赖配置的分派；环境适配中的配置路径与优先级改为引用唯一来源。
- 补充文档与技能包的审计路径，按典型请求、阶段及失败场景描述假设负载；同步 B51 的既有文字规约。
- 保留已有的四轮预算预留、动态验证隔离与静态降级规则、按需加载所需的规则摘要及 Codex 标准 `agents/openai.yaml`；这些未被证实为审计所称的缺陷。

验证：运行时与指南的 134 个相对链接/锚点、版本一致性、Ruby YAML frontmatter 与展示元数据解析通过；静态核算 N=2/3/4/5、每候选 1/2 名评估者及第二轮复核的额度。Python 通用 Skill 校验器因缺少 PyYAML 未运行成功，已用 Ruby YAML 补足格式检查。未执行真实 Agent 行为回归，B51 仍为文字规约。独立审查状态在交付记录中说明；配置中的 Gemini 开发模型不受当前委派接口支持，本次由主代理实施，不称完整模型编排闭环完成。

## 0.13.0 更新说明（2026-10-24）

- 高风险边界改为开放式下限：至少包括安全与供应链、鉴权与授权、支付、敏感数据、并发一致性、破坏性数据操作与迁移兼容；无法排除相关影响时进入 `full`，不再因清单未列或改动只有一行降级。
- 审查与编排补不可信代码执行边界：待审仓库的测试、构建、依赖安装和项目脚本仅在断开网络、无宿主凭证、被审目标只读挂载的操作系统沙箱或容器级隔离中运行；系统临时目录、“只读检查”标签或审查者意图本身不构成安全隔离，否则只做静态检查并报告未验证范围。
- 工作区隔离补脏状态处理、创建失败确认与本地集成恢复边界；子代理交接要求按任务命中模块补读并回报实际加载内容，覆盖 `planning`、`candidates`、`refactoring`、`verification`、`baseline-safety`、`debugging` 与 `worktree`。
- 同步 README 与 B02、B06、B16、B51 文字规约；新增安全边界未增加可执行测试载体。

验证：Markdown 相对链接与锚点、版本与运行时契约断言、表格结构及 `git diff --check` 通过。独立审查因宿主 `spawn_agent` 工具不受支持而受阻，仅完成自查（非独立），不称 `full` 闭环完成；B02/B06/B16/B51 为文字规约，未执行真实 Agent 回归。

## 0.12.1 更新说明（2026-10-24）

- 复核深度审计发现，修复评分证据、相对路径、后台进程清理与验收绕过；新增对应回归，逐项裁决见 `tests/audit-0.12.1.md`。
- SWE-bench 以 JUnit 判定测试结果，排除 pytest 配置补丁，拒绝异常退出与证据矛盾；仍要求可信隔离环境，不能防止任意代码篡改评测进程。
- B02 增加运行期数据/所有权变化验收及显式读取策略；验收要求完成标记，超时保存为结构化失败，嵌套 `.git` 不再漏检。
- 独立验收允许异常文本回显本次请求已知 ID，仍拒绝未知所有者、金额及其他订单数据。

- 修复 quick 性能优化/效果声明未加载测量要求的问题，同步 B56 文字规约。
- 行为 runner 在切换工作目录前归一化路径；增加相对输出、工具、Skill 与认证路径的 CLI 回归。
- B02 使用文件数据源及实际文件访问审计，拒绝先读数据后校验身份的伪修复，保留合法读取与所有权验收。
- 环境适配文档的安装说明改为仓库链接，最小安装包不再引用未随包分发的 README。
- 按用户决定保持固定模型配置路径，不恢复 XDG_CONFIG_HOME 支持。

验证：Python 3.9/3.12 下分别通过 agent_runner 28 项、SWE-bench/归因 43 项及固定补丁复验（11 类伪修复拒绝、2 类合法异常文本接受）；新增绕过用例具修复前失败/修复后通过证据。Markdown 链接与锚点、Python 3.9 语法、版本及 diff 检查通过。未执行真实 Agent 回归。Claude/Zed 额度耗尽后，经用户授权改用 GPT-6.1 Sol/high；两轮独立审查完成，首轮确认的 1 项 P1 与 5 项 P2 均经修复、回归及复审关闭。

## 0.12.0 更新说明（2026-10-23）

- 新增 `references/verification.md` 并接入按需加载与执行模块：围绕当前任务复用启动、实例检查、真实入口操作、结果与副作用取证及清理方法；反复使用时才保存已验证步骤。
- 执行模块补性能测量可信度要求，排障模块补重复问题的结构性预防；新增 B55–B57 文字行为规约，尚未执行真实 Agent 回归。
- 新增 `references/refactoring.md`：行为基线、可检查的结构收益、分阶段兼容迁移、旧新版差分验证及重构交付；沿用 quick/full 与现有审查预算。
- `SKILL.md` 接入按需加载并明确纯重构不要求红测；执行、审查模块及 README 同步引用；新增 B52–B54 文字行为规约，尚未执行真实 Agent 回归。
- 模型配置文档统一 macOS/Linux 用户外部配置路径为 `~/.config/easydev/models.json`，README 补充查看方式。

验证：本地 Markdown 链接与锚点、版本一致性及 `git diff --check` 检查通过；仅做自查，B52–B57 尚未执行真实 Agent 回归。通用 Skill 校验器因环境缺少 PyYAML 未运行；未更新本机安装副本。

## 0.11.1 更新说明（2026-10-23）

- 安装与更新改为可直接交给 Agent 的提示词：通用目录保存技能，专用目录按实际支持情况使用软链接、目录联接或复制。
- 保留本地修改保护、运行时文件范围、安装完整性与实际加载状态检查；删除重复的命令及目录表。
- 修正环境适配文档中的安装链接，指向当前仓库 README。

验证：版本一致性、文档链接与 `git diff --check` 检查通过；未在各宿主中实测安装或加载。

## 0.11.0 更新说明（2026-10-22）

本次发布新增「候选方案评审」：设计阶段先产出 N 个互相隔离的候选，再由未参与该候选产出的实例交叉评估，最后由主代理按统一矩阵择优。这是设计侧的可选子流程，不改变单一写入者、作者隔离、`r/4` 预算与完成边界。

- 新增 `references/candidates.md`：启用条件（两个以上实质不同且都成立的方案，差异影响验收、契约、成本或回滚）；默认 N=3、上限 4（含一个最小改动基线）；候选作者互不可见，评估者不得评自己产出的候选；候选冻结与 `v2` 重评规则；固定五维交叉评估与带证据输出；先定权重后评分的择优矩阵、弃因与吸收项；`N+2N` 委派上限与两轮评估循环；无子代理能力时标“自查（非独立）”；账本与交接记录格式。
- `SKILL.md`：`design` 行接入候选评审；按需加载表新增 `candidates.md` 行；“与其他技能共存”明确候选评审同属内置规程，不构成第二套审查体系。
- `references/planning.md`：深度选择表与脑暴段落改为按 candidates.md 判定是否派生候选；拆分段明确择优后仍由单一开发者实施。
- `references/orchestration.md`：角色段补候选作者与评估者的实例隔离；审查时机新增“设计阶段存在多个实质不同方案”一行（占用前置设计审查轮次）；账本模板与报告补择优结论。
- `references/review.md` 与 `references/models.md`：设计/计划审查指向候选评审，择优结论不替代对采用方案的审查；逐候选模型属对话运行期覆盖，不新增配置键。
- `README.md`、`tests/behavior.md`：新增“候选方案评审”运行规则小节、模块表与目录条目、使用示例；新增 B51 行为用例与维护对应。
- 审计后修复：未提交改动的独立深度审计发现 1 项 P2——每候选“至多 5 条发现”可能截断已确认缺陷，改为摘要上限并强制完整列出所有确认 P0/P1、范围内 P2 与验收缺口；同时补“能委派但无法保证候选互不可见”的受阻与降级路径，把“补一轮”限定在两轮上限与第二轮对象内；B51 增补超量缺陷、隔离受阻与对照四验收；CHANGELOG 日期改为实际发布日。
- 未纳入：多候选产出的自动评分或外部评测协议；真实宿主行为回归（B51）未运行。

验证：`node ../scripts/version.mjs check` 通过；agent_runner 22 项与 `public-contract-callers` 固定补丁复验通过；运行时 Markdown 相对链接与锚点检查无失效；审查记录分两段，互不覆盖：功能实现阶段为完整初审 `r=1/4` 发现 1 项 P2（`v2` 重评对象冲突），定向复审 `r=2/4` 关闭且无新发现；审计后修复阶段为独立审查 `r=1/4` 发现 1 项 P2（本条验证行未区分两阶段），修复后定向复审 `r=2/4` 确认关闭；B51 为文字规约，未执行真实宿主回归。

## 0.10.0 更新说明（2026-10-21）

本次发布修正规则契约之间的冲突，并把审查侧往返成本收敛到可执行口径；不改变任何代码行为，只影响代理如何判定交付、如何处置模型配置与审查争议。

- 交付门禁：`references/review.md` 的 P2 定义改为“单次 review/audit 报告中不阻断用户决策，进入 full 闭环或授权修复时属范围内必关项，仅用户明确接受或延期可带出”，与 `orchestration.md` 处置矩阵、`execution.md` 完成边界一致。
- 模型配置：`models.md` 把强约束前提改为“存在且解析有效”，补用户显式确认默认值/对话指定文件的异常恢复；区分有委派才“应用”、无委派只记录来源；分派后不一致改为“修正请求后最多重试一次，仍不满足按 setup 暂停”。`SKILL.md` 与 `orchestration.md` 只留摘要并指向 `models.md` 唯一事实源，外部路径仅在 `models.md` 维护；`upgrades` 相邻阶段须至少一个角色的模型或深度不同，完全相同按无效配置处理。
- 分流与轮次：`orchestration.md` 的 review/audit 转授权修复补 `quick` 分支（自查、不设轮次），不再一律记 `r=1`；`review.md` 的作者隔离限定为“审查对象包含本会话产生的改动”。
- 审查成本：作者争议允许可复现证据交同轮审查者复核后关闭、不额外占轮，无法复现或复核不通过才交下轮；处置矩阵同步为“视证据”。审查者可在系统临时隔离目录执行只读检查与构建（不写入被审工作区、共享缓存或外部系统），`review.md` 强度表与预算安全段按写入边界同步，不再一律回交开发者。
- 账本与探针：小任务的授权基线与采用的默认项至少在会话 scratch 或账本留一行，供压缩后重建；15 秒能力探针仅限依赖 pty、真实网络或无头浏览器的测试，纯进程内检查直接运行。
- 交付口径：`SKILL.md` 的 audit 行补“最多 20 条、假设负载、未检查部分与验证状态”；full 报告补实际模型与深度、`r/4` 与升级原因；`setup.md` 的加载条件与按需加载表对齐。
- 文档同步：`README.md` 的 audit、探针、作者争议、模型不一致处置改为新口径；`tests/behavior.md` 的 B27、B50 期望同步。
- 未纳入：任务级累计修复次数上限（与“范围内确认缺陷均须关闭”冲突，保留设计取舍）；README/tests 中与本次改动无关的旧口径与测试载体缺陷不在本版处理，测试代码按计划另行重写。

验证：`node scripts/version.mjs check` 通过；agent_runner 12 项与 swebench 63 项离线单测通过；10 个运行时文件的链接与锚点 0 失效；本轮改动经一轮独立审查发现 2 项 P2 并修复，第二轮定向复审关闭且无新发现。真实宿主行为回归（B01/B02/B27/B50）未运行。安装副本需按安装指引重新同步 `SKILL.md` 与 `references/` 后生效。

## 0.9.0 更新说明（2026-10-21）

本次发布把「测试只验证用户可感知行为」和「验证实验不得无界等待」写成硬约束，避免测试绑定内部实现，以及交互式或阻塞实验反复卡住任务。

- `SKILL.md`「测试硬约束」：原「按行为定粒度」改为「按用户可感知行为定粒度」，要求断言命令输出、退出码、界面状态、文件与外部系统副作用，优先通过 CLI、界面或公共 API 驱动，禁止按代码行、私有 helper、内部调用顺序或实现步骤拆测；新增「仅外部边界可 mock」，确实无法从可感知行为观测时才 mock 网络、命令行工具、时钟等外部边界，不 mock 被测代码内部结构，需要内部入口时选最小入口且断言仍落在外层结果。
- `SKILL.md`「全程约束」新增「验证实验防卡死」：可能阻塞的命令必须带硬超时，超时立即终止并换方案；设计测试前先做最长 15 秒的能力探测（pty、网络、浏览器等），探测失败改用 mock 或进程内方案；同一验证思路超时或连续 2 次无新证据即停止，修复尝试仍按既有三次上限、两者分开计数；不设计依赖多轮交互输入的自动化测试，必须交互的流程改成单轮可测或 mock 外部边界。
- `README.md`、`references/execution.md`、`references/review.md` 同步上述规则：README 补可观测断言、外部边界 mock、硬超时与能力探测；`execution.md` 的测试选择新增「可观测性优先」，验证调度补超时与探测；`review.md` 的「测试与体验」改为核对用户可感知行为与回归覆盖、断言不依赖内部实现。
- `tests/behavior.md` 新增 B50 可观测测试与防卡死，编号范围更新到 B50，并把该场景加入测试与调度规则变更后的回归集合。
- 仓库根 `test/version.test.mjs`、`test/reset.test.mjs` 改为只通过 CLI 与文件系统断言用户可感知结果：版本脚本覆盖 `show`/`check`/`notes`/`bump`/`sync` 与错误输入；重置脚本覆盖帮助、参数校验、非交互拒绝、本地重置、配置校验与凭证拒绝，远程删除只在无法端到端观测时 mock wrangler 与 Cloudflare API 两个外部边界。`.github/workflows/version-check.yml` 同时运行这两个文件。

验证：`node scripts/version.mjs check` 通过；`node --test test/version.test.mjs test/reset.test.mjs` 19 项通过（约 2 秒）；B50 未执行真实宿主回归。安装副本需重新同步 `SKILL.md` 与 `references/` 后生效。

## 0.8.1 更新说明（2026-10-21）

本次发布修复审计发现的规则冲突、验证载体与维护脚本缺陷。

- `SKILL.md`：quick 收尾按暴露内容分流（高风险边界或公共契约影响升级 `full`，仅验证或实现细节超出内联核心时加载模块并留在 `quick`），删除「复杂 quick」表述；`full` 选择条件补上高风险边界；按需加载的 execution 行改为同一表述；模型配置来源段只在需要字段校验、用户指定模型或升级规则时读取 `models.md`；收尾段补上 `debug（仅定位）`；聊天表达段说明用户只写 `ultra` 时按表达档位处理并说明推理深度未改变。
- `references/orchestration.md`：审查轮次改为发起即记 `r/4`（占用）、完成后更新为（完成）或（受阻），连续重连超过 2 次按环境故障报告受阻；强度分档链接改指 `review.md#强度分档`。
- `references/planning.md`：范围与授权链接文本改为「全程约束中的范围与授权」；「未答默认」只在用户明确表示跳过/继续时生效，用户未回应时依赖该项的实现保持暂停。
- `references/execution.md`：既有 `quick` 历史不占 `full` 阶段 0 的试修预算（配置 `upgrades` 时该阶段为 1 次），也不消耗无 `upgrades` 时的三次预算。
- `references/setup.md`：新增「Codex 与 Pi」小节，记录可验证的分派字段与「审查实例只读、与实现实例不同」的要求，不写未验证的工具签名。
- `tests/experiments/swebench/run.py`：模板解包允许指向包内相对路径的软链与硬链，拒绝绝对路径、空或含 NUL 的目标，以及词法上或解析后逃出目标目录的链接（`..` 逃逸、经由已有包内软链的逃逸、硬链指向归档外路径）；软链目标按链接所在目录、硬链目标按归档根解析；解包按归档成员顺序逐个校验并立即提取，避免归档内后建链接绕过预扫描，无 `data_filter` 的运行时同样安全，目录成员只建目录、不在写入阶段改权限，所有成员提取完后，目录属性使用创建时解析的规范路径并在应用前复核包含性，按深度降序应用目录的 mode 与 mtime（单个目录失败不中断其余目录），保留只读目录归档的可用性，支持的 Python 版本在 `extract` 时传 `filter="data"`；Codex 运行目录收紧到 `0700`；先原子写入 `protected-modes.json` 再 `chmod(0)`，实验开始时按记录恢复被中断运行留下的 0 权限。`test_run_protocol.py` 的 tar 链接用例从 1 条扩展到 8 条（包内软链/硬链、子目录相对软链、`..` 逃逸、绝对路径、硬链逃逸、归档内后建链接链的两种成员顺序），并新增只读目录（0555）、嵌套只读目录（0500/0500）、逃出目标目录的目录成员、目录属性落在创建时规范路径、目录属性按深度降序共五条用例。
- `scripts/version.mjs`：`version` 必须位于 `metadata:` 块内，块内缺失或版本挂到其他顶层键时校验失败；`sync` 用同一规则定位再写入。`test/version.test.mjs` 新增合法与错位两条用例。
- `tests/cases/public-contract-callers/acceptance.py`：新增 `track_order_value`，让后来出现的所有者值同时进入值集合与文本扫描正则。
- `.github/workflows/easydev-fixture.yml`：新增 agent_runner、swebench 离线单测；`.github/workflows/version-check.yml` 固定 Node 20 并新增 `node --test test/version.test.mjs`。
- `tests/behavior.md` 与 README：说明当前只有 B01、B02、B16 与 `public-contract-callers` 具备可执行载体；README 行为场景编号改到 B49，Skills CLI 安装补安装后校验命令，维护条目补 Node 单测的版本约束。
- 独立审查 r1 修正：`run.py` 把 `protected-modes.json` 的恢复移到读取 `dataset.json`/`task-baselines` 之前，中断重跑不再因数据文件为 mode 0 直接 `PermissionError`；恢复只处理当前 mode 为 0 的路径（不覆盖用户后来收紧的权限），记录必须是「路径→0..0o777 整数」的 JSON 对象，损坏时抛可执行的 `RuntimeError`，正常结束删除记录（`lock_protected_paths`）。`scripts/version.mjs` 只认顶层 `metadata:` 的直接子键 `version`，块扫描跳过空行与 `#` 注释行，`check`/`sync` 共用同一套定位（新增 `updateSkillFrontmatterVersion`）。`SKILL.md` 按需加载表 models 行改为「需要字段校验、用户指定/调整模型、配置异常或需要升级时的来源核对与应用」，与第 33 行一致。版本脚本单测从 `easydev-fixture.yml` 移到对所有 push/PR 生效的 `version-check.yml`。README 维护段区分仓库根与 `easydev/` 目录的 Node 测试命令；`references/execution.md` 恢复 `quick`/`full`/`upgrades` 反引号；`references/setup.md` 补 Pi 字段以宿主当前工具 schema 为准。新增 `test_run_protected_modes.py` 与 `test/version.test.mjs` 表驱动用例。
- 独立审查 r2 修正：`run.py` 锁定前用 `expanduser().resolve()` 把保护路径（含 `--protect-paths` 与 `experiment` 派生路径）规范化为去重的绝对路径，锁定时归并为不重叠集合（祖先已在集合时丢弃其后代，只锁父目录）；恢复只接受绝对路径条目，遇到相对路径抛 `RuntimeError` 并给出记录路径与手工 `chmod` 出口，不再按当前工作目录解释旧记录；恢复与 `finally` 还原按路径深度升序执行，祖先先于后代，父目录为 mode 0 也不再阻断子路径。`test_run_protected_modes.py` 新增相对路径记录、旧格式拒绝、父子归并与恢复顺序、跨目录重启五条用例。README、`tests/behavior.md` 与本节 CI 说明按实际工作流同步：固定补丁复验与 agent_runner、swebench 离线单测在 `easydev-fixture.yml`，版本校验与版本脚本单测在 `version-check.yml`，真实 Agent 行为回归尚未接入。
- 独立审查 r3 修正：`run.py` 的 `lock_protected_paths` 在写入记录与任何 `chmod` 之前，把 `protected-modes.json` 与归并后的保护路径都 `resolve()` 比较，保护路径是记录的祖先（含记录文件本身）时抛 `RuntimeError`，消息写明被拒绝的保护路径、记录路径、原因（记录会被锁住导致中断后无法恢复）与出口（改为保护更具体的子路径，或不要保护实验目录），拒绝时不写记录也不改权限；符号链接别名经 `resolve()` 同样被拦住。`restore_protected_modes` 的校验循环拒绝含 `..` 组件的记录条目（与相对条目同一处置：抛 `RuntimeError` 并给出记录路径、原路径与手工 `chmod` 出口），不在可能含符号链接的旧路径上做词法折叠；所有条目先全部校验通过再 `chmod`，不会先恢复部分条目。`test_run_protected_modes.py` 新增保护实验目录、保护其祖先、保护指向它的符号链接、旧记录含 `..` 条目四条拒绝用例与保护 `task-baselines` 子路径仍允许的对照用例。
- 独立审查 r4 修正：`run.py` 的 `lock_protected_paths` 在写入记录与任何 `chmod` 之前，发现记录路径本身是符号链接（用 `os.path.lexists` 覆盖悬空链接）就抛 `RuntimeError`，消息说明记录路径必须是普通文件、链接可能来自旧运行或外部预置，并给出出口（删除链接让工具重建记录，或手工 `chmod` 恢复权限后重跑）；`reject_record_inside_protected_paths` 改用 `record_path.parent.resolve() / record_path.name`（`os.replace` 的实际落点）与保护路径比较，不再用会跟随链接的 `resolve()` 结果，避免「记录位于被保护目录内」被放行后记录实际落回实验目录、中断时被 mode 0 遮住。`test_run_protected_modes.py` 新增五条用例：记录路径是指向实验目录外有效 JSON 的链接时分别保护实验目录与保护 `task-baselines` 子路径都被拒、悬空链接同样被拒、同结构下普通（不存在）记录路径正常锁定与清理的对照，以及落点包含判断的直测。

验证：`node scripts/version.mjs check` 通过；`node --test 'test/*.test.mjs'`（14 项）、agent_runner（12 项）与 swebench（63 项）离线单测通过；`tests/cases/public-contract-callers/run.sh` 在本机用显式 `/tmp` 模板的 `mktemp` 包装后通过（macOS `mktemp` 忽略 `TMPDIR`，夹具本身未改）；本地 Markdown 链接与锚点检查 0 失效；tar 包内软链/硬链与子目录相对软链三条允许用例在一律拒绝链接的旧实现下报 `RuntimeError`（已复现），三条拒绝用例在移除逃逸检查后不再抛 `RuntimeError`，其中逃逸软链会留下指向目标目录外的软链、绝对路径软链会留下指向 `/etc/passwd` 的软链（已复现），`test_run_protected_modes.py` 的新用例分别在移除对应修复后按预期失败（已复现：去掉 r3 的前置拒绝后，保护实验目录、保护其祖先、保护指向它的符号链接三条拒绝用例报「RuntimeError not raised」；去掉 `..` 条目拒绝后，该用例报「RuntimeError not raised」，且正常条目被提前恢复为 `0o644`，而拒绝后才保持 `0`；r4 的两点修复同时去掉后，记录链接的三条拒绝用例报「RuntimeError not raised」，只去掉叶节点拒绝后保护实验目录的用例改报包含判断的消息、子路径与悬空链接两条仍报「RuntimeError not raised」，只把落点换回 `resolve()` 时落点直测报「RuntimeError not raised」）。本轮把模板解包改为逐成员校验并立即提取后，新增的链接链用例在「全量预扫描 + `extractall`」的旧实现下报「RuntimeError not raised」，同一归档会把 `pwn` 写到目标目录外（已复现），恢复后通过；把目录成员改回逐个 `archive.extract()`（立即应用归档 mode）后，只读目录与嵌套只读目录两条用例在 Python 3.9 与 3.12 上都报 `PermissionError`（已复现：`repo/file.txt` 写入与 `a/b` 建目录被拒），恢复逆序应用后通过；本轮把目录属性改为「创建时解析的规范路径 + 应用前复核」后，把属性阶段改回字面路径并去掉复核时，目录属性用例因目标目录外哨兵目录的 mode 与 mtime 被改（0o750→0o500、mtime→归档值）而失败（已复现），改回 `reversed()` 时先子目录后父目录的顺序用例报「0o755 != 0o500」（已复现），恢复后通过。r4 的两点修复与本轮解包顺序修复为自查复现，尚未经过下一轮独立审查。行为场景与真实宿主回归未运行。

## 0.8.0 更新说明（2026-10-21）

本次发布补上「意图不清时怎么问」的缺失环节：此前只有「何时可以请求用户决定」，没有提问格式与禁止猜测的约束，模型倾向按自己的理解继续。

- `references/planning.md` 新增「选项式澄清」：把互斥解读收敛成带 `[推荐]` 的有限选项，最多 3 题，每题 2–4 个互斥选项加「其他（说明）」，每题给「未答默认」且默认项须安全；给出必须提问（改变可见行为、公共契约、数据或兼容承诺、成本量级，或高风险边界无安全默认）与继续实施（唯一解读、常规技术选择、可逆低成本默认）两侧边界；规定待答期间的分工、答复入账、同一问题不重复问，并禁止把猜测写成已确认需求。附格式示例。
- `SKILL.md` 开工第 1 步改指「选项式澄清」并禁止自行选定一种解读继续，`全程约束` 新增「不猜意图」；按需加载表的 planning 行标注该职责。
- `references/execution.md` 收敛「需求含糊先做最小版本」：核心工作与验收唯一时仍先做最小版本，存在会改变结果的互斥解读时先问再改；审查反馈里需用户决定的部分改由主代理按该格式合并提问。`references/review.md` 的「严重歧义时提问」指向同一规则，避免审查者各自向用户开问。
- `references/orchestration.md` 交接段要求子代理不直接问用户、不以猜测补缺，把候选选项交主代理合并，并在交接中记录采用的默认项与未确认解读。
- README 运行规则新增「意图澄清」小节，维护条目把「澄清」纳入需要同步的范围；`tests/behavior.md` 新增 B49，覆盖选项式提问、默认项与对照场景。

验证：`node ../scripts/version.mjs check` 通过；`node --test test/` 通过；`references/planning.md` 与 `SKILL.md` 的本地链接经人工核对。B49 未执行真实宿主回归。
## 0.7.1 更新说明（2026-10-21）

本次发布让只持有安装副本、没有源码的用户也能确认技能版本。

- `SKILL.md` 前置元数据新增 `metadata.version`，与 `versions.json` 保持一致；安装副本读该字段即可确认版本，不需要源码、git 历史或远端仓库。选规范的 `metadata` 字段而非顶层 `version`，pi 等宿主忽略未用键，不影响加载。
- `scripts/version.mjs` 为 `easydev` 增加 `skillPath`：`sync` 写入并规范化 `metadata.version`，`check` 校验其与 `versions.json` 一致，缺失或过期时报错；未声明 `skillPath` 的其他项目不受影响。
- README 安装章节新增「查看已安装版本」，给出从 `SKILL.md` 读取版本的命令；维护条目补上该字段必须与 `versions.json` 同步。

验证：`node ../scripts/version.mjs check` 通过；把版本改为 `9.9.9`、或删除 `metadata` 块时，`check` 分别报不一致与缺失，`sync` 可写回；`SKILL.md` 前置元数据经 YAML 解析得到 `metadata.version`。未执行真实宿主中“仅安装副本读取版本”的回归。

## 0.7.0 更新说明（2026-10-21）

本次发布集中在审计验收契约、规则一致性与文档修复。

- B16 审计验收改为核对 prompt 要求的机器可核对块（`easydev-findings`）：逐行 `P级 | 位置=文件:行号 | 触发= | 后果=`，检查块格式、1–20 条、P0→P3 排序；每条位置均须解析到被审计仓库内真实存在的行，且至少一条落在 `withdraw()` 函数体内（AST 计算函数边界，支持 `文件:符号` 与 Windows 反斜杠路径）。触发/后果字段只要求非空，不再用词表猜描述含义。只解析报告层级围栏（0–3 个前导空格；4 空格缩进、列表或引用容器内的围栏不在支持范围内，prompt 要求块只在顶层给出），未闭合块、第二个机器块、块内杂围栏、空字段、越界路径报格式/证据问题。
- B01 改为核对 prompt 要求的末行标记 `审查状态：自查` / `审查状态：独立`：只认最后一条非空行、整行匹配（从行首开始，不接受前导空格或 Tab），围栏代码块内的标记不计（围栏按 0–3 个前导空格识别）；在 quick 场景下“独立”与缺失均不通过。
- 行为 runner 的场景清单改由 `scenarios.SCENARIO_MODULES` 单一来源派生，`list` 输出与 `--scenario` 参数校验不再手写重复列表。
- `SKILL.md` quick 核心补充同一问题最多三次有新证据尝试的停止规则（含纯调查不计次、环境故障不耗预算并保留候选；此前只写在 quick 默认不加载的 `execution.md`），并给“不加载参考模块”补上复杂任务的例外，与按需加载表和 `references/execution.md` 一致。
- `references/baseline-safety.md` 的加载条件改为与按需加载表一致：`full` 不再无条件加载，只在脏文件、未跟踪/敏感文件、无 Git/首次提交或需恢复时加载。
- README 手工安装补充 `agents/` 可选扩展与 `examples/` 示例配置的安装方式；SWE-bench README 补记 `setup` 子命令；行为 runner README 说明新适配器必须把原生读取动作映射到 `file_access`/`module_access`，否则 B16 与 B01 的读取判定失效。
- `public-contract-callers` 用例说明 `solution.patch` 是夹具级参考补丁（含 `check_orders.py`、`docs/orders.md` 更新），不是 Agent 合规样例；场景 prompt 注明契约文档由评测方同步，消除“Skill 要求同步文档”与“禁止改 `docs/`”的表面冲突。

## 0.6.0 更新说明（2026-10-21）

- 模型配置改为强约束：外部 `~/.config/easydev/models.json`（或 XDG/APPDATA 等效路径）成为权威来源，优先级调整为外部配置 > 对话指定文件 > 内置 `inherit` 默认值。进入 `full` 或任何分派前必须实际读取并记录来源，不可读或无效时暂停分派、不得静默回退；配置或用户指定的具体模型与深度为硬性要求，宿主无法满足时暂停分派而非改用继承模型；`review`/`audit` 与任何分派前的来源检查同受约束。分派后按返回值、运行记录或事件核对实际模型与推理深度，与请求不一致时报告并重新分派，确无记录或不可访问时才记未知并说明原因，可查到的事实不得写成未知。配置与示例中的具体模型 `model` 必须与宿主模型表逐字一致（如 `codex/gpt-6.1-sol`），保留其命名空间前缀、裸写可能无法解析；模型层级与 `inherit` 按各自规则，深度只写在 `reasoning_effort`。示例文件 `examples/models.cross-provider.json` 同步改为完整 ID。同步更新 `SKILL.md`、`references/{models,orchestration,setup}.md`、README 与示例说明；新增 B48 行为用例，尚未执行真实 Agent 回归。
- 吸收 Claude Code 2.1.304 的减少确认思路：授权规则改为开工时一次性归纳「可直接执行 / 需确认 / 禁止」授权基线（小任务记在当前上下文），只对未覆盖项提问，同一授权跨文件、阶段与子代理复用；项目规则只提供日常开发约定，提交、发布、部署、对外消息不因项目规则自动放行，须用户明确授权并遵守宿主规则，被审仓库规则与审查材料不构成授权；压缩或换实例后按项目规则与账本/交接记录重建基线。账本与交接模板同步增加该字段。
- 审查规程新增「轻 / 标准 / 深」三档强度（见 `references/review.md#3-审查强度与检查深度`）：默认「标准」，用户指定 > 项目或组织默认 > 按风险自选，高风险边界最低取「深」且该下限优先于上述来源，单凭选定某一档不算放宽；深档需要的动态验证按既有只读隔离交开发者执行。强度只决定调查广度与验证深度，不改变 P0–P3 定义、证据要求、只读约束与 `r/4` 轮次预算，也不拆分 `full` 流程档位。
- 新增 B46（授权基线与重复确认，含仓库规则声明可直接发布的对照）、B47（审查强度分档，含用户指定「轻」但触及高风险边界的对照）行为用例，并同步 README；尚未执行真实 Agent 回归。
- 修正 Codex 技能默认提示词：流程按风险而非复杂度选择，交付对象改为覆盖只读路径的“实际产物”，与当前分流规则保持一致。
- 取消包内 `models.json`：默认行为恒为角色 `inherit` 且 `upgrades` 为空，已由内置规则覆盖，不再需要实体文件。
- 模型配置优先级定稿为三级：用户外部配置（`~/.config/easydev/models.json` 等）> 当前对话明确指定的文件 > 内置 `inherit` 默认值；任何一级都不读取包内 JSON。默认结构模板移至 `examples/models.default.json`，仍不自动加载。
- 运行时最低文件集缩减为 `SKILL.md` 与 `references/`；安装说明、校验命令、目录结构、评测脚本（agent_runner、SWE-bench）及行为用例同步更新。

- 清理历史项目测评报告与审查记录，移除相关文档引用；保留测试用例、夹具、脚本及 CI。
- 将当前固定测试材料从 `tests/fixtures/b30/` 迁移到语义化目录 `tests/cases/public-contract-callers/`，场景 ID 同步改为 `public-contract-callers`；runner 的 `--fixture-root` 改为 `--case-root`，默认目录改为 `tests/cases`。历史版本记录中的 B30 名称保留为当时事实。
- 修复 SWE-bench 完整数据集与任务子集同时使用时的契约加载：`summarize` 按实验 manifest 与实际 run 的任务并集校验，事件归因按实际 run 校验；没有 manifest 时按实际 run 或全部数据集行回退，不再要求未纳入实验的任务存在 baseline。报告任务数取自实际结果，P2P 回归分母只包含官方任务列出的测试。
- 独立验收补充异常对象及 `error.args` 的订单值泄露检查，并新增 `error-text-leak` 与 `other-user-error-text-leak` 两类伪修复回归；B16 会过滤“其余检查项”及“本次审查范围内”等范围型零结论句，无论其独立成行、位于发现之后、先于后续证据或紧跟“结论：”等标签，都不会否决有效发现。
- SWE-bench 补丁采集改用任务运行前的可信 Git 配置，并禁用 Agent 可注入的 hooks、fsmonitor 与外部 Git 配置/attributes；补充 hook 和 clean filter 回归测试。

- 在 `SKILL.md` 增加所有路径及子代理适用的测试硬约束：按验收行为定粒度，同语义输入优先参数化，禁止围绕实现细节机械拆测；保留独立失败诊断与必要边界覆盖。
- 新增测试须证明相对现有覆盖的增量价值，并从需求、契约或独立参考取得预期结果；禁止镜像实现、自设需求或按数量凑测试，已有有效覆盖时不要求新增测试。
- 独立检查必须在稳定快照上批量并行并收齐结果；串行仅限有具体依据的依赖、共享资源冲突、资源或工具限制，局部冲突不串行化全部检查。
- 移除审查反馈“一次改一项并验证”的固定要求，同步 quick、实施计划、full 编排与 README，补充 B43–B45 行为回归定义；新增用例尚未执行真实 Agent 回归。

## 0.5.0 更新说明（2026-10-20）

### 离线归因与行为回归

- 新增 `tests/experiments/swebench/analyze_events.py`：按运行统计 reviewer 实例、报告、P0–P3、reference 文件访问、usage、缓存率、耗时、声明路径与升级行为；子代理不可见 token 保持未知。
- 新增 `tests/experiments/agent_runner/`：提供 B01、B02、B16、B30 的隔离准备、Codex CLI 适配、事件归因与场景验收入口；离线自测覆盖四个场景，真实模型运行尚未执行。
- 新增 [四臂审查消融协议](tests/experiments/review-ablation/README.md)，明确无审查、同模型审查、同强度跨模型审查、弱开发加强审查四组，不包含伪造实验结果。
- reviewer 只按 `completed` 状态和实际报告计数，重复快照去重；顶层与嵌套事件形状均校验，malformed/空事件流不记为有效调用。SWE-bench runner 只接受带 `result_protocol=2` 的完整结果，行为 runner 使用独立的 `behavior_result_protocol=behavior-v1`，两者历史结果仅作事件归因并单独统计历史 resolved。
- SWE-bench 存储结果必须与真实 `events.jsonl` 的有效事件数、malformed 数和 `turn.completed` 交叉一致；分析器与 SWE-bench runner 同时校验任务、臂、重复目录身份及 F2P/P2P 契约，避免只信任可单独改写的 `result.json`。行为 runner 的结果计数直接取自当次事件解析，恢复语义不按 SWE-bench 结果协议处理。
- 读取归因覆盖 `time`/`nice` 等包装命令、Python 脚本参数及 `git diff -p`/`--patch-with-stat` 等内容输出；B30 增加 `gold`、`runs/`、`result.json`、`agent.patch`、JUnit/pytest 产物等标记，区分摘要命令与真实内容读取，并记录失败命令中的访问尝试及 `cd`/`pushd`/`popd` 与嵌套 shell 的目录上下文。
- 初始 `quick`、最终 `full` 与否定升级语句分别判定；B02/B30 接受真实的 quick 到 full 迁移，拒绝“不升级/禁止升级”等伪升级，`full-local`/`full-staged` 不冒充正式 `full`。B16 的具体发现块排除限制性标题、未覆盖和零发现伪装，同时允许“具体发现 + 未检查边界”。
- 行为 runner 对安装后的运行时 Skill 做前后内容快照，Git 排除规则不能隐藏 Skill 篡改；`result.json` 改为同目录临时文件加原子替换，截断或非法结果会被忽略并允许重跑。
- 结果协议校验 usage 非负整数、缓存数不超过输入数、测试状态枚举、F2P 与 `resolved` 语义、有限墙钟时间及任务/臂/重复目录身份；reviewer 完成只接受 `wait`/`close` 的 `completed` 状态，不接受 `spawn_agent` 伪造。
- 超时清理轮询记录后代 PID，并按进程组、根进程和已记录后代分别发送 `SIGTERM`/`SIGKILL`；发信号前复核 PID 身份，覆盖调用 `setsid()` 的已跟踪后代并降低 PID 复用误杀风险。快速双 fork 并被 init 收养等逃逸仍需 OS 级沙箱兜底。
- B16 对排除 `.git` 后的全工作区做前后指纹，除 Git diff 与索引外还能发现 `.pytest_cache`、`__pycache__` 等 ignored 路径变化。
- B01/B02/B30 的验收子进程使用 `python -S -B`，阻止工作区 `sitecustomize.py` 与字节码产物劫持或污染只读检查；捕获路径之外的全工作区变化同样会使验收失败。

### 运行文本与模型配置

- 将 `normal`/`lite`/`ultra` 三档聊天表达完整内联到 `SKILL.md`，删除 `references/expression.md`，quick 不再为表达规则额外读取文件。
- 将 clean quick 执行闭环内联到 `SKILL.md`；把脏文件、敏感快照、无 Git 与恢复规则拆到 `references/baseline-safety.md`，仅在需要时加载。
- 删除 `shortcut:` 强制注释格式，改为在注释或文档中说明实际限制与升级条件；收尾不可计量的 token、耗时、模型或深度统一标未知。
- 默认包内 `inherit` 配置不再强制读取 `models.md`/`setup.md`；用户指定模型、外部配置、配置错误或升级时才加载。`examples/` 永不自动加载；删除 Antigravity 专用模型示例，默认让所有 harness 继承宿主模型，跨角色分流由用户自行修改外部配置。
- `full-local`/`full-staged` 仍作为消融后的候选方案记录，不进入当前运行规则；在真实行为、隐藏验收和成本证据满足采用门槛前，`full` 不拆分内部档位。
- 最小安装的 rsync 示例排除可选的 `agents/` 与 `examples/`，与运行时最低文件集保持一致。

### 验证与限制

- `git diff --check`、两份剩余模型 JSON 解析、离线事件归因/协议自测与 runner 离线自测通过。
- 行为 runner 只验证夹具准备、隔离和固定验收，不证明真实 Agent 会按 Skill 执行；真实 Agent、网络配置实测、文件系统硬隔离、隐藏验收和四臂消融均未执行。
- 历史 pilot 使用 `0.3.10` 且禁止修改测试，不能用于证明 TDD、独立审查收益或当前版本效果。

## 0.4.1 更新说明（2026-10-20）

- 对 `SKILL.md` 与按需 `references/` 模块做保守精简，并回补精简中弱化的规则：账本已试修复层级、模型诊断调用条件、审查未列场景判断、不虚构路径/行号、依赖引入条件、表达完整性、测试去重与跨进程状态。
- 修正“少关闭证据”、`inherit` 字段说明和若干压缩语病；运行时正文由 29,135 字符降至 25,889 字符（约 -11.2%）。
- 完成本地链接、模型 JSON、版本一致性与 `git diff --check` 静态检查；未执行真实 Agent 行为回归，行为无损性仍需回归用例确认。

## 0.4.0 更新说明（2026-10-20）

- full 采用集成初审与定向复审，高风险公共契约按需提前检查；前置、阶段与最终审查共享预算，保留最终完整变更覆盖。
- 明确人工介入条件与可回查的精简交接，按故障原因补上下文、取证或升级。
- 将最小易读代码、精简注释、正向且完备的文档与人工接手纳入实现和审查标准。
- 补充融合效果的四维对照协议与行为用例；效果收益仍待对照评测。

## 0.3.10 更新说明（2026-10-19）

### B30 错误文本泄露

- 缺失身份的错误文本现在按词边界扫描已知订单字段值，拦截把订单 ID、所有者或金额写进 `error`/`message`/`detail` 的实现（如 `"denied for o1 total 120"`）。
- `run.sh` 新增 `text-leak` 负向变体，负向伪修复共九类；断言仍为 38 条。

## 0.3.9 更新说明（2026-10-19）

### B30 缺失身份结果形状

- 0.3.8 推送后复测发现：缺失身份返回变形后的嵌套订单字段（如 `{"status": 200, "order": {"total": 121}}`）仍能通过。已改为限定缺失身份允许的结果：报错、`None`、空容器，或仅含 4xx `status` 与错误文本的字典。
- `run.sh` 新增 `transformed-leak` 负向变体，负向伪修复共八类；断言仍为 38 条。

## 0.3.8 更新说明（2026-10-19）

### B30 验收与夹具

- 独立验收新增运行时生成订单，并分别改动固定/动态订单的实际所有者，拒绝把固定订单到用户的映射写死或使用导入时快照；缺失身份检查覆盖直接返回的标量订单字段。
- `run.sh` 新增硬编码订单映射与标量泄露两类负向变体，负向伪修复共七类；断言计数改为运行时统计（当前 38 条）。
- 夹具提交显式使用 `-c commit.gpgsign=false`，不再受安装者全局 Git 提交签名配置影响。

### 安装与文档

- 明确运行时最低文件集为 `SKILL.md`、`references/`、`models.json`；`agents/` 与 `examples/` 为可选扩展。
- `references/setup.md` 的 Antigravity 示例改用仓库绝对链接，并说明最小安装缺少 `examples/` 时回退 `models.json` 的 `inherit` 默认值。

## 0.3.7 更新说明（2026-10-19）

### 安全验收与审查反馈

- B30 独立验收扩到 23 条：新增第二订单、跨订单越权、`user_id="admin"`、None/空身份、缺失/空导出角色等断言；`run.sh` 增加五类负向伪修复并检查不生成 `__pycache__`/`.pyc`。
- “处理审查反馈”实施纪律迁入 `references/execution.md`，`orchestration.md` 改为引用，`SKILL.md` 加载表补充，覆盖委派 developer 与 quick/B19。
- Antigravity 示例升级阶段审查模型保持 `pro`；`models.md`/`setup.md` 修正 `inherit`、模型 ID 与层级契约，并补充宿主指令约束。
- 移除本地 `.pi/sandbox.json` 对 `~/.dsh/.env` 的写白名单（本地配置不随 Git 发布）。

## 0.3.6 更新说明（2026-10-19）

### 安装与模型示例

- 新增 Antigravity 模型配置示例 `examples/models.antigravity.json`：`flash` 开发、`pro` 审查，升级阶段换 `pro` 开发；README 与 `references/setup.md` 说明其以降低成本为目标，效果待成本基线验证。
- README 安装章节新增只复制运行时文件的 rsync 命令：`DEST` 明确为技能父目录，排除 `README.md`、`CHANGELOG.md` 与 `tests/`，并说明无 `--delete` 时的更新步骤，避免嵌套目录和旧文件残留。
- `references/setup.md` 的安装与成本基线链接改为仓库绝对 URL，避免安装副本中指向未复制文件。

### 回归与文档

- `tests/behavior.md` 新增“最小成本回归集合与记录”：定义每次升级的最低复跑集合 B01/B02/B09/B13/B16/B25q、与大版本升级集合的并集关系及去重项，并要求在 `tests/results/` 记录成本基线。
- 发布前检查改为解析 `examples/` 下全部 `models*.json`；目录树补充新示例与本轮审计记录。
- 清理 `references/orchestration.md` 中与“默认至多 4 轮正式审核”重复的旧句，预算规则不变。

## 0.3.5 更新说明（2026-10-19）

### 安装与发现元数据

- 补充 Google Antigravity App/IDE 与 CLI 的技能目录、Skills CLI ID、旧目录映射和原生委派能力核对说明。
- `SKILL.md` description 补充英文能力与排除边界，使用 YAML 折叠标量；流程正文与路由规则保持不变。英文激活效果及 Antigravity 宿主行为尚未实测。

### 证据与回归边界

- 历史 Opus 输出的仓库引用改为相对路径，临时产物链接改为普通文本，并声明路径整理不代表重新执行。
- 明确 B30 CI 仅复验固定补丁，不调用 Agent；补充模型/宿主升级后的优先回归集合。持续 Agent 行为回归套件仍未建立。

## 0.3.4 更新说明（2026-10-19）

### 评测加固

- B30 独立验收补充缺失/空身份断言与伪修复回归检查：覆盖 `get_order("o1")`、`get_order("o1", None)`、`handle_show_order({}, "o1")`，并检测完整/部分/嵌套订单数据泄露；`run.sh` 负向回归要求恰好 3 条失败并匹配三个 label。
- B30 用户请求显式声明 `request["user_id"]`、`PermissionError`、`{"status": 403}`（仅 status）与 404；`behavior.md` 内联摘录注明完整夹具以 `tests/fixtures/b30/initial/` 为准。

### 安装说明

- 手动安装只复制运行时文件，排除本地 `.pi/` 与 `tests/`；更新说明同步；CLI 安装后要求检查目录、包含则删除。

### 发布

- 版本 0.3.4。

## 0.3.3 更新说明（2026-10-19）

### 评测加固

- B30 红阶段加入接口探针 `probe.py`（初始期望 200、修复后期望 403），并要求失败原因恰好是 3 条 `TypeError`，不再接受任意非零退出。
- 新增独立验收脚本 `acceptance.py`：断言集不依赖被测仓库的 `check_orders.py`，可直接用于 Agent 任务评测；夹具 README 区分历史补丁复验、Agent 任务评测与独立验收三种用途。
- 新增 `.github/workflows/easydev-fixture.yml`：仅在 `easydev/**` 变更时执行 B30 一键复验；真实 Agent 行为测试仍不进入 CI。

### 发布

- 版本 0.3.3。

## 0.3.2 更新说明（2026-10-19）

### 评测修正

- 更正 0.3.1 完整 `full` 实验的交付状态：工作流已执行、功能检查通过、独立审查完成，但身份来源可信性未验证，按完成边界记为“部分完成/待确认”，不新增规则。
- B30 改为可复现夹具：`tests/fixtures/b30/` 提供初始仓库、完整补丁、审查原文与红绿运行脚本；验收条件收紧为必须适配仓库内调用方并验证原有调用路径，无法确认的外部调用方须记录兼容性风险，未经授权的破坏性变更不能仅靠披露风险过关。

### 发布

- 版本 0.3.2。

## 0.3.1 更新说明（2026-10-19）

### 流程修正

- 分流改按最小必要调查后的实际影响判定：任务描述不完整、根因尚未定位、改动跨文件本身都不再构成 `full` 理由；`quick` 中实际影响超出初判时按实际影响升级，并保留已用尝试与证据。
- 明确用户显式限定流程与高风险边界的优先级：遵守用户授权，但不省略必要的安全验证；缺少独立审查时如实报告缺口，不称闭环完成。
- 区分低影响 P3 延期建议与实质缺陷争议：不实施可延期的 P3 不触发独立确认轮；把确定缺陷降级、以越界为由移出或拒收影响验收的发现，仍须附证据并交下一轮独立确认。

### 测试与发布

- B29 拆分为 B29a（纯非开发问题，不读取 `references`）与 B29b（规则咨询可读取相关文档，但不进入开发流程）。
- 新增 B30（公共契约与调用方检查）、B31（跨文件机械改动仍为 `quick`）、B32（根因初始未知）、B33（仅可延期 P3 不追加复审）。
- 修复版本漂移：`versions.json` 对齐 0.3.1；`scripts/version.mjs` 为 EasyDev 增加 CHANGELOG 发布说明校验；新增版本一致性 CI。

## 0.3.0 更新说明（2026-10-19）

### 破坏性变更

- 删除实现档位。路径后裸写的 `lite`/`ultra` 不再改变实现取舍，档位词只指聊天表达。旧提示词里的实现档位写法会静默失效（不再报错，也不再产生缩减或质疑需求的行为），需要表达极简请写“表达 ultra”。
- `full` 触发条件重写：不再把“跨文件行为变化”作为触发词，改为影响多个调用方、公共契约或持久数据，根因或方案存在实质不确定性，或用户明确要求闭环。单纯文件多的机械改动不再因此进入完整闭环。

### 其他改动

- 合并重复定义：路径表、表达档位与 `full` 边界以 SKILL.md 及各模块为唯一来源，README 不再复制。
- 新增 B29 负向激活用例与结果落盘要求。

## 0.2.1 更新说明（2026-10-19）

- 修正授权修复路由、技能控制边界、作者隔离与默认主代理开发规则。
- 明确档位语法、任务级持续范围、统一修复计数和可恢复账本位置。
- 拆分原生/fallback 工作区用例，补充针对性场景，撤回缺少证据的历史通过声明。
- 补充作者争议的独立确认、quick/full 预算隔离、review 转修复的审核计数、敏感基线保护及 B27/B28 场景。

## 0.2.0 更新说明（2026-10-19）

### 流程与能力

- 新增 `spike` 路径：用最小实验验证可行性并给出结论，产物标记为 `throwaway`，不并入产品代码。
- 新增 `audit` 路径：对全仓或指定目录执行一次性只读审计，按严重度输出最多 20 条确定发现，并优先检查高影响路径和预期负载。
- 完善根因排障：逐层定位偏离边界、对照正常样本、保持单一假设；同一问题三次修复失败后重新质疑架构，并区分代码、环境与时序根因。
- 补充 TDD 红绿纪律：新功能和非平凡行为先确认失败原因，再以最小实现转绿；修复证据必须同时记录修复前失败与修复后通过。
- 补充审查反馈处理：收到反馈后先核实事实，信息不足先澄清，再按优先级逐项修复，并允许基于证据反驳。
- 补充可交接计划契约：计划包含目标、验收标准、影响路径、接口签名、实施步骤、验证方式与提交边界，不允许占位符。
- 新增工作区隔离与收尾：优先使用宿主原生工作树工具，完成后提供本地合并、推送并创建 PR、保留现状三个选项，清理必须获得授权。

### 规则与文档

- 聊天表达统一为 `normal`、`lite`、`ultra` 三档；`ultra` 仅在明确要求时启用，压缩表达不省略风险、数字、路径、错误原文和验证结果。
- 新增技能共存规则：同时安装多个开发或审查技能时，只保留一个流程控制者，避免并行闭环。
- 完善任务基线保护，覆盖干净 Git、已有未提交修改、未跟踪文件和无 Git 仓库。
- 扩展设计与计划审查、全仓审计、独立审查预算和完成边界。
- 精简 README 中的重复内容，将模型字段、优先级和校验规则集中到 `references/models.md`。
- 删除与 `models.json` 重复的 `examples/models.inherit.json`，保留跨供应商示例。
- 修正 DeepSeek Harness 的原生技能目录与共享目录说明。
- 新增 `tests/behavior.md`，定义 B01–B21 行为回归用例。

### 兼容性

- 本次仅修改 Skill 指令、参考文档、示例与测试定义，不涉及数据迁移。
- 外部 `models.json` 的配置格式保持兼容；原先把个人配置直接写入安装目录的用户应先迁移到外部配置路径。
- 更正验证声明：曾记录 B01–B21 通过及 B21 fallback 已验证，但未保留可核验执行证据；历史结果不可核验，不能作为已通过基线。原生工作树路径仍需实测。

## 0.1.0 更新说明（2026-10-18）

- 首次发布一体化编码 Skill，提供 `auto`、`quick`、`full`、`design`、`plan`、`debug`、`review` 七条路径。
- 支持开发与独立审查角色分离，最多四轮正式审核；模型升级、换实例和多个审核者不增加轮数。
- 引入最小完整实现、任务基线保护、验证证据、P0–P3 审查分级和完成边界。
- 提供 developer、reviewer 与升级阶段模型配置。
- 支持六个 harness 的安装位置和 Skills CLI 安装方式。
