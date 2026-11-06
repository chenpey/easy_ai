# 维护材料总览

`tests/` 只用于维护 EasyDev 本身，不是普通编码任务的运行依赖。这里的检查都不启动真实模型：外部边界（Codex CLI、pytest 子进程、网络、时钟）由 `tests/experiments/stubs/` 中的替身替代，断言落在 CLI 的退出码、stdout/stderr 和产物文件上。

## 运行

套件统一由 pytest 运行（CI 安装 `pytest==8.4.2`），测试代码也不依赖 unittest：断言用 bare `assert`，每例夹具用 `setup_method`/`teardown_method` 建立与逆序清理，数据驱动用 `@pytest.mark.parametrize`。同一函数里必须保留「前置准备 + 循环 + 后置断言」结构时，用 `from conftest import subtests` 加 `with subtests.test(command=command):` 包住每次迭代：单个参数失败只记录、循环继续，用例结束时按参数标签汇总成一次失败（等价 `unittest.subTest`；少了它，红跑时第一个失败会吃掉同函数剩余参数，也定位不到是哪个参数）；`pytest.skip` 在子用例内同样只跳过当前参数，且失败优先于跳过：已发生的参数失败不会被后面的 skip 变成「跳过」（那会让红跑 rc=0），循环外抛异常时已记录的参数失败与原异常一并报告。`pytest.ini` 的 `python_classes` 沿用仓库既有类名（`Test*`/`*Tests`/`*Test`/`*Case`）。在本目录的上级（`easydev/`）执行：

```bash
bash tests/cases/public-contract-callers/run.sh
bash tests/cases/version-gate/run.sh
bash tests/cases/design-gate/run.sh
bash tests/cases/audit-overflow/run.sh
bash tests/cases/models-config/run.sh
python3 -m pytest tests/experiments/test_role_metric.py tests/experiments/test_design_gate.py -v
python3 -m pytest tests/experiments/agent_runner -v
python3 -m pytest tests/experiments/swebench -v
```

八项互不依赖，在同一冻结快照上批量并发并分别保存日志；CI 另将仓库门禁与安装冒烟同这八项共十个检查拆为 fail-fast=false 的矩阵任务。只有固定补丁复验内部须串行，按“红 → 参考补丁 → 绿 → 独立验收 → 伪修复拒绝”，因为后一步依赖前一步的仓库状态。本机全量耗时目标 < 240s（实测单进程全量 pytest 约 210s，并发跑八项时墙钟约 150s，`agent_runner` 一项占其中的 140s 以上）。

`pytest.ini` 设了 `--import-mode=importlib`，所以 `python3 -m pytest` 在 `easydev/` 根就能一次跑全量（`agent_runner` 与 `swebench` 下同名的 `test_cli.py` 不再冲突）；上面拆成三条只是为了让 CI 并行。

`agent_runner` 套件里的 `rg --files` 正例需要本机装有 `ripgrep`（不在 POSIX 必备工具内）：装有 `rg` 时断言原工具真的执行并列出工作区文件，缺失时只校验守卫的放行判定、不宣称原工具已执行；CI 的行为 runner 作业先用 `apt-get` 安装 ripgrep，真实执行分支由 CI 覆盖。

## 覆盖什么

- **固定补丁复验** (`tests/cases/public-contract-callers/`)：初始仓库的红测、参考补丁后的 403 与契约检查、独立验收，以及 11 类伪修复被拒绝；红绿两段仍运行夹具自带的 `check_orders.py`，独立验收 `acceptance.py` 不依赖它。
- **行为 runner CLI** (`tests/experiments/agent_runner/test_cli.py`)：`list` / `prepare` / `run` 的退出码与产物；B01 路由/状态标记/访问/ignored 文件/Git 配置负例、运行时随机完成标记（每次验收由环境变量传入，退出码 0 且 stdout 精确匹配才通过；伪造 `print("ok")` 后提前退出、只打印标记、直接 `raise SystemExit(0)`、多余 stdout 与 `PYTHONOPTIMIZE=1` 下文案未改均失败，正常改动及单引号/无空格变体通过）、运行时 Skill 篡改、普通/回退暂存与 assume-unchanged、缺 `turn.completed`、畸形事件、非零退出码、超时后进程组清理、捕获不执行 hook/filter；相对路径与认证链接、B02 高风险 full、quick/候选档位、未完成 reviewer、所有权校验及拒绝先读后鉴权、`sitecustomize` 不执行；B16 合格发现块与围栏/位置/排序/数量/读取负例；b67 与 b67-review 覆盖代码质量判据的实施与审查口径——写盘失败必须传到调用方、生成文件不手改、修复回退后回归测试必须失败，以及固定夹具的代价型发现须报 P3（仅改 P2 被拒）、纯风格不报；public-contract-callers 的独立验收、保留文件与 benchmark 元数据负例。
- **SWE-bench runner CLI** (`tests/experiments/swebench/test_cli.py`)：`setup` / `run` / `summarize` / `calibrate`；结果读取器对身份、事件计数、协议版本与 F2P/P2P 矛盾的拒绝；补丁应用失败（agent patch 与 test patch）的终态存储、`patch_apply_failures` 计数与重跑不调用模型；manifest 选择与报告计数、每任务 eligible/excluded P2P 披露（含 `|` 的 nodeid 在表格单元格内转义）；模板解包逃逸与合法链接/延迟目录属性；保护模式的收紧权限、损坏/软链记录、软链在 chmod 前被拒绝、父子顺序、失败结束与换 cwd 恢复；补丁采集不执行 Agent hook/filter；P2P 回归与每 arm 报告。
- **事件归因 CLI** (`tests/experiments/swebench/test_analyze_cli.py`)：声明路径（含 normal 与升降级；中文「采用/分流/路径」与英文 `normal path`、`moved to the full route`，裸 `full path` 不误判、英文否定不误判）、真正 reviewer 交接与开发者提及/否定/禁读的区分、reviewer 完成与报告、P0–P3（发现须自成一行并以严重度起头，交接里复述模板不算发现）、文件/模块访问、usage/缓存率/耗时、零发现声明、畸形行容错、`receiver_thread_ids: null` 按缺失处理、`--output` 与 stdout 一致。
- **流程状态机与独立设计环节的确定性复验** (`tests/cases/design-gate/run.sh`)：normal 一次不同实例审查、无审查/作者自查不可完成、首轮无缺陷不得发起复审、第三次审查被拒；首轮仅局部 P2 时一次局部修复加定向复审可交付（审查 2 次），复审仍未关闭转 full 且记 `r=2/5`，非局部 P2 首轮后转 full 且继承 `r=1/5`；门控五项判定 14 组 fixture（含「只交给独立审查者不构成多阶段」与「高风险但未改变行为/决策/数据流不启用」两个可达成性回归，以及跨两包纯措辞仍命中跨边界）；`d/3` 与 `r/5` 记账与上限；候选第二轮须用户 opt-in；升版重写不计轮；受阻轮不算缺陷已关闭；剩余 `r` 不足两轮禁止单开设计重审；阶段增量审查最多 3 轮；施工期 `d` 用尽停止；宿主无委派能力暂停与用户授权后自查/豁免；多方案任务六种组合的可达性枚举；文档契约 46 条列表（含局部 P2 判定条件、定向复审的直接调用方范围、「现成检查」口径、高风险边界实际影响判定、计量字段键序与角色分段、模型的来源优先级与能力降级三档、施工闸必关项、重审前置范围、入口第四信号、effort 档位与 quick 专项验证强制加载入口；并行判断门槛替换旧放行条目，新增片数选择、高风险例外、判断账本、入口加载、入口单写边界、B58 系列和可选收益观测 7 条），另有 1 条门控项数检查（合计 47 项），列表契约按小节作用域求值并支持禁用措辞（标题锚点截到下一个同级或更高级标题；非标题锚点截到下一个二级或更高级标题；围栏内伪标题不计）；另有 9 组判负检查加 1 组文件作用域正例（共 10 组机制自检），保证“句子搬到邻节”“旧新措辞并存”会被判负，包含并行门槛搬动和旧评测门槛残留，而允许跨节搬动的整份文件作用域不被误判。
- **回放读取守卫** (`tests/experiments/agent_runner/test_replay_read_guard.py`)：两个 canonical 允许根内放行；HOME 外 operator-config、sibling 前缀、软链、`..`、显式 `/etc`/`/usr` 读取拒绝；`rg --follow`/`find -L`/`ls -L` 固定拒绝；无链普通递归与含外链树的区别；入口预算/内部截止/畸形输入/缺 policy/结构化无路径均为 deny + 健康失败；生成 hooks.json 的绝对命令与 `.git/info/exclude`。
- **流程边界与文档 CLI 回归** (`tests/experiments/test_design_gate.py`)：通过流程模型公开 API 检查当前委派条件、验收阶段门控、升版关闸、最终完整审查、依赖失效和历史预算；通过真实 `design-gate/run.sh` 检查隔离文档副本的规则退化被拒，含取消独立性与隔离门槛、任意片数、高风险默认并行、串行包装、验证不停写及把可选观测改成准入。只证明确定性约束，不模拟宿主调度或宣称真实模型通过。
- **按角色计量** (`tests/experiments/test_role_metric.py`)：主代理与子代理分别归因；`--path` 过滤回答「哪个角色读了某个文件」；子代理只用无路径参数的外部委派工具时，stderr 给一行提示而不是默认「没读」；`--json` 可机器读取。
- **normal 微闭环观测**（`tests/experiments/agent_runner/test_cli.py` 的 `b61` 用例）：按 `tests/behavior.md` 的 B61 三支判分——首轮无必关项或仅局部 P2 时终态 normal、独立审查 1–2 次、必须带回报告；首轮报 P0/P1、非局部 P2 或验收缺口时允许转 full，但必须有初始档位声明、起跳于 normal、升级前已有一份完成的审查报告、引用一条归因解析出的结构化缺陷，且 `r≤5`/`d≤3`；`details.normal_micro_loop` 区分「交付」与「按规升级」。另固定第三次审查、无声明、无报告即升级、无证据升级、超预算升级、quick 起跳、normal 内闭环 P0/P1、错误行为各自被拒，`不升级到 full` 与「不称 full 闭环」这类否定复述不被误判；复审是否读到调用方只记 detail。任务文本钉死 `float` 数值域与折扣方向（`discount` 为减免比例），两处都是实测逼出来的：不钉时升级链没有收口点，或两次回放各选一种读法导致样本报废。
- **档位对照臂** (`tests/experiments/swebench/test_arms.py`)：三个 `easydev-*` 档位臂的 prompt 各自钉住档位且互不相同、auto 臂 prompt 不变、默认臂集合不变、未知臂在读取实验资产前被拒。
- **audit 溢出与分片覆盖** (`tests/cases/audit-overflow/run.sh`)：固定 21 个 P1 与 2 个 P2，正文仅取 20 条，附录保留 23 条完整记录；摘要披露正文外的 1 个 P1；正文超限、附录遗漏、溢出摘要缺失、虚假全覆盖或未列未审分片均被拒绝。
- **版本提取门禁** (`tests/cases/version-gate/run.sh`)：`scripts/skill_version.py` 的九组 fixture（合法、`description` 内含 `version:` 但 `metadata.version` 合法、重复 `version` 键、`version` 错位到其他映射、只在 `description` 里出现 `version`、带引号的重复 `version` 键、双引号转义后的重复 `version` 键、值内嵌套引号、未闭合引号）；有 PyYAML 与无 PyYAML 两个分支都要通过，后者用临时 `yaml` 垫片固定。降级模式明确拒绝双引号键中的反斜杠转义，PyYAML 分支按解码后的重复键拒绝该 fixture。

- **模型配置 schema 与来源选择** (`tests/cases/models-config/run.sh`)：`scripts/models.py` 是 `check.sh` 与 `environment.md` 共用的唯一 schema/validator 与来源选择实现。正例（内置默认、项目/用户/对话指定文件）必须通过；重复键、非法 JSON、未知顶层键或 role 键、缺必需角色、`model` 缺供应商、`reasoning_effort` 越界、`upgrades` 超两项或相邻级完全相同、选中文件无效或不存在时静默回退、`role_overrides` 为空对象都须被拒绝；并固定「文件之间不合并」与「项目 > 用户 > 内置默认」两处来源优先级；`off` 合法、无宿主依据的 `ultra` 被拒绝。

## 未覆盖什么

- `analyze_events` 只读 `result.json` 的 usage，事件流 `turn.completed` 的 usage 不聚合，token 0 无法区分“未知”与真实零；这是当前实现的已知限制，测试只固定行为，不宣称“缺失即未知”（见 `tests/experiments/swebench/README.md`）。
- 保护模式的 P2 边缘未逐项迁移：相对 `--protect-paths` 的记录内容、指向实验目录的软链别名、记录落点包含判断（均由 `run.py` 现有实现约束，但当前没有 CLI 回归输入）。
- PID 身份复用、`setsid()` 脱离会话或忽略 SIGTERM 的后代清理、双 fork 被 init 收养等超时清理边界；普通子进程与默认 SIGTERM 响应已覆盖。

- 回放守卫所用的 `tests/experiments/shell.py` 对 heredoc 只支持有限子集：只处理能证明正文边界的 Bash/POSIX 风格形态。嵌套执行 heredoc、任意 `${...}`（含简单 `${NAME}`）、`$((...))`、复杂或嵌套反引号、未引用正文里的反斜杠续行与跨行 `$()` 交互都整次回退到旧词法路径，回退仍沿用旧解析器的误拦/漏检，不构成沙箱；`<<-` 只去前导 tab，delimiter 不做宽松 trim，CRLF 与 Windows/PowerShell 未覆盖。
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
├── cases/
│   ├── README.md
│   ├── version-gate/
│   │   └── run.sh
│   ├── models-config/
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
    ├── test_design_gate.py
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

## 版本历史与历史记录

各版本的验证边界与回归补充见 [CHANGELOG](../CHANGELOG.md) 对应条目；`0.12.1` 的逐项裁决保存在 [审计处置](audit-0.12.1.md)。
