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
```

七项互不依赖，在同一冻结快照上批量并发并分别保存日志；CI 另将仓库门禁与安装冒烟同这七项共九个检查拆为 fail-fast=false 的矩阵任务。只有固定补丁复验内部须串行，按“红 → 参考补丁 → 绿 → 独立验收 → 伪修复拒绝”，因为后一步依赖前一步的仓库状态。本机全量耗时目标 < 120s。

## 覆盖什么

- **固定补丁复验** (`tests/cases/public-contract-callers/`)：初始仓库的红测、参考补丁后的 403 与契约检查、独立验收，以及 11 类伪修复被拒绝；红绿两段仍运行夹具自带的 `check_orders.py`，独立验收 `acceptance.py` 不依赖它。
- **行为 runner CLI** (`tests/experiments/agent_runner/test_cli.py`)：`list` / `prepare` / `run` 的退出码与产物；B01 路由/状态标记/访问/ignored 文件/Git 配置负例、运行时随机完成标记（每次验收由环境变量传入，退出码 0 且 stdout 精确匹配才通过；伪造 `print("ok")` 后提前退出、只打印标记、直接 `raise SystemExit(0)`、多余 stdout 与 `PYTHONOPTIMIZE=1` 下文案未改均失败，正常改动及单引号/无空格变体通过）、运行时 Skill 篡改、普通/回退暂存与 assume-unchanged、缺 `turn.completed`、畸形事件、非零退出码、超时后进程组清理、捕获不执行 hook/filter；相对路径与认证链接、B02 高风险 full、quick/候选档位、未完成 reviewer、所有权校验及拒绝先读后鉴权、`sitecustomize` 不执行；B16 合格发现块与围栏/位置/排序/数量/读取负例；public-contract-callers 的独立验收、保留文件与 benchmark 元数据负例。
- **SWE-bench runner CLI** (`tests/experiments/swebench/test_cli.py`)：`setup` / `run` / `summarize` / `calibrate`；结果读取器对身份、事件计数、协议版本与 F2P/P2P 矛盾的拒绝；补丁应用失败（agent patch 与 test patch）的终态存储、`patch_apply_failures` 计数与重跑不调用模型；manifest 选择与报告计数、每任务 eligible/excluded P2P 披露（含 `|` 的 nodeid 在表格单元格内转义）；模板解包逃逸与合法链接/延迟目录属性；保护模式的收紧权限、损坏/软链记录、软链在 chmod 前被拒绝、父子顺序、锁定中途失败回滚、失败结束与换 cwd 恢复；补丁采集不执行 Agent hook/filter；P2P 回归与每 arm 报告。
- **事件归因 CLI** (`tests/experiments/swebench/test_analyze_cli.py`)：声明路径（含 normal 与升降级）、真正 reviewer 交接与开发者提及/否定/禁读的区分、reviewer 完成与报告、P0–P3、文件/模块访问、usage/缓存率/耗时、零发现声明、畸形行容错、`receiver_thread_ids: null` 按缺失处理、`--output` 与 stdout 一致。
- **流程状态机与独立设计环节的确定性复验** (`tests/cases/design-gate/run.sh`)：normal 一次不同实例审查、无审查/作者自查不可完成、首轮无缺陷不得发起复审、第三次审查被拒；首轮仅局部 P2 时一次局部修复加定向复审可交付（审查 2 次），复审仍未关闭转 full 且记 `r=2/5`，非局部 P2 首轮后转 full 且继承 `r=1/5`；门控五项判定 13 组 fixture（含「只交给独立审查者不构成多阶段」与「高风险但未改变行为/决策/数据流不启用」两个可达成性回归）；`d/3` 与 `r/5` 记账与上限；候选第二轮须用户 opt-in；升版重写不计轮；受阻轮不算缺陷已关闭；剩余 `r` 不足两轮禁止单开设计重审；阶段增量审查最多 3 轮；施工期 `d` 用尽停止；多方案任务六种组合的可达性枚举；文档契约 14 组（含局部 P2 判定条件、定向复审的直接调用方范围、「现成检查」口径、高风险边界实际影响判定、计量字段键序与角色分段）。
- **按角色计量** (`tests/experiments/test_role_metric.py`)：主代理与子代理分别归因；`--path` 过滤回答「哪个角色读了某个文件」；`--json` 可机器读取。
- **normal 微闭环观测**（`tests/experiments/agent_runner/test_cli.py` 的 `b61` 用例）：路由必须是 normal、独立审查 1–2 次、必须带回报告、不得宣称 full 闭环、只改 `src/cart.py`；另固定第三次审查、quick 声明、错误行为各自被拒，复审是否读到调用方只记 detail。
- **档位对照臂** (`tests/experiments/swebench/test_arms.py`)：三个 `easydev-*` 档位臂的 prompt 各自钉住档位且互不相同、auto 臂 prompt 不变、默认臂集合不变、未知臂在读取实验资产前被拒。
- **audit 溢出与分片覆盖** (`tests/cases/audit-overflow/run.sh`)：固定 21 个 P1 与 2 个 P2，正文仅取 20 条，附录保留 23 条完整记录；摘要披露正文外的 1 个 P1；正文超限、附录遗漏、溢出摘要缺失、虚假全覆盖或未列未审分片均被拒绝。
- **版本提取门禁** (`tests/cases/version-gate/run.sh`)：`scripts/skill_version.py` 的九组 fixture（合法、`description` 内含 `version:` 但 `metadata.version` 合法、重复 `version` 键、`version` 错位到其他映射、只在 `description` 里出现 `version`、带引号的重复 `version` 键、双引号转义后的重复 `version` 键、值内嵌套引号、未闭合引号）；有 PyYAML 与无 PyYAML 两个分支都要通过，后者用临时 `yaml` 垫片固定。降级模式明确拒绝双引号键中的反斜杠转义，PyYAML 分支按解码后的重复键拒绝该 fixture。

## 未覆盖什么

- `analyze_events` 只读 `result.json` 的 usage，事件流 `turn.completed` 的 usage 不聚合，token 0 无法区分“未知”与真实零；这是当前实现的已知限制，测试只固定行为，不宣称“缺失即未知”（见 `tests/experiments/swebench/README.md`）。
- 保护模式的 P2 边缘未逐项迁移：相对 `--protect-paths` 的记录内容、指向实验目录的软链别名、记录落点包含判断（均由 `run.py` 现有实现约束，但当前没有 CLI 回归输入）。
- PID 身份复用、`setsid()` 脱离会话或忽略 SIGTERM 的后代清理、双 fork 被 init 收养等超时清理边界；普通子进程与默认 SIGTERM 响应已覆盖。

- 真实 Codex CLI 与真实模型行为：所有检查都使用替身，只验证验收机制本身，不验证模型是否遵守 EasyDev 流程，也不产生可引用的模型成绩。
- `role_metric.py` 的 `--path` 是子串匹配：`src/cart.py` 也会命中 `legacy/src/cart.py`，只用于同一仓库内的相对比较。
- `role_metric.py` 只统计工具参数里出现的路径与 usage，不判断审查质量，也不证明报告里的计量诚实；Codex 宿主没有这套 artifact 时不可用。
- `b61` 的「复审是否读了直接调用方」只记 detail：事件层无法把文件读取稳定归属到 reviewer 线程，它提供真实回放的观测值，不构成该规则的回归保护。
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
    │   ├── adapters/{__init__,codex_cli}.py
    │   ├── scenarios/{__init__,common,b01,b02,b16,b61,public_contract_callers}.py
    │   ├── test_cli.py
    │   └── test_process_control.py
    └── swebench/
        ├── README.md
        ├── analyze_events.py
        ├── protocol.py
        ├── run.py
        ├── test_analyze_cli.py
        ├── test_arms.py
        └── test_cli.py
```

`tests/experiments/review-ablation/README.md` 与 `tests/experiments/routing-replay/README.md` 是协议文档，不含测试断言或夹具，因此不随本次测试材料重建删除。

## 0.12.1 回归补充

逐项裁决见 [审计处置](audit-0.12.1.md)。新增裸命令 PATH、正常退出后代清理、嵌套 `.git`、B02/公共契约提前退出、B02 空打开硬编码、运行期数据变化、SWE-bench 伪造 stdout 及相对资产路径、验收超时与继承输出句柄检查。固定补丁验收另接受两种合法的调用者已知 ID 异常文案，11 类伪修复仍须被拒绝。

## 1.2.0 – 1.2.3 验证边界

入口 4096 字节上限由仓库 `scripts/check.sh` 校验，链接、版本和配置示例沿用原门禁。normal 的首轮局部 P2 修复与至多一次定向复审、两次独立审查硬上限、转 full 时 `r=1` 或 `r=2` 的继承，以及门控第 5 项「高风险边界但未改变行为/决策/数据流时不启用」由 design-gate 状态机覆盖；normal 声明及升降级由事件分析 CLI 覆盖，上下文继承与实例分离由 design-gate 替身覆盖。B65 的 20 条上限与保留策略由 audit-overflow 覆盖。B61–B66 的行为结果仍须真实回放；字节数和替身结果不证明实际 token、费用或耗时收益。1.2.1 另由文档契约锁定定向复审的直接调用方范围、「现成检查」口径、高风险边界的实际影响判定与计量键序；1.2.2 把计量改为按角色分段并锁定该口径（第 14 条），新增 `tests/experiments/routing-replay/README.md` 复放协议。真实回放仍未覆盖 normal 微闭环、计量格式与角色分段的读取事件来源。
