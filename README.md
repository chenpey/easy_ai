# easy_ai

本仓库采用类似 `easy_suite` 的子项目结构，EasyDev 源码、文档和维护材料位于 [`easydev/`](easydev/)。

## 子项目

| 名称 | 说明 | 版本 | 文档 |
| --- | --- | --- | --- |
| EasyDev | 面向编码 Agent 的流程技能，包含设计、实现、排障、审查、审计规程及离线维护用例 | 0.11.0 | [easydev/README.md](easydev/README.md) |

## 使用

从仓库根目录安装 EasyDev 技能：

```bash
npx skills add ./easydev --skill easydev
```

也可以在支持的 Agent 中直接调用 `$easydev`。具体用法、安装选项和维护检查见 [EasyDev 文档](easydev/README.md)。

## 仓库结构

```text
easy_ai/
├── .gitignore
├── README.md
└── easydev/
    ├── README.md
    ├── CHANGELOG.md
    ├── SKILL.md
    ├── agents/
    ├── examples/
    ├── references/
    └── tests/
```
