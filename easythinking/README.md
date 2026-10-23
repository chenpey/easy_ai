# EasyThinking

EasyThinking 是一个通用思考辅助 Skill，帮助用户澄清问题、区分观察与推断、检查关键证据、比较方案，并提出可验证的下一步。简单问题直接回答；复杂度较高时按需分析，不强制套用固定模板。

它尊重用户的感受和价值偏好，不把推断写成事实，也不替用户做决定或扩展为未请求的调查与对外操作。编码、医学、法律、投资等专业任务仍应使用相应的专业流程。

## 安装

在仓库根目录运行：

```bash
npx skills add ./easythinking --skill easythinking
```

指定 Codex 和用户级安装时：

```bash
npx skills add ./easythinking --skill easythinking --agent codex --global
```

通过 Skills CLI 安装到它支持的所有 harness 时：

```bash
npx skills add ./easythinking --skill easythinking --agent '*'
```

安装后按宿主要求刷新技能或开启新会话。技能目录存在不代表宿主已加载它。

## 文件

```text
easythinking/
├── README.md
├── SKILL.md
├── agents/
│   └── openai.yaml
└── references/
    ├── examples.md
    └── methods.md
```

运行时说明位于 `SKILL.md`；`references/` 提供按需读取的方法和应用示例。关于 `agents/openai.yaml` 与跨 harness 兼容性的说明见仓库根目录 [README](../README.md#harness-兼容性)。
