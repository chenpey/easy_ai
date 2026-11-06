# SWE-bench Lite 小样本实验

本目录包含基于 SWE-bench Lite 任务集的小样本对照实验运行器与事件归因分析工具。

## 判定标准

```text
resolved = 全部 FAIL_TO_PASS 修复成功
           且环境基线可通过的 PASS_TO_PASS 无新增回归
```

- **基线过滤**：`PASS_TO_PASS` 预先在本机运行基线，剔除环境自身无法通过的用例；测试结果直接从 JUnit XML 解析。
- **补丁失败终态**：代码补丁或测试补丁应用失败为非重试终态，在结果中标记 `patch_applied=false` 并记录于汇总报告中。

## 运行前提

运行器不负责下载数据集或构建依赖，需预先准备：
1. `princeton-nlp/SWE-bench_Lite` 任务元数据 JSON。
2. 各任务源码压缩包 `<instance_id>.tar` 与 Python 虚拟环境 `<instance_id>`。
3. 可用的 Codex CLI 及 `~/.codex/auth.json`。

解包阶段内建路径逃逸安全检查（拒绝绝对路径、`..` 越界或外部硬链接）。

## 核心命令

```bash
# 1. 校准：验证测试补丁与 gold patch 基线
python3 tests/experiments/swebench/run.py calibrate \
  --dataset /path/to/swebench-lite.json \
  --templates /path/to/templates \
  --venvs /path/to/venvs \
  --out /path/to/experiment

# 2. 准备：链接/解包实验资产
python3 tests/experiments/swebench/run.py setup \
  --dataset /path/to/swebench-lite.json \
  --templates /path/to/templates \
  --venvs /path/to/venvs \
  --experiment /path/to/experiment

# 3. 运行：执行小样本对照实验（已完成的任务自动跳过）
python3 tests/experiments/swebench/run.py run \
  --experiment /path/to/experiment --repeats 3 --concurrency 2

# 4. 汇总：生成报告与评分结果
python3 tests/experiments/swebench/run.py summarize \
  --experiment /path/to/experiment

# 5. 事件归因：独立分析已有运行目录的路由与审查记录
python3 tests/experiments/swebench/analyze_events.py \
  --experiment /path/to/experiment
```

## 对照臂说明

- 默认对照：`baseline`（无技能）、`checklist`、`easydev`（auto 自动分流）。
- 强制档位：`easydev-quick`、`easydev-normal`、`easydev-full`（锁定特定路径，用于路由对照实验）。
- 安全保护：`run` 执行期间默认将 `dataset.json`、`gold/` 与基线目录设为只读保护（mode 0），运行后恢复。

## 事件归因与已知限制

`analyze_events.py` 解析模型声明路径、实际委派 reviewer、严重度分级（P0–P3）、文件读取及 usage 统计。

**已知限制**：当前仅读取 `result.json` 中的 usage，不聚合 `turn.completed` 事件流中的 usage。若结果中缺失 usage，输出为 0，与真实的零 token 无法区分。

## 离线自测

```bash
python3 -m pytest tests/experiments/swebench -v
```

通过 `codex_stub.py` 与 `pytest_stub.py` 替身驱动，覆盖 CLI 解析、模板安全解包、保护模式恢复、事件归因与 usage 边界。
