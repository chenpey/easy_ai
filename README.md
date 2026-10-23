# easy_ai

本仓库包含 2 个 Skill：

| 名称 | 版本 | 简单说明 |
| --- | --- | --- |
| [EasyThinking](easythinking/README.md) | 0.2.0 | 理清问题、检查证据、比较选择并形成可验证的下一步。 |
| [EasyDev](easydev/README.md) | 0.11.0 | 面向编码 Agent，按风险组织设计、实施、排障、验证与审查。 |

## Harness 兼容性

`easydev/agents/openai.yaml` 和 `easythinking/agents/openai.yaml` 是 OpenAI/Codex 专用的可选界面元数据，用于提供技能显示名称、简介和默认提示词。它们不是 Agent Skills 的必需文件，也不负责其他 harness 的兼容性；其他 harness 可以忽略这些文件。跨 harness 的核心是各技能目录中的 `SKILL.md`（包含规范要求的 `name` 和 `description`）及其引用的随包文件。实际安装和加载能力取决于目标 harness；通过 Skills CLI 安装时，`--agent '*'` 只覆盖该 CLI 当前支持的 harness。
