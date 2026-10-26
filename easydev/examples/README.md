# 模型配置示例

[models.cross-provider.json](models.cross-provider.json) 是角色配置示例，演示跨供应商写法（开发与审查分属不同供应商，`upgrades` 再对调）。示例里的模型 ID 是 `<provider-a>/<model-a>` 这类占位值，只演示保留供应商前缀的写法，既不是真实 ID 也不构成供应商推荐，必须替换为宿主实际支持的值；照抄会按 [模型配置](../references/models.md) 在第一次分派时暂停报受阻，不会回落到 `inherit`。文件名沿用历史名称。

内置默认已是三个角色都 `inherit`、`upgrades` 为空，不另放全 `inherit` 的示例文件：它与默认值语义重复，无自动校验时还会与默认值漂移。

示例不会自动加载；复制到选定位置或在对话中明确指定。来源优先级、字段和模型升级只以 [模型配置](../references/models.md) 为准。文档与能力说明不写未经核对的虚构模型 ID，实际值须核对宿主工具；不修改密钥或宿主配置。
