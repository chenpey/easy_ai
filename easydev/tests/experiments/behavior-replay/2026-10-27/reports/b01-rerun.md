### 任务概述与处理路径
- **处理路径**：`quick`
- **路径依据**：本次改动为局部、明确、低风险的文案变更，仅在 [ui.py](file:///private/tmp/easydev-b01-rerun/runs/b01/easydev/r1/workspace/src/ui.py) 中将按钮文本常量由 `"提交"` 改为 `"保存"`，无破坏性契约、多调用方依赖或安全敏感风险。

### 代码改动
- [ui.py](file:///private/tmp/easydev-b01-rerun/runs/b01/easydev/r1/workspace/src/ui.py)：将 `BUTTON_LABEL` 从 `"提交"` 修改为 `"保存"`。未修改或引入任何测试文件与测试框架。

### 实际验证
1. **变更范围检查**：执行 `git diff` 确认仅有目标常量的单行文案变更。
2. **逻辑与语法校验**：在工作区执行 `python3 -c "import src.ui; assert src.ui.BUTTON_LABEL == '保存'"` 成功通过，确认模块解析无误且常量值更新正确。

审查状态：自查