# 0.3.1 抽测加载证据（从实例会话 toolCall 提取）

来源：主代理 Pi 会话目录中各实例 `session.jsonl`；只提取读取快照文件的 toolCall，不含对话正文。

| 用例 | 运行实例 ID | 读取 toolCall | 时间 | 模型/深度 |
| --- | --- | --- | --- | --- |
| BH-01B | `fb7f66f6-1086-4553-8831-5327196719dd` | `read` `call_00_xam8xw5ok5lb7n2zpne6sdsj` → `/tmp/easythinking-probe/SKILL.md` | 2026-10-24T10:10:35.464Z | magpie/opencode-go/deepseek-v4.1-flash |
| BH-01C | `137ee508-73ec-4f68-86d8-909a520d2740` | `read` `call_00_mh4nn4gcjwjbevt30vp6nctx` → `/tmp/easythinking-probe/SKILL.md` | 2026-10-24T10:09:12.196Z | magpie/opencode-go/deepseek-v4.1-flash |
| BH-01E-variant | `7135d846-5762-426e-a837-d313ad720345` | `read` `call_01_f2xoui0rycnp4i8paoqsj9om` → `/tmp/easythinking-probe/SKILL.md` | 2026-10-24T10:09:12.447Z | magpie/opencode-go/deepseek-v4.1-flash |
| BH-01H | `03870adb-31bc-4a64-9fe3-958782f56b95` | `read` `call_01_yfuv8wigfq1dy4tilusmg3sl` → `/tmp/easythinking-probe/SKILL.md` | 2026-10-24T10:09:12.009Z | magpie/opencode-go/deepseek-v4.1-flash |
| BH-01I | `4989bed3-4bf2-4b14-a0a8-37bd31820e22` | `read` `call_00_fu1swnpq1imdfupfs0wi9ev7` → `/tmp/easythinking-probe/SKILL.md` | 2026-10-24T10:09:12.041Z | magpie/opencode-go/deepseek-v4.1-flash |
| BH-01J | `fd353cb4-6e2a-4bb0-b0be-951bd193356f` | `read` `call_01_pub7vtmp3reaj2qbhpqed22g` → `/tmp/easythinking-probe/SKILL.md` | 2026-10-24T10:10:35.496Z | magpie/opencode-go/deepseek-v4.1-flash |
