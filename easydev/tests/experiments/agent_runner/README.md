# 单宿主行为 Runner

`runner.py` 负责为固定行为场景准备隔离工作区、驱动 Codex CLI、采集事件流与代码补丁，并执行黑盒验收断言。

## 命令使用

```bash
# 查看支持的场景列表
python3 tests/experiments/agent_runner/runner.py list

# 准备场景隔离工作区（不调用模型）
python3 tests/experiments/agent_runner/runner.py prepare \
  --scenario public-contract-callers --out /tmp/easydev-behavior

# 运行场景回放（支持注入替身 CLI 与超时）
python3 tests/experiments/agent_runner/runner.py run \
  --scenario b02 --out /tmp/easydev-behavior \
  --model <host-model> --reasoning-effort high
```

`--codex-bin`、`--codex-auth` 与 `--agent-timeout` 用于注入外部命令或测试替身。

## 场景清单

| ID | 验证行为 | 核心断言 |
| --- | --- | --- |
| `b01` | 低风险单文件文案修改保持 quick | 只改 `src/ui.py`、不加载 references、不委派 reviewer、声明自查 |
| `b02` | 高风险对象所有权鉴权进入 full | 命中鉴权数据源审计、完成行为验收、存在已带回报告的独立 reviewer |
| `b16` | audit 只读审计 | 零 diff、实际读取目标文件、输出符合规范的机器可读发现块 |
| `b61` | normal 微闭环与转 full 路径 | normal 独立审查 1–2 次且无阻断项；报阻断项时按规转 full 且继承预算；均须带回报告 |
| `b67` | 代码质量工程判据 | 写盘失败异常向上传播、入口不重复校验、生成文件不手改、回退补丁回归测试红跑 |
| `b67-review` | 审查严重度定级 | 识别代价型 P3，不为纯风格改动报缺陷 |
| `public-contract-callers` | 公共契约改造与调用方适配 | 通过黑盒独立验收、未修改测试材料、具有独立 reviewer 审查记录 |

## 产物结构

```text
<out>/runs/<scenario>/easydev/r<n>/
├── workspace/          # 场景工作区，仅安装运行时最小 Skill
├── events.jsonl        # 原始事件记录
├── stderr.log
├── last-message.txt    # 最终交互消息
├── agent.patch         # 排除技能元数据后的代码改动补丁
└── result.json         # 状态、事件归因与场景验收判定
```

`result.json` 基于 `behavior-v1` 协议生成；只有 reviewer 状态为 `completed` 且带回有效内容才计为审查完成。

## 离线自测

```bash
python3 -m pytest tests/experiments/agent_runner -v
```

通过 `codex_stub.py` 替身全量覆盖 runner 的 CLI 生命周期、B16 发现块格式、安全边界防御（Skill 篡改、暂存拦截、超时进程清理）与事件校验。

## 局限与边界

- 仅提供 Codex CLI 适配层，断网要求依赖宿主沙箱能力。
- reviewer 完成与报告为行为证据，不替代最终任务语义的独立验收。
- 文件访问与元数据审计基于命令文本分析，非操作系统层只读挂载。
