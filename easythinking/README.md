# EasyThinking

版本：`0.2.5`

帮助你理清问题、检查证据、比较选择，并找到可验证的下一步。简单问题直接回答，复杂问题按需分析，不套固定模板，也不替你做决定。

## 安装

### 方式一：交给 Agent 安装（快捷）

将下面的提示词复制给你使用的 Agent，让它完成安装或更新：

```text
请安装或更新 EasyThinking：
https://github.com/chenpey/easy_ai/tree/main/easythinking

识别当前操作系统和 Agent，将仓库 main 分支的最新技能文件保存到用户主目录下的 ~/.agents/skills/easythinking。
只安装 easythinking，保留 SKILL.md 及其引用文件的相对位置。

如果当前 Agent 支持该通用目录，直接使用；否则在其实际配置的用户级技能目录中创建指向上述目录的软链接，让所有项目都能使用。确认 Agent 能读取该链接；若不支持软链接，可使用受支持的目录联接或复制，并说明采用的方式。

优先使用已有工具，避免安装不必要的依赖。更新前检查已有文件和链接，保留本地修改及自建文件；仅替换确认未被本地修改的旧版技能文件，并处理新版已移除的文件。遇到冲突先说明，不直接覆盖或删除。

完成后检查技能及引用文件是否完整、链接是否有效，告知安装路径、版本和生效步骤。未实际验证 Agent 加载时，请明确说明。
```

### 方式二：手动安装（确定性对照）

如果需要稳定、可复现的安装，可在终端执行以下最简命令：

#### 1. 通用目录（Codex、Cursor、OpenCode、Pi、DeepSeek Harness 等共用）

```bash
mkdir -p ~/.agents/skills
git clone --depth=1 https://github.com/chenpey/easy_ai.git /tmp/easy_ai_repo
rm -rf ~/.agents/skills/easythinking
cp -R /tmp/easy_ai_repo/easythinking ~/.agents/skills/
rm -rf /tmp/easy_ai_repo
```

#### 2. 专用目录

- **Claude Code**（官方仅读取 `.claude/skills/`）：
  ```bash
  mkdir -p ~/.claude/skills
  cp -R ~/.agents/skills/easythinking ~/.claude/skills/
  ```

- **Google Antigravity**（官方用户级配置）：
  ```bash
  mkdir -p ~/.gemini/config/skills ~/.gemini/antigravity-cli/skills
  cp -R ~/.agents/skills/easythinking ~/.gemini/config/skills/
  cp -R ~/.agents/skills/easythinking ~/.gemini/antigravity-cli/skills/
  ```

## 使用

直接告诉 agent：

> 用 easythinking 帮我理清这个问题：……

例如：“这个判断靠谱吗？”“这两个方案怎么选？”“我是不是漏了什么关键证据？”“同事总针对我，我是不是想多了？”

也可用快捷指令：Codex 输入 `$easythinking`，Pi 输入 `/skill:easythinking`。

编码、医学、法律、投资等专业问题，可用它辅助梳理思路，但仍需专业验证。涉及重大不可逆决策或现实人身风险时，严格遵守安全优先与风险提示规则。

## 思考方法

![EasyThinking 思考脑图：明确问题、梳理信息、概念与尺度、证据与推理、判断与取舍、行动与更新](assets/easythinking-mindmap.svg)

各分支按需使用，无需每次走完。更多内容见[方法说明](references/methods.md)、[应用示例](references/examples.md)、[行为用例](tests/behavior.md)和[更新日志](CHANGELOG.md)。

## 方法来源与致谢

EasyThinking 的起点来自知乎作者“啥都不明白”的[回答](https://www.zhihu.com/question/9115866072/answer/2096187142615590644)，提炼其关于事实梳理、概念定义和程度澄清的思路。在此基础上，本技能增补了论证结构分解、证据分级、感受与价值保留、安全优先、不可逆高风险表态、多候选解释、风险偏好与承受力区分、检索默认核查及行动反馈闭环。
