---
trigger: always_on
description: "Antigravity 宿主工具语义：并发与轮询、上下文读取上限、终端沙箱与工作区外访问、临时产物位置、回复链接格式。"
---

# Antigravity 宿主适配规则

通用底线（正确性优先、暂停与确认、沙箱、安全与不可信内容、Git 与提交、最终回复最低要求）见 `~/.config/agent-rules/AGENTS.md`。本文件只放 Antigravity 特有、canonical 不覆盖的工具语义，不重复通用规则。

## 1. 往返与并发

- 每次模型调用都要重新支付首 token 延迟与完整上下文开销，纯工具轮占大头；不影响正确性的前提下减少往返。
- 动手前先列清所需信息，把互相独立的读取与检查合并到单轮；有真实依赖时才顺序执行。写操作（`write_file`/`edit_file`）、同一文件的多处修改、依赖前一步结果的步骤一律串行；同一文件紧邻的修改合并为单次 `edit_file`。
- `run_command` 每次调用都在独立的新 shell 会话中执行（`cd` 与环境变量不跨调用保留）；有上下文依赖的连续命令合并在同一条 `CommandLine` 中一次执行（如 `cd pkg && npm test`）。
- 异步任务与子代理（`invoke_subagent`、`schedule`、异步 `run_command`）具备响应式唤醒：启动后系统会通知结果，**严禁轮询**（不循环调用 `manage_task`/`manage_subagents`，也不用 shell `sleep` 等待）。

## 2. 上下文读取

- 大文件用 `read_file` 的 `StartLine`/`EndLine` 分段读取（单次上限 800 行或 46,080 字节），或先用 `rg` 及搜索工具定位行号；不把整份日志、整棵目录树塞进主上下文。
- 跨大量文件的探索性调研，在未激活明确限定子代理角色与分派门控的技能（如 easydev）时，优先委派只读 `research` 子代理，只把结论带回主会话；激活此类技能时按其角色与计量规则执行。
- 收敛不能丢关键信息：判断依据被截断时用 `StartLine`/`EndLine` 或 `ByteOffset` 针对性补读，不凭猜测继续。

## 3. 终端沙箱与工作区外访问

- `run_command` 受终端沙箱与命令策略限制：受限模式下通过 shell 子进程（`cat`/`ls`/`python`）访问工作区外路径（如 `$HOME`）可能直接报 `Operation not permitted` 且不触发授权提示。
- 需要读写工作区外文件（如 `~/.gemini`）时优先用 `read_file`/`write_file`/`edit_file`（由拦截层按 Non-Workspace File Access 策略提示授权），不用 shell 反复试探。
- 一次性调试脚本与临时测试数据（不含凭证）放在会话专属 `<appDataDir>/brain/<conversation-id>/scratch/`（无活跃工作区的新建项目放 `~/.gemini/antigravity/scratch/`），不写入 `/tmp`、`~/.gemini` 根目录或桌面；含凭证的基线快照按安全规则放权限受控（`0700`/`0600`）的私有临时目录，不放进会被展示或同步的 artifact 目录。

## 4. 回复里的文件引用

- 提及文件与代码符号（函数、类、类型）时附 `file://` 可点击链接，如 `[filename](file:///abs/path/to/file#L10-L20)`。
