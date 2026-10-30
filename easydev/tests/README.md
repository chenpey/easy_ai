# 维护材料总览

`tests/` 只用于维护 EasyDev 本身，不是普通编码任务的运行依赖。这里的检查都不启动真实模型：外部边界（Codex CLI、pytest 子进程、网络、时钟）由 `tests/experiments/stubs/` 中的替身替代，断言落在 CLI 的退出码、stdout/stderr 和产物文件上。

## 运行

在本目录的上级（`easydev/`）执行：

```bash
bash tests/cases/public-contract-callers/run.sh
bash tests/cases/version-gate/run.sh
bash tests/cases/design-gate/run.sh
bash tests/cases/audit-overflow/run.sh
python3 -m unittest discover -s tests/experiments -p 'test_role_metric.py' -v
python3 -m unittest discover -s tests/experiments/agent_runner -p 'test_*.py' -v
python3 -m unittest discover -s tests/experiments/swebench -p 'test_*.py' -v
python3 -m unittest discover -s tests/hooks -p 'test_*.py' -v
```

八项互不依赖，在同一冻结快照上批量并发并分别保存日志；CI 另将仓库门禁与安装冒烟同这八项共十个检查拆为 fail-fast=false 的矩阵任务。只有固定补丁复验内部须串行，按“红 → 参考补丁 → 绿 → 独立验收 → 伪修复拒绝”，因为后一步依赖前一步的仓库状态。本机全量耗时目标 < 120s。

`agent_runner` 套件里的 `rg --files` 正例需要本机装有 `ripgrep`（不在 POSIX 必备工具内）：装有 `rg` 时断言原工具真的执行并列出工作区文件，缺失时只校验守卫的放行判定、不宣称原工具已执行；CI 的行为 runner 作业先用 `apt-get` 安装 ripgrep，真实执行分支由 CI 覆盖。

## 覆盖什么

- **固定补丁复验** (`tests/cases/public-contract-callers/`)：初始仓库的红测、参考补丁后的 403 与契约检查、独立验收，以及 11 类伪修复被拒绝；红绿两段仍运行夹具自带的 `check_orders.py`，独立验收 `acceptance.py` 不依赖它。
- **行为 runner CLI** (`tests/experiments/agent_runner/test_cli.py`)：`list` / `prepare` / `run` 的退出码与产物；B01 路由/状态标记/访问/ignored 文件/Git 配置负例、运行时随机完成标记（每次验收由环境变量传入，退出码 0 且 stdout 精确匹配才通过；伪造 `print("ok")` 后提前退出、只打印标记、直接 `raise SystemExit(0)`、多余 stdout 与 `PYTHONOPTIMIZE=1` 下文案未改均失败，正常改动及单引号/无空格变体通过）、运行时 Skill 篡改、普通/回退暂存与 assume-unchanged、缺 `turn.completed`、畸形事件、非零退出码、超时后进程组清理、捕获不执行 hook/filter；相对路径与认证链接、B02 高风险 full、quick/候选档位、未完成 reviewer、所有权校验及拒绝先读后鉴权、`sitecustomize` 不执行；B16 合格发现块与围栏/位置/排序/数量/读取负例；b67 与 b67-review 覆盖代码质量判据的实施与审查口径——写盘失败必须传到调用方、生成文件不手改、修复回退后回归测试必须失败，以及代价型 P3 必报、纯风格不报；public-contract-callers 的独立验收、保留文件与 benchmark 元数据负例。
- **SWE-bench runner CLI** (`tests/experiments/swebench/test_cli.py`)：`setup` / `run` / `summarize` / `calibrate`；结果读取器对身份、事件计数、协议版本与 F2P/P2P 矛盾的拒绝；补丁应用失败（agent patch 与 test patch）的终态存储、`patch_apply_failures` 计数与重跑不调用模型；manifest 选择与报告计数、每任务 eligible/excluded P2P 披露（含 `|` 的 nodeid 在表格单元格内转义）；模板解包逃逸与合法链接/延迟目录属性；保护模式的收紧权限、损坏/软链记录、软链在 chmod 前被拒绝、父子顺序、锁定中途失败回滚、失败结束与换 cwd 恢复；补丁采集不执行 Agent hook/filter；P2P 回归与每 arm 报告。
- **事件归因 CLI** (`tests/experiments/swebench/test_analyze_cli.py`)：声明路径（含 normal 与升降级；中文「采用/分流/路径」与英文 `normal path`、`moved to the full route`，裸 `full path` 不误判、英文否定不误判）、真正 reviewer 交接与开发者提及/否定/禁读的区分、reviewer 完成与报告、P0–P3（发现须自成一行并以严重度起头，交接里复述模板不算发现）、文件/模块访问、usage/缓存率/耗时、零发现声明、畸形行容错、`receiver_thread_ids: null` 按缺失处理、`--output` 与 stdout 一致。
- **流程状态机与独立设计环节的确定性复验** (`tests/cases/design-gate/run.sh`)：normal 一次不同实例审查、无审查/作者自查不可完成、首轮无缺陷不得发起复审、第三次审查被拒；首轮仅局部 P2 时一次局部修复加定向复审可交付（审查 2 次），复审仍未关闭转 full 且记 `r=2/5`，非局部 P2 首轮后转 full 且继承 `r=1/5`；门控五项判定 13 组 fixture（含「只交给独立审查者不构成多阶段」与「高风险但未改变行为/决策/数据流不启用」两个可达成性回归）；`d/3` 与 `r/5` 记账与上限；候选第二轮须用户 opt-in；升版重写不计轮；受阻轮不算缺陷已关闭；剩余 `r` 不足两轮禁止单开设计重审；阶段增量审查最多 3 轮；施工期 `d` 用尽停止；多方案任务六种组合的可达性枚举；文档契约 21 条（含局部 P2 判定条件、定向复审的直接调用方范围、「现成检查」口径、高风险边界实际影响判定、计量字段键序与角色分段），按小节作用域求值并支持禁用措辞；另有 4 组机制自检保证“句子搬到邻节”“旧新措辞并存”会被判负。
- **可选 Hook Plugin** (`tests/hooks/`)：四宿主 envelope 的协议/激活（显式调用、机器上下文、成功读取 `SKILL.md`、未关联 reviewer 仅 role_only、可信父交接、SessionStart 不注入）、P1 Git 包装/多段/`-C` 与基线任务根（默认上下文与生产一致：不配 `task_root`，走自动快照；手工任务根用例用 `use_manual_baseline()` 显式 opt-in 后仍断言旧 epoch 复用拒绝、权限/逐层软链只读核对、patch 多文件）、reviewer 只读、P2 models 来源/重复键/升级相邻差异/局部覆盖与 reviewer 交接/嵌套、P3 共享写序列/checkpoint 取 max/双审查逆序/文档排除/提醒预算；父子绑定按可信分派固定 epoch（`test_epoch.py`：A 分派→切 B→旧 child 初始化/写/完成不污染 B、`active=false` 退役、同 actor ID 不同 session 隔离）、内部预算/非普通文件/锁忙降级（`test_hardening.py`）、完成事件能力边界（Completion：Codex 从生成配置与 `SubagentStart`/`SubagentStop` 序列进入，pi/Antigravity/DSH 断言不续跑）；r3 修复回归（`test_r3_fixes.py`：官方 Codex 字段经生成 command 的完成链、旧 child 重投 start 不改绑、task_root 祖先软链拒绝与安全根索引、缺成功 post 后重新分类、shell 内 `cd` 写目标、锁忙保留危险 Git deny、AG 同一 transcript 不重开 epoch、锁内绑定复核、FIFO 非阻塞预算、失败子终态不计完成、父子同名 tool ID 双计、task_root 与 repo 重叠仍计入构建文件、transcript 成功读取 `SKILL.md` 激活与降级）；r4 修复回归（`test_r4_fixes.py`：旧 child 两次重投 start 不复活、状态 JSON 损坏仍保留危险 Git deny、无命令名重定向/相对 `env -C`/目录目的 `cp`、业务软链指向另一 Git 仓库、200 行后新任务与普通跟进去重、跨仓库同名目标配对、同分派 ID 改内容整段失效、exec 非零退出码与 `is_error` 冲突、原生 `session_meta.id`、transcript 每条读取的 cwd、probe 锁忙只观察、未激活 child 放行、误标 `doc_paths` 的运行配置、分派查询不占 task 锁、夹具 HOME/XDG/Git 全局配置隔离）；r5 修复回归（`test_r5_fixes.py`：同分派 ID 跨 epoch 复用不复活旧 child、数字文件名重定向与 fd 复制区分、`cd . > file` 同段重定向按执行前 cwd、`mv` 的 `-t/-T` 源删除受基线保护、历史 SKILL 读取保留只读角色、transcript 原生身份先于两条激活路径、transcript 机器上下文停用、input 取证在锁外且签名变化丢弃、安装备份/写入前逐层核验目标与父链）；r6 修复回归（`test_r6_fixes.py`：`>&word` 按完整词边界解析，`>&1.txt`/`>&2log`/`>&-file` 是文件目标（开发与只读会话都拒绝），`>&1`/`2>&1`/`>&-` 仍是 fd 复制；显式停用后的历史 SKILL 读取不再随普通跟进重新激活，普通工具继续放行、旧 child 不推进写序列，恢复只认当前回合的新激活证据并开新 epoch）、r7 修复回归（`test_r7_fixes.py`：反斜杠 + 真实换行的行续接在单引号外（含双引号内）于词法阶段消除——`>&1\`+换行+`.txt`、`>& "1\`+换行+`.txt`、`>& 1\`+换行+`.txt` 与 `> 1\`+换行+`.txt` 等普通重定向都按真实目标 `1.txt` 拒绝，`git reset --ha\`+换行+`rd` 与 `g\`+换行+`it reset --hard` 同样拒绝且真实 `1.txt` 字节不变（仅 allow 才执行的临时 Bash 负例不执行），`>&1`/`2>&1`/`>&-` 与 `> 1`/`>> 1`/`2> 1` 对照不变；停用后直接用 read 工具成功读取精确 `SKILL.md` 按新 epoch 恢复（root.active=true、epoch=2、actor task_active，旧 origins/last_success/reviews/checkpoint 不复用），旧任务结果重新写入需当前任务基线、旧 child 不推进、新的标准交接 child 进入 epoch2，失败/requested 读取、其他 Skill 路径不恢复，首次读取仍为 epoch1）、r8 修复回归（`test_r8_fixes.py`：单引号外的反斜杠 + 真实换行在**运算符组合判定之前**整体消除——`>`+`\`+换行+`>1.txt`（实为 `>>1.txt`）、`>&1.txt`、`>|1.txt` 与 `2>`/`&>` 跨续行形式都按真实目标 `1.txt` 拒绝且点名 `baseline/1.txt`，真实 `1.txt` 字节不变；单引号内保持字面，`>&1`/`2>&1`/`>&-`、`> 1`/`>> 1`/`2> 1` 与危险 Git 续行对照不变；停用后重投停用前已消费的同一成功读取（同 actor、同 tool ID、同参数、同结果）保持 inactive/epoch1，普通 Git 与写放行、旧 child 不推进，只有新 tool ID 的真实成功读取恢复 epoch2，且消费证据按 actor/session 隔离、跨 epoch 保留在 actor 索引而非任务缓存）、r9 修复回归（`test_r9_fixes.py`：行续接预处理与注释/引号/词边界共用一份词法状态——`#` 只在词首开始注释、注释内的引号与反斜杠无语法作用、注释止于物理换行，`# don't overwrite` 之后的 `>`+`\`+换行+`&1.txt`/`>|1.txt` 按真实目标 `1.txt` 点名 `baseline/1.txt` 拒绝、`git reset --ha\`+换行+`rd` 与 `g\`+换行+`it reset --hard` 同样拒绝且原工具不执行，真实 `1.txt` 字节不变，普通注释/单引号字面/fd 复制对照不变；同一 tool ID 的成功读取无论摘要是否变化（路径等价拼写、额外参数、`success`↔`completed`）都不再作为新证据恢复，只把该调用观测标为失效；`consumed_read_ids` 跨 epoch 保存停用前见过的调用 ID 且不做淘汰，超过 100 项的近期摘要索引裁剪后被淘汰的旧 ID 重投仍保持 inactive/epoch1，新 ID 的成功读取仍恢复 epoch2/epoch3，actor/session 隔离不变）、安装生成、清单驱动的合并/冲突/卸载、manifest 文件集与源分发目录一致、manifest 分发件的独立启动与 pi Node 夹具（含 stderr 降级告警：每次调用至多一条汇总、只披露 12 类固定类别、未知文案回退到“细节已隐藏”兜底、不回显路径或 sentinel；类别 catalog 逐条喂真实 core 文案，覆盖 probe 直写、normalize 多工作区、run 的输入/协议/内部异常、write_targets gap、终态全部 `CHILD_FAILURE_STATUSES` 取值与多类同时命中的顺序去重，并固定“无合法诊断边界、正文只出现类别关键词”走隐藏兜底）、Antigravity 事件结构安装（`test_install.py`：`easydev-hooks` 组、工具事件 Grouped/非工具事件 Flat、逐事件 `--event`、`apply`/`uninstall` 只动本组、用户改过的 Flat 条目保留、`probe→enforce` 不追加两套）、官方 Antigravity 载荷（`test_protocol.py`：仅有 `toolCall` + 整型 `stepIdx`、无 `id`/`status` 的 `PostToolUse` 能直读激活；dirty 首写自动快照 + 成功 post 建立 `last_success`/`write_seq`；二次写命中成功指纹；带 `error` 的 post 不建立成功；换步号不配对；`Stop` 只披露 P3 缺口不续跑）、带 transcript 的未激活短路（`test_short_circuit.py`：Codex/Pi 首回合带 transcript 后锚点落盘、后续 pre/post 零 Git/零锁/无告警、停用后仍 inactive 且危险 Git 放行；Antigravity `PreInvocation #1` → 同回合 `SKILL.md` 读取 → `#2` 激活，已激活时仍按记录键去重）、Antigravity 真实形状 transcript 归因（`test_transcripts.py`：无 `id`、参数带外层引号、`PLANNER_RESPONSE` 后连续的 1:N `GENERIC` 结果行；`run_command` 只认元数据头里的退出码（头部 code 1 + 正文 code 0 不确认、头部 code 0 + 正文 code 1 仍确认、截断预览前言里的退出码照认），读取类工具要求非空 `content` + 结果头里整行匹配的带行号范围读取结果行（`Showing lines <int>-<int> of <int>`/`Showing lines <int> to <int>`/`Partial read (lines <int>-<int> of <int>). Showing bytes <int>-<int> of <int> in this line range, truncated at the <int>-byte limit.` 及其两种已知续读指引（`ByteOffset=<int>` / `larger byte budget`））或完整措辞的真实截断通知（`The tool's output was truncated due to being too long. …`）（缺/空/非字符串 content、只有通用时间戳、时间戳加空白、正文里才提到 `Showing lines`/`was truncated`、首段说明文字恰好以 `Showing lines`/`The tool's output was truncated`/`Here is a preview` 开头、数字后粘连文本与数字格式示例句、`Partial read` 点号后的任意其它尾句或缺少字节范围/上限说明、头部失败标记都不确认，有效空文件头、真实读取头、长正文含 error 词、截断提示仍确认，旧版 `view_file` 的 `File Path`/`Total Lines`/`Total Bytes` 头也认），合成 ID 稳定）、未激活 O(1) 短路（`test_short_circuit.py`：无 Git 子进程/无锁/无告警、四宿主放行形状、停用后重新短路、`SKILL.md` post 仍激活、未激活 child 成功读取精确 `SKILL.md` 后只做本 actor 的局部激活并拒绝嵌套分派（失败/结果未知/其他路径不激活、普通 post 与根会话仍零 Git 短路，含 Pi 只带 `PI_SUBAGENT_CHILD=1` 的真实 CLI）；局部激活写入 actor 级读取消费证据，显式停用后重投同 actor/同调用 ID 的成功读取（同参数或改参数/改 `success`↔`completed` 拼写）仍 inactive/嵌套放行，新调用 ID 才重新激活（三宿主，含 Pi 真实 CLI）；默认配置（无显式 `state_root`）下四宿主真实 CLI 的“A 激活→B 普通调用→A 危险 Git/reviewer 只读仍 deny”、停用后零 Git 稳态、不写全局会话索引、无 cwd 退回完整判定、分区状态不可读 fail-open 告警）、自动快照（`test_baseline_auto.py`：dirty/untracked 首次内容落私有状态根 0600/0700、第二次不覆盖、多文件 patch、软链/FIFO/超限/不可读拒绝、成功后被外部改动仍拒绝、手工 `task_root` 兼容、连续超限/非普通文件拒绝不泄漏源文件 FD）；写序列记账（`test_write_accounting.py`：非写工具/纯读 shell/role_only 静默，同目标多写去重，软链与不安全指纹不被合并丢弃，预取变化 pre 拒绝、超时降级，解释器/herestring/`-c`/管道/fd/脚本文件壳的未解析 stdin 仍披露缺口）。解析器级用例另锁 `&&`/`||`/`|` 后接换行的续接（危险 Git 仍进 `analysis.git`，悬空 `true &&` 仍披露不完整）、“保留字只认裸写”（`'esac'`/`'fi'`/`\fi` 是普通词，不再误判结束符或结构关键字）、词首续行后的行首注释、`!` 交换成功/失败出口、管道比 `&&`/`||` 绑得更紧且多段按源码顺序记录、`&` 异步组不外泄 cwd、长链迭代（2000+ 段不 RecursionError）、argv 里的裸花括号与 `sort` 短选项集群；heredoc 用例组（`test_shell.py`、`test_heredoc_warnings.py`）锁住词法边界与消费者判定：引用/未引用 delimiter、`<<-` 只去前导 tab、定界符精确匹配、多个正文按源码顺序、未闭合或动态 delimiter 与嵌套替换整次回退并保留旧 Git 与写证据、纯数据白名单与解释器 stdin、壳与组尾/fd/管道/未知消费者保守按脚本并标 `stdin_program`；core 级另验解释器 stdin 不触发假 Git 拒绝、`sh` 正文 reset 与 reviewer touch 仍 deny（`git_destructive`/`reviewer_command_write`）、引用写目标完整进自动快照，以及五类基线结构告警 pre-only（含缺 pre、post-only、pre/post 参数不同三种信息损失的逐格断言）；共用分析器的 `stdin_program` 由部署副本的读取守卫一律 `target_unresolved` 拒绝，相应 helper 返回非只读/未知（副本取自实际安装的 shell.py）；unified diff 的 hunk 按 `@@ -n,m +x,y @@` 计数消费正文、只按补丁自身的 LF 分行（U+2028/垂直制表符留在正文里），`--- ` 样子的删除行不再被当文件头；回放守卫的部署副本用例覆盖 `find -exec/-execdir/-ok` 的不可证明读、条件 cd 导致的未知 cwd、替换内 `cd <绝对目录>` 的根内/根外对照，runner 另覆盖“守卫已调用但健康日志被删 → 样本无效”；协议与规则用例在进程内调用 `core.run()`，模板 Git 仓库每进程只建一次并逐例 `copytree`（本机 hooks 套件 1094 项约 129s）；CLI 入口、安装器、分发启动与 Node 适配器仍用子进程，断言落在 core 的 stdin/stdout/stderr、Node 适配器返回值与临时文件变化。
- **回放读取守卫** (`tests/experiments/agent_runner/test_replay_read_guard.py`)：两个 canonical 允许根内放行；HOME 外 operator-config、sibling 前缀、软链、`..`、显式 `/etc`/`/usr` 读取拒绝；`rg --follow`/`find -L`/`ls -L` 固定拒绝；无链普通递归与含外链树的区别；入口预算/内部截止/畸形输入/缺 policy/结构化无路径均为 deny + 健康失败；生成 hooks.json 的绝对命令与 `.git/info/exclude`。
- **按角色计量** (`tests/experiments/test_role_metric.py`)：主代理与子代理分别归因；`--path` 过滤回答「哪个角色读了某个文件」；子代理只用无路径参数的外部委派工具时，stderr 给一行提示而不是默认「没读」；`--json` 可机器读取。
- **normal 微闭环观测**（`tests/experiments/agent_runner/test_cli.py` 的 `b61` 用例）：按 `tests/behavior.md` 的 B61 三支判分——首轮无必关项或仅局部 P2 时终态 normal、独立审查 1–2 次、必须带回报告；首轮报 P0/P1、非局部 P2 或验收缺口时允许转 full，但必须有初始档位声明、起跳于 normal、升级前已有一份完成的审查报告、引用一条归因解析出的结构化缺陷，且 `r≤5`/`d≤3`；`details.normal_micro_loop` 区分「交付」与「按规升级」。另固定第三次审查、无声明、无报告即升级、无证据升级、超预算升级、quick 起跳、normal 内闭环 P0/P1、错误行为各自被拒，`不升级到 full` 与「不称 full 闭环」这类否定复述不被误判；复审是否读到调用方只记 detail。任务文本钉死 `float` 数值域与折扣方向（`discount` 为减免比例），两处都是实测逼出来的：不钉时升级链没有收口点，或两次回放各选一种读法导致样本报废。
- **档位对照臂** (`tests/experiments/swebench/test_arms.py`)：三个 `easydev-*` 档位臂的 prompt 各自钉住档位且互不相同、auto 臂 prompt 不变、默认臂集合不变、未知臂在读取实验资产前被拒。
- **audit 溢出与分片覆盖** (`tests/cases/audit-overflow/run.sh`)：固定 21 个 P1 与 2 个 P2，正文仅取 20 条，附录保留 23 条完整记录；摘要披露正文外的 1 个 P1；正文超限、附录遗漏、溢出摘要缺失、虚假全覆盖或未列未审分片均被拒绝。
- **版本提取门禁** (`tests/cases/version-gate/run.sh`)：`scripts/skill_version.py` 的九组 fixture（合法、`description` 内含 `version:` 但 `metadata.version` 合法、重复 `version` 键、`version` 错位到其他映射、只在 `description` 里出现 `version`、带引号的重复 `version` 键、双引号转义后的重复 `version` 键、值内嵌套引号、未闭合引号）；有 PyYAML 与无 PyYAML 两个分支都要通过，后者用临时 `yaml` 垫片固定。降级模式明确拒绝双引号键中的反斜杠转义，PyYAML 分支按解码后的重复键拒绝该 fixture。

## 未覆盖什么

- `analyze_events` 只读 `result.json` 的 usage，事件流 `turn.completed` 的 usage 不聚合，token 0 无法区分“未知”与真实零；这是当前实现的已知限制，测试只固定行为，不宣称“缺失即未知”（见 `tests/experiments/swebench/README.md`）。
- 保护模式的 P2 边缘未逐项迁移：相对 `--protect-paths` 的记录内容、指向实验目录的软链别名、记录落点包含判断（均由 `run.py` 现有实现约束，但当前没有 CLI 回归输入）。
- PID 身份复用、`setsid()` 脱离会话或忽略 SIGTERM 的后代清理、双 fork 被 init 收养等超时清理边界；普通子进程与默认 SIGTERM 响应已覆盖。

- heredoc 支持是有限子集：只处理能证明正文边界的 Bash/POSIX 风格形态。嵌套执行 heredoc、任意 `${...}`（含简单 `${NAME}`）、`$((...))`、复杂或嵌套反引号、未引用正文里的反斜杠续行与跨行 `$()` 交互都整次回退到旧词法路径，回退仍沿用旧解析器的误拦/漏检，不构成沙箱；`<<-` 只去前导 tab，delimiter 不做宽松 trim，CRLF 与 Windows/PowerShell 未覆盖。
- 诊断类别只匹配当前 core 的固定文案，`warning: …` 是无转义文本协议：恶意 stderr 可伪造分隔符让汇总多命中类别；当前生产者值域有界（身份描述只有会话/线程/actor，终态只取固定失败集合），`test_install.py` 的 `CoreDiagnosticProducerTests` 用真实 core 逐值固定生产者值域，与伪造分隔符的替身多命中用例分开。类别精确化也不找回 core 前三条规定之外的诊断。
- 未在真实宿主 probe：四宿主 Hook 信任、真实 stdin 结构与输出消费、Stop 续跑只有离线夹具；pi 侧也只经 Node 夹具（假 `ExtensionAPI` + 真实 `child_process` argv）验证，不证明宿主已加载扩展或端到端拦截生效。
- 真实 Codex CLI 与真实模型行为：所有检查都使用替身，只验证验收机制本身，不验证模型是否遵守 EasyDev 流程，也不产生可引用的模型成绩。
- `role_metric.py` 的 `--path` 是子串匹配：`src/cart.py` 也会命中 `legacy/src/cart.py`，只用于同一仓库内的相对比较。
- `role_metric.py` 只统计工具参数里出现的路径与 usage，不判断审查质量，也不证明报告里的计量诚实；Codex 宿主没有这套 artifact 时不可用。
- `role_metric.py` 看不见外部委派里的读取：经 `antigravity` 等工具执行时，参数里只有 `{tool,input}`，`--path` 为空不代表没读。本仓库 6 个历史会话的 23 次 reviewer 运行中有 4 次属此类（工具仅为 `antigravity`）。现在会给一行提示，但无法还原读了什么。
- `role_metric.py` 的子代理产物按**父会话目录**归属：用子代理自己的 cwd 当 `--session-dir` 找不到会话文件，需传主会话目录。
- `b61` 的「复审是否读了直接调用方」只记 detail：事件层无法把文件读取稳定归属到 reviewer 线程，它提供真实回放的观测值，不构成该规则的回归保护。
- `b61` 的升级证据只认事件归因解构出的 P0/P1/P2 发现块：「缺陷是否真的落在需求契约内」是语义判断，未自动化，`details.escalation_evidence` 只记录引用了什么；任务文本里的 `float` 数值域是 fixture 限制，不代表真实任务都有界。
- 真实 pytest / SWE-bench 依赖环境：`pytest_stub.py` 只按计划输出状态与 JUnit XML，不执行测试。
- 网络隔离、容器级进程隔离：runner 只请求 Codex CLI 关闭网络；超时清理覆盖进程组与已记录后代，仍不能替代 cgroup、容器或宿主 supervisor。
- Linux CI 上尚未在本机验证的差异（如 `/proc` 进程枚举）由 CI 运行结果补充；本机验证记录只代表 macOS。

## 目录

```text
tests/
├── README.md
├── behavior.md
├── audit-0.12.1.md
├── hooks/
│   ├── support.py
│   ├── fixtures/
│   │   └── pi_adapter_fixture.mjs
│   ├── test_protocol.py
│   ├── test_safety.py
│   ├── test_dispatch.py
│   ├── test_epoch.py
│   ├── test_hardening.py
│   ├── test_settle.py
│   ├── test_transcripts.py
│   ├── test_install.py
│   ├── test_r3_fixes.py
│   ├── test_r4_fixes.py
│   ├── test_r5_fixes.py
│   ├── test_r6_fixes.py
│   ├── test_r7_fixes.py
│   ├── test_r8_fixes.py
│   ├── test_r9_fixes.py
│   └── test_regressions.py
├── cases/
│   ├── README.md
│   ├── version-gate/
│   │   └── run.sh
│   ├── design-gate/
│   │   ├── README.md
│   │   ├── gate_model.py
│   │   ├── run.sh
│   │   └── verify.py
│   ├── audit-overflow/
│   │   ├── README.md
│   │   ├── audit_model.py
│   │   ├── run.sh
│   │   └── verify.py
│   └── public-contract-callers/
│       ├── .gitattributes
│       ├── README.md
│       ├── acceptance.py
│       ├── initial/
│       │   ├── app/{__init__,admin,api,db,orders}.py
│       │   ├── check_orders.py
│       │   ├── docs/orders.md
│       │   └── probe.py
│       ├── run.sh
│       ├── solution.patch
│       └── verify.py
└── experiments/
    ├── README.md
    ├── process_control.py
    ├── role_metric.py
    ├── test_role_metric.py
    ├── b67-replay/
    │   └── 2026-10-27/
    │       └── README.md
    ├── behavior-replay/
    │   └── 2026-10-27/
    │       ├── README.md
    │       ├── evaluation.json
    │       ├── patches/
    │       └── reports/
    ├── review-ablation/
    │   └── README.md
    ├── routing-replay/
    │   └── README.md
    ├── stubs/
    │   ├── codex_stub.py
    │   └── pytest_stub.py
    ├── agent_runner/
    │   ├── README.md
    │   ├── runner.py
    │   ├── replay_read_guard.py
    │   ├── adapters/{__init__,codex_cli}.py
    │   ├── scenarios/{__init__,common,b01,b02,b16,b61,b67,b67_review,public_contract_callers}.py
    │   ├── test_cli.py
    │   ├── test_process_control.py
    │   └── test_replay_read_guard.py
    └── swebench/
        ├── README.md
        ├── analyze_events.py
        ├── protocol.py
        ├── run.py
        ├── test_analyze_cli.py
        ├── test_arms.py
        └── test_cli.py
```

`tests/experiments/review-ablation/README.md` 与 `tests/experiments/routing-replay/README.md` 是协议文档，不含测试断言或夹具，因此不随本次测试材料重建删除。`tests/experiments/b67-replay/<date>/` 存放 B67 的真实回放记录（B67 的判拒口径由 `agent_runner` 的 `b67`/`b67-review` 离线覆盖，回放记录不构成回归保护）；首轮为 `2026-10-27`，两个工作区各一次真实模型运行。`tests/experiments/behavior-replay/<date>/` 存放四场景的行为回放记录（同一口径，同样不构成回归保护）；`2026-10-27` 那批的宿主是本地 pi 子代理 + `antigravity` 外部 CLI，没有 codex 适配器的工作区隔离与断网约束，执行者确实读过仓库外的 `~/.config/easydev/models.json`，因此只能当线索、不能当隔离环境下的证据。

## 0.12.1 回归补充

逐项裁决见 [审计处置](audit-0.12.1.md)。新增裸命令 PATH、正常退出后代清理、嵌套 `.git`、B02/公共契约提前退出、B02 空打开硬编码、运行期数据变化、SWE-bench 伪造 stdout 及相对资产路径、验收超时与继承输出句柄检查。固定补丁验收另接受两种合法的调用者已知 ID 异常文案，11 类伪修复仍须被拒绝。

## 1.2.0 – 1.2.3 验证边界

入口 4096 字节上限由仓库 `scripts/check.sh` 校验，链接、版本和配置示例沿用原门禁。normal 的首轮局部 P2 修复与至多一次定向复审、两次独立审查硬上限、转 full 时 `r=1` 或 `r=2` 的继承，以及门控第 5 项「高风险边界但未改变行为/决策/数据流时不启用」由 design-gate 状态机覆盖；normal 声明及升降级由事件分析 CLI 覆盖，上下文继承与实例分离由 design-gate 替身覆盖。B65 的 20 条上限与保留策略由 audit-overflow 覆盖。B61–B66 的行为结果仍须真实回放；字节数和替身结果不证明实际 token、费用或耗时收益。1.2.1 另由文档契约锁定定向复审的直接调用方范围、「现成检查」口径、高风险边界的实际影响判定与计量键序；1.2.2 把计量改为按角色分段并锁定该口径（第 14 条），新增 `tests/experiments/routing-replay/README.md` 复放协议。真实回放仍未覆盖 normal 微闭环、计量格式与角色分段的读取事件来源。
