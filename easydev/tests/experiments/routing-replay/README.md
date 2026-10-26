# 路由复放协议

只定义口径，不含结果、不含可执行检查、不进 CI。用途：回答两个被推迟的判断——第二视角信号②是否过宽（1.2.1 评审意见 3）、normal 那一次独立审查值不值（意见 7）。离线替身只能验证记录格式，结论必须来自真实宿主与真实模型。

## 任务选取

同一仓库一批取三个真实任务（真实提交或真实 issue，不人为构造），分别覆盖：

1. 局部私有函数改动，改动前已有能拦截回归的检查（预期 quick）。
2. 有界兼容增量：两个及以上独立内部调用方，或改动共享契约但存在本次未改动的内部使用方（预期 normal，命中信号②或③）。
3. 改变重大外部契约、持久数据语义或高风险边界行为（预期 full，通常同时命中设计门控）。

选取时记录三项事实：改动前的检查覆盖状况、独立调用方/使用方清单、是否触及高风险边界。每个任务须有**执行者不可见的独立验收**（口径同 [四臂消融](../review-ablation/README.md#任务与预算)），否则无法判定某缺陷是否被发现。

## 复放

每个任务分别以 quick、normal、full 各跑一次，每批共 9 次。同一基线提交、同一模型与推理深度；顺序固定 quick → normal → full；每次从干净工作区开始，不复用前一档的改动、账本或上下文。

档位由 `swebench/run.py` 的三个对照臂钉住，prompt 明写「用户已指定档位，不要改用 auto 重新分流」：

```bash
python3 tests/experiments/swebench/run.py setup  --dataset … --templates … --venvs … --experiment /tmp/rr
python3 tests/experiments/swebench/run.py run    --experiment /tmp/rr \
  --tasks <t1> <t2> <t3> --arms easydev-quick easydev-normal \
  --repeats 1 --concurrency 1
python3 tests/experiments/swebench/run.py summarize --experiment /tmp/rr
```

`--tasks` 必须显式给出本批三个任务（`run.py` 的默认任务表是五个，照抄会得到 5×2 次）；档位顺序靠逐臂串行（`--concurrency 1`）保证；`summarize` 按臂名字典序聚合，报告里的臂顺序不代表执行顺序。先用 `easydev-quick` 与 `easydev-normal` 两臂回答「normal 值不值」，需要门控数据时再加 `easydev-full`；`easydev`（auto）臂保留作实际分流的对照。隐藏的 FAIL_TO_PASS / PASS_TO_PASS 即本协议要求的「执行者不可见的独立验收」。

角色分段与 token 用 `tests/experiments/role_metric.py`（pi 宿主，读会话目录）或 `swebench/analyze_events.py`（Codex 宿主，读 `events.jsonl`）：前者给子代理的实际模型、usage、耗时与两类的 references 读取（主会话 jsonl 不含 usage，主代理行只有读取记录），后者给声明路径、reviewer 实例与报告、模块访问。Codex 侧不推断子代理 token，按角色的成本只能到「读了哪些 references」。

## 记录字段

| 字段 | 来源 |
| --- | --- |
| 角色分段的 `模块` | 读取事件（pi：`role_metric.py`；Codex：`analyze_events.py`）；无事件时取报告并标注为自述 |
| 总 token | 宿主 usage，不可得写未知 |
| 墙钟耗时 | 宿主记录 |
| 独立审查次数、`d`/`r` | 报告与账本 |
| 缺陷：各档审查报出的确定发现 | 各档审查报告（quick 档取自查与验证记录） |
| 缺陷：后续任务或用户才发现 | 交付记录与独立验收 |
| 其中 quick 档没有任何机制能发现的缺陷 | 逐条比对上一行与本档报告，给位置、影响与来源档 |
| 实际档位与分流依据 | 报告中的判定依据 |
| 门控命中项（仅 full 档） | 账本 |

核心指标是「其中 quick 档没有任何机制能发现的缺陷」一行：它直接回答多花一次审查换来了什么，由「缺陷」两行与本档报告（quick 档取自查与验证记录）逐条比对得出，不由其他行推导。所有字段由运行方记录，不采用模型自评。

## 判读规则

- 只记录线索，不直接改阈值：单批 3×3 只能否证、不能证明。
- 信号②：仅在**两个及以上**命中信号②的任务上，normal 审查均零确定发现、且这些任务的缺陷在 quick 下同样未暴露时，记为「②在该仓库可能过宽」的线索。单批三个任务通常只含一个此类任务，需第二批同向才考虑收紧。
- 信号①：分流允许 quick 的任务（任务 1，任务 2 视实际判定）若 quick 档漏掉后续才发现的 P1/P2，即构成「不得放宽①」的否证证据。
- 设计门控不在本协议评估范围内（已有确定性载体 `tests/cases/design-gate`）；full 档只记录门控命中项与审查发现，用于成本对比，不据此删除门控。

## 边界

- 需要真实宿主委派能力与真实模型；替身环境不产生有效数据。
- 成本为每批 9 次运行，token 与耗时以实际记录为准，不预设上限。
- 结果按日期落 `tests/experiments/routing-replay/<date>/`，保留原始报告、缺陷清单、判定依据与「自述/事件」标注；结论写在该目录，不写回本协议。
