# 实验目录

本目录包含离线实验套件（替身驱动，无真实模型调用，进入 CI）与真实模型回放记录（`b67-replay/`、`behavior-replay/`，静态留存，不进 CI）。

## 模块清单

| 路径 | 作用 |
| --- | --- |
| `stubs/codex_stub.py` | 模拟 `codex exec`：通过 `EASYDEV_STUB_PLAN` JSON 注入事件流、审查报告、改动与退出码 |
| `stubs/pytest_stub.py` | 模拟 `pytest`：通过 `EASYDEV_PYTEST_PLAN` 输出测试日志并生成 JUnit XML |
| `agent_runner/` | 单宿主行为 Runner，覆盖 B01、B02、B16、B61、B67 及契约场景 |
| `swebench/` | SWE-bench Lite 小样本运行器、事件归因与 CLI 套件 |
| `process_control.py` | 共享的跨平台超时控制与进程树清理工具 |
| `role_metric.py` | 按角色统计 references 文件读取、实际模型与 usage 消耗 |
| `review-ablation/` | 独立审查四臂消融实验协议文档 |
| `routing-replay/` | 三任务 × 三档位路由复放协议文档 |
| `b67-replay/` | B67 实施侧与审查侧的真实模型回放证据 |
| `behavior-replay/` | 四场景行为回放的静态评测记录 |

## 运行方式

```bash
# 需 pytest 环境
python3 -m pytest tests/experiments/test_role_metric.py -v
python3 -m pytest tests/experiments/agent_runner -v
python3 -m pytest tests/experiments/swebench -v
```

三组套件互不依赖，可并行执行；各测试用例使用专属临时目录隔离。

## 替身边界

替身仅模拟外部进程接口（Codex CLI、pytest、时钟），不侵入被测模块内部。断言基于 `runner.py`、`run.py` 和 `analyze_events.py` 的 CLI 输出与产物文件；不替代真实模型的自律性评测。
