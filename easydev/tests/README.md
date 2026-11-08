# 维护材料总览

`tests/` 仅用于维护 EasyDev 本身，非编码任务运行依赖。所有测试均使用 `tests/experiments/stubs/` 替身驱动（模拟外部 CLI、pytest、网络与时钟），不消耗真实模型调用，断言基于退出码、输出文本与产物文件。

## 运行

在 `easydev/` 目录下执行：

```bash
# 确定性断面用例（无第三方依赖，纯标准库即可运行）
bash tests/cases/public-contract-callers/run.sh
bash tests/cases/version-gate/run.sh
bash tests/cases/design-gate/run.sh
bash tests/cases/audit-overflow/run.sh
bash tests/cases/models-config/run.sh

# 单元与行为回归套件（需 pytest）
python3 -m pytest tests/experiments/test_role_metric.py tests/experiments/test_design_gate.py -v
python3 -m pytest tests/experiments/agent_runner -v
python3 -m pytest tests/experiments/swebench -v
```

- **执行方式**：各项检查互不依赖，支持并发执行；CI 将上述用例与根目录门禁拆为并行矩阵。
- **运行环境**：测试遵循 pytest 规范（`pytest==8.4.2`，断言用 bare `assert`，夹具由 `setup_method`/`teardown_method` 清理）。`agent_runner` 真实执行分支需 `ripgrep`，缺失时自动降级仅验证守卫放行逻辑。

## 覆盖什么

- **固定补丁复验** (`tests/cases/public-contract-callers/`)：红测失败、参考补丁后契约通过与独立验收，以及 11 类伪修复（身份忽略、数据泄露、硬编码等）召回拒绝。
- **版本提取门禁** (`tests/cases/version-gate/`)：提取 `metadata.version`，拒绝重复键、错位键、未闭合引号及非法转义（覆盖 PyYAML 与标准库无 yaml 降级模式）。
- **模型配置校验** (`tests/cases/models-config/`)：`scripts/models.py` 的 schema 校验与来源优先级（项目 > 用户 > 内置默认，且文件不合并；拒绝非法键/角色/供应商缺失与越界参数）。
- **设计门控与流程状态机** (`tests/cases/design-gate/`)：normal 定向审查与局部 P2 修复；转 full 继承账本；门控五项判定、`d/3` 与 `r/5` 预算记账、施工闸放行条件及文档契约回归。
- **审计分片与溢出处理** (`tests/cases/audit-overflow/`)：正文上限 30 条，附录保留全量记录，摘要逐条披露溢出 P1 影响与覆盖完整性校验。
- **行为 Runner CLI** (`tests/experiments/agent_runner/`)：覆盖 `list`/`prepare`/`run` 命令行生命周期；B01 quick 保持、B02 full 委派、B16 只读 audit、B61 normal 微闭环与复审范围、B67 代码质量判据。
- **SWE-bench Runner CLI** (`tests/experiments/swebench/`)：小样本运行器 `setup`/`run`/`summarize`/`calibrate`；结果解析、模板解包安全、路径保护与异常退出处理。
- **事件归因分析** (`tests/experiments/swebench/analyze_events.py`)：解析路径声明、reviewer 独立性、P0–P3 严重度分级、文件访问及 usage 统计。
- **角色计量** (`tests/experiments/test_role_metric.py`)：统计主代理与子代理 references 读取情况与模型消耗。

## 未覆盖什么

- **真实模型自律性**：替身仅验证验收机制有效性，不证明真实模型一定会遵守规则。
- **真实网络与沙箱隔离**：测试仅通过 CLI 参数要求断网与工作区隔离，不替代容器或操作系统级别的命名空间沙箱。
- **事件流 Usage 聚合限制**：`analyze_events.py` 当前仅读取 `result.json`，不聚合 `turn.completed` 的事件流 usage，真实零与未知 token 无法在结果中区分。
- **外部工具读取深度**：`role_metric.py` 统计基于命令行参数，通过外部委派（如嵌套 agent CLI）执行的操作无法还原其子进程读取的文件。
- **系统级恶意代码逃逸**：回放守卫采用词法与路径分析，不覆盖复杂的进程替换、脱离会话或特权逃逸。

## 目录结构

```text
tests/
├── README.md                          # 维护材料总览
├── behavior.md                        # 行为规约与用例矩阵
├── audit-0.12.1.md                    # 历史审计处置记录
├── cases/                             # 确定性断面用例目录
│   ├── README.md
│   ├── public-contract-callers/       # 契约变更与调用方适配复验
│   ├── version-gate/                  # 版本号提取门禁
│   ├── models-config/                 # 模型配置 schema 与优先级
│   ├── design-gate/                   # 设计门控与两本账状态机
│   └── audit-overflow/                # 大仓审计分片与溢出处理
└── experiments/                       # 离线实验与回放工具
    ├── README.md
    ├── process_control.py             # 进程树与超时清理
    ├── role_metric.py                 # 按角色统计 usage 与读取
    ├── stubs/                         # Codex CLI 与 pytest 替身
    ├── agent_runner/                  # 单宿主行为 Runner
    └── swebench/                      # SWE-bench 小样本运行与事件归因
```

## 版本历史与追溯

各版本历史边界与修改细节见 [CHANGELOG](../CHANGELOG.md)；历史专项处置记录见 [审计处置](audit-0.12.1.md)。
