# 模型配置示例

[models.cross-provider.json](models.cross-provider.json) 是角色配置起点，默认全部 inherit，可按当前宿主支持的模型与深度分别填写 developer、reviewer、designer；upgrades 默认空数组。文件名保留以兼容已有引用，不代表固定使用不同供应商。

示例不会自动加载；复制到选定位置或在对话中明确指定。来源优先级、字段和模型升级只以 [模型配置](../references/models.md) 为准。能力说明不作为虚构模型 ID，实际值须核对宿主工具；不修改密钥或宿主配置。
