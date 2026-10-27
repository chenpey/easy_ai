# easy_ai

本仓库包含 2 个 Skill：

| 名称 | 版本 | 简单说明 |
| --- | --- | --- |
| [EasyThink](easythink/README.md) | 0.4.0 | 理清问题、检查证据、比较选择并形成可验证的下一步。 |
| [EasyDev](easydev/README.md) | 1.2.7 | 面向编码 Agent，按风险组织设计、实施、排障、验证与审查。 |

## Harness 兼容性

`easydev/agents/openai.yaml` 和 `easythink/agents/openai.yaml` 是 OpenAI/Codex 专用的可选界面元数据，用于提供技能显示名称、简介和默认提示词。它们不是 Agent Skills 的必需文件，也不负责其他 harness 的兼容性；其他 harness 可以忽略这些文件。跨 harness 的核心是各技能目录中的 `SKILL.md`（包含规范要求的 `name` 和 `description`）及其引用的随包文件。实际安装和加载能力取决于目标 harness；通过 Skills CLI 安装时，`--agent '*'` 只覆盖该 CLI 当前支持的 harness。

## 全局规则（AGENTS.md）

运行时唯一事实源是 `~/.config/agent-rules/AGENTS.md`。仓库根 `AGENTS.md` 是可版本跟踪的副本，两边内容须保持一致，冲突时以仓库副本为准。

各 harness 不各存一份，而是用软链指向同一文件：现有的 Pi（`~/.pi/agent/AGENTS.md`）和 Codex（`~/.codex/AGENTS.md`）已是指向该文件的软链；Antigravity 的全局规则 `~/.gemini/config/AGENTS.md` 同样软链到 `~/.config/agent-rules/AGENTS.md`；其他支持 AGENTS.md 的 harness 按其约定的加载路径做同样的软链。不支持 AGENTS.md 的 harness 不在本仓库覆盖范围。

宿主专有规则不进 canonical，各自在仓库里跟踪再软链分发：Antigravity 的专有规则由 `agent-rules/antigravity.md` 跟踪，软链到 `~/.gemini/config/rules/antigravity.md`，该目录的 `*.md` 需带 YAML frontmatter（`trigger: always_on`）。canonical 只放跨 harness 的通用底线。

部署顺序：改仓库副本 → 提交 → 同步到通用位置 → 确认软链仍指向它。

```bash
set -e
cd /path/to/easy_ai
mkdir -p ~/.config/agent-rules ~/.pi/agent ~/.codex ~/.gemini/config/rules
cp AGENTS.md ~/.config/agent-rules/AGENTS.md
diff -q AGENTS.md ~/.config/agent-rules/AGENTS.md
ln -sfn ~/.config/agent-rules/AGENTS.md ~/.pi/agent/AGENTS.md
ln -sfn ~/.config/agent-rules/AGENTS.md ~/.codex/AGENTS.md
ln -sfn ~/.config/agent-rules/AGENTS.md ~/.gemini/config/AGENTS.md
ln -sfn "$PWD/agent-rules/antigravity.md" ~/.gemini/config/rules/antigravity.md
```

每次改完仓库副本都要重跑上面的块；块内 `set -e` 保证 `cp` 或校验失败时立即中止，不会把软链指到未写入的副本。
