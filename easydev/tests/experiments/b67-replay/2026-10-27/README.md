# B67 真实回放记录（2026-10-27）

B67（代码质量判据与 P3 口径）第一轮真实回放，两个工作区各跑一次真实模型。判据直接复用
`agent_runner` 的 `b67` 与 `b67-review` 场景 `evaluate`，因此离线替身与本次回放判定口径一致。

## 口径

- 单次、单模型、单仓库：只能作**线索**，不构成回归保护，也不改变判据、档位或预算。
- 两侧各自从 `runner.py prepare` 生成的新工作区开始，互不复用改动与上下文。
- 记录「实测」与「未采集」两类事实；未采集的字段不按通过处理。

## 环境

| 项 | 值 |
| --- | --- |
| 宿主 | Codex CLI `0.164.0-alpha.11.1`（`Logged in using ChatGPT`），经 pi 的 external-cli 子代理启动 |
| 模型与深度 | `gpt-6.1-sol` / `reasoning_effort = high`（本机 `~/.codex/config.toml`） |
| 工作区 | `/private/tmp/b67-run/runs/{b67,b67-review}/easydev/r1/workspace`（一次性目录，已不保留） |
| 技能副本 | `runner.py prepare --skill-root easydev`，安装时为 1.3.0；`references/` 与 1.3.1 一致 |
| 未采集 | `runner.py run` 的 `events.jsonl`、`result.json`、`valid_invocation`、usage 与墙钟；本次由子代理直接执行，只保留补丁与报告 |

## 实施侧（`b67`）

任务原文：让写盘失败能被上层感知并补回归测试；`save(order_id, total)` 签名、`DB` 常量不变；
不直接手改 `src/generated/schema.py`，要改就改 `codegen.py` 再生成。

模型改动两处，未碰生成文件与 `.agents/`：

```diff
--- a/src/service.py
+++ b/src/service.py
@@
 def save(order_id: str, total: int) -> None:
-    try:
-        records = json.loads(DB.read_text(encoding="utf-8")) if DB.exists() else []
-        records.append({"order_id": order_id, "total": total})
-        DB.write_text(json.dumps(records), encoding="utf-8")
-    except OSError:
-        pass
+    records = json.loads(DB.read_text(encoding="utf-8")) if DB.exists() else []
+    records.append({"order_id": order_id, "total": total})
+    DB.write_text(json.dumps(records), encoding="utf-8")
```

`tests/test_service.py` 新增 `test_save_propagates_write_failure`：patch `Path.write_text`
抛 `OSError`，断言同一个异常对象传到调用方。

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 写盘失败传到调用方 | 通过 | 探针 `RAISED` |
| 入口仍拒绝空 `order_id` | 通过 | 探针 `REJECTED` |
| 正常路径仍落库 | 通过 | 落库内容含 `"order_id": "o-1"` |
| 生成文件未被手改 | 通过 | 与 `codegen.py` 输出逐字节一致 |
| 改动不出产品代码与测试 | 通过 | 仅 `src/service.py`、`tests/test_service.py` |
| 测试套件通过 | 通过 | `unittest` rc=0（2 项） |
| 回退修复后回归测试必须失败 | 通过 | 换回吞异常原文后 rc=1 |
| 运行时 Skill 未被改 | 通过 | 快照无差异 |

8/8 通过。模型没有新增异常类型包装，也没有在 `service.save` 里补一道 `order_id` 校验，
落在「克制复杂度」判据一侧。

## 审查侧（`b67-review`）

同一夹具加两个对照样本：`src/report.py`（四层嵌套、三处重复回退行）与
`src/export.py`（两空格缩进、驼峰循环变量、混用引号）。要求只读、按 P0–P3 报确定发现、
纯风格不单独报、代价型按 P3 写明位置与代价。

模型给出的机器块：

```easydev-findings
P2 | 位置=src/service.py:12 | 触发=读取或写入订单文件发生 OSError | 后果=异常被吞掉，入口正常返回，调用方无法感知订单保存失败
P3 | 位置=src/report.py:4 | 触发=修改默认报表行规则或汇率分支 | 后果=多层嵌套及三处重复追加逻辑增加分支核对和同步修改成本
```

| 检查 | 结果 | 证据 |
| --- | --- | --- |
| 未改动文件 / 索引 / 运行时 Skill | 通过 | `git status` 为空 |
| 发现块格式合法 | 通过 | 顶层单块、每行四段 |
| 按严重度排序 | 通过 | `P2, P3` |
| 位置可解析 | 通过 | `src/service.py:12`、`src/report.py:4` 均为真实行 |
| 报出代价型写法 | 通过 | `src/report.py:4` 命中 |
| 严重度不高于 P2 | 通过 | 代价型为 P3 |
| 不为纯风格产生发现 | 通过 | 无 `src/export.py` 条目 |

9/9 通过。报告另写了「仅静态分析、未执行测试」的限制说明，未把未验证项算成发现。

## 判读

- 本轮支持「B67 判据可被真实模型执行」这一线索：实施侧长在克制复杂度与回退可证伪，
  审查侧长在代价/风格区分。
- 单次运行不能证明稳定性，也不能说明换了模型仍成立；`b67`/`b67-review` 的离线用例只验证
  判拒口径，不替代回放。
- 下次同向结果需另起日期目录；口径冲突时以 `tests/behavior.md` 的 B67 与
  `references/code-quality.md` 为准，不写回本记录。
