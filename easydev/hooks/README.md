# EasyDev Hooks（可选 Plugin）

本目录是一个**独立可选 Plugin**，不属于 EasyDev Skill 的安装集。Skill 仍然只安装 `SKILL.md`、`references/` 与可选 `agents/`；不安装本目录时，EasyDev 的分流、预算与规则完全照旧。

Plugin 的目标是把可观测、可机器判断的安全与交接检查下沉到宿主生命周期事件，不建立第二套流程编排器，也不替代人工风险判断。

## 不做什么

- 不在 `SessionStart` 注入任何 EasyDev 规则文本，也不自动创建任务目录。
- 不自动定档（`quick` / `normal` / `full`），不解析自然语言发现，不维护 `d`/`r` 或修复次数。
- 不写业务文件；需要内容基线的目标由 Hook 在写入前自动快照到私有状态根（`<state_root>/baselines/…`）。那个快照是**私有缓存，不是业务备份**，也不替代 [基线恢复](../references/baseline-safety.md) 的手工恢复流程。
- 不用 `overwrite` 改写交接 prompt；缺项只 `deny` 并提示补齐。
- 不提供操作系统级沙箱；同 UID 进程仍可替换文件或 Hook 程序。

## 目录

| 文件 | 作用 |
| --- | --- |
| `core.py` | CLI、输入校验、宿主归一化、规则顺序、响应编码；支持 `probe` / `enforce` |
| `state.py` | 私有状态目录、有限锁、原子写、安全打开 |
| `shell.py` | 有限 shell 分段、包装解包、Git 与读写命令识别；永不执行被检命令 |
| `paths.py` | 仓库根、路径/patch 提取、基线只读核对 |
| `models.py` | 唯一模型 schema/validator；`scripts/check.sh` 与运行时共用 |
| `transcripts.py` | 宿主 transcript 的只读读取归因 |
| `install.py` | 生成配置、显式 `apply` 合并、卸载、清理旧版本生成目录（`prune`） |
| `pi/index.ts`、`index.ts` | pi 薄适配器与生成入口 |
| `templates/` | Antigravity / Codex / DSH 原生事件注册模板 |
| `manifest.json` | Plugin 名、release、内部 protocol 与分发文件清单 |

Plugin 分发不包含 `tests/` 与 Skill 文件；安装器也不会安装它们。

## 安装（默认只生成，不碰宿主配置）

先人工把本目录复制到一个受信位置，然后用同目录 `install.py` 生成配置。插件根推荐用**固定路径**（升级就替换文件，路径不变）；pi 的插件根同时是扩展目录，必须固定为 `~/.pi/agent/extensions/easydev-hooks`：

```bash
python3 ~/.local/share/easydev-hooks/install.py generate \
  --host codex \
  --plugin-root ~/.local/share/easydev-hooks \
  --skill-root ~/.agents/skills/easydev \
  --output-root ~/.local/share/easydev-hooks/config
```

`--output-root <root>` 把生成物固定写到 `<root>/<host>/`（推荐）：同一路径重新生成只覆盖本插件自己的文件，升级不再堆积 `<root>/<X.Y.Z>/` 版本目录。`--output <dir>` 仍保留，用于显式指定一个独立生成目录（两者互斥）。生成物包含：

- `<host>-hooks.json`：可直接合并进对应宿主的 hooks 配置；
- `install-context.json`：Python 核心需要的绝对路径、Host、模式、Skill 根与**绝对解释器路径**（`python` 字段）；
- `install-manifest.json`：本次安装的文件清单、逐条目身份/摘要与命令，`apply`/`uninstall` 只按它匹配。

`--host pi` 时生成器额外把同一份 `install-context.json` 写到 `<plugin-root>/install-context.json`：pi 扩展默认从该位置读取，复制完整 Plugin 树后即已接入。若把扩展单独放到别处，用 `EASYDEV_HOOKS_CONTEXT=<abs-install-context.json>` 指过去（`EASYDEV_HOOKS_PYTHON` 可覆盖解释器）。

解释器、`core.py` 与 context 路径都会逐项按 POSIX shell 规则引用；pi 走 `execFile`/`spawn` 的 argv，不经过 shell，并默认使用生成物记录的绝对解释器路径而不是 PATH 里的 `python3`。生成物含本机绝对路径，不能直接提交为可搬迁的发布配置。

**默认 `enforce`**：不带 `--mode` 就是真正拦截（危险 Git、reviewer 写、脏目标、模型配置与交接缺项等）；`--mode probe` 只记录事件名、工具名、字段名/类型、能力缺项和判定代码，不记录 prompt、命令、补丁、路径正文、工具结果或凭据，适合首次接入尚未真实 probe 的宿主。模式写在生成命令与 `install-context.json` 里：**切换模式后必须重新 `apply`**，否则宿主仍用旧命令。

### 升级与清理

升级分两步：用新版本替换插件根的文件，再用同一 `--output-root` 重新生成。命令路径不变时 `apply` 报告 `changed=false`，无需重跑；只有 `--mode`、解释器或插件根变了才需要重新 `apply`。

若仍按版本号目录保存插件副本（如 `~/.local/share/easydev-hooks/1.4.4/`），或历史上用过 `<root>/<X.Y.Z>/` 布局，旧目录可显式清理：

```bash
# 默认 dry-run，只列出可清理项；加 --apply 才删除
python3 ~/.local/share/easydev-hooks/install.py prune \
  --root ~/.local/share/easydev-hooks/config
# 需要留一两个版本比对时
python3 ~/.local/share/easydev-hooks/install.py prune \
  --root ~/.local/share/easydev-hooks/config --keep 1.4.2 --apply
```

`prune` 只删“完全由本插件生成”的版本号目录：目录名必须是 `X.Y.Z`、每个宿主子目录必须带 `name`/`release` 对得上的 `install-manifest.json`，且不能有未知文件、未知子目录或任何软链；不满足就跳过并列原因。删除后该版本的 `uninstall` 清单不再可用——若旧版本仍 apply 在某个宿主配置里，先用旧清单卸载或用当前版本重新 `apply` 覆盖。

### 合并到宿主

`apply` 必须显式指定目标真实文件；它在任何备份/写入之前逐层核验目标父链（`dir_fd + O_NOFOLLOW`，目标或任一祖先目录是软链、目标非普通文件或存在硬链都直接拒绝且不留备份），再在已打开的目录内保留原字节备份（`0600`）并原子写入；按生成的 `install-manifest.json` 匹配条目；`probe` → `enforce` 重新生成后**替换**上一代条目而不是追加两套；用户改过的条目保留并报冲突（非零退出），不覆盖。合并按 Hook 组进行：Codex/DSH 操作顶层 `hooks`，Antigravity 只操作命名组 `easydev-hooks`（不存在则新建），其他命名组与无关配置原样保留；卸载只移除仍与清单一致的条目，命名组清空后整组删除。

```bash
# 默认用法：清单从 --source 同目录的 install-manifest.json 读取
python3 ~/.local/share/easydev-hooks/install.py apply \
  --target <repo>/.codex/hooks.json \
  --source ~/.local/share/easydev-hooks/config/codex/codex-hooks.json

# 清单不在默认位置时显式指定
python3 ~/.local/share/easydev-hooks/install.py apply \
  --target <repo>/.codex/hooks.json \
  --manifest ~/.local/share/easydev-hooks/config/codex/install-manifest.json
```

| 宿主 | 挂载 | 生效与能力差异 |
| --- | --- | --- |
| Antigravity | 把生成配置里的命名 Hook 组 `easydev-hooks` 写入 workspace `.agents/hooks.json`，或用户明确选择的 `~/.gemini/config/hooks.json`；可整个组拷贝，也可与自建组共存 | 组内每个事件槽位的命令自带 `--event <EventName>`（真实 stdin **不带**事件名，也不带 `hookEventName`）；事件数组按官方区分两种结构：工具事件 `PreToolUse`/`PostToolUse` 是 Grouped（`[{"matcher": "*", "hooks": [{"type": "command", …}]}]`），`SessionStart`/`PreInvocation`/`Stop` 等非工具事件是 Flat（`[{"type": "command", …}]`，包一层就读不到 `command`）；公共字段为 `workspacePaths` 数组（也兼容 `workspacePath` 字符串），PreToolUse 放行必须回 `{"decision": "allow"}`；工具事件的 `toolCall` 内没有 `id`、`PostToolUse` 没有 `status`：顶层整型 `stepIdx` 作为回合键并合成工具 ID `step:<stepIdx>`（同一步内的多个调用共用该 ID，写配对按 ID 覆盖），失败与否只看顶层 `error`（非空字符串 = 失败，缺失或空串 = 成功）；PreInvocation/PreToolUse/PostToolUse/Stop 可观测；原生 `transcript.jsonl` 可按 `PLANNER_RESPONSE` + 后续 `GENERIC` 结果行归因读取；子代理关联需要父 `invoke_subagent` 与 child conversationId 的实证；`stop_hook_active` 不可靠 |
| Codex | 合并至 `<repo>/.codex/hooks.json` 或 `~/.codex/hooks.json` | 非 managed hooks 需在 `/hooks` 信任后才生效；子 Hook 的 `session_id` 是父 session，需用 `agent_id`/child 事件区分 actor；`transcript_path` 可能为 null |
| DSH | 生成 Codex 五事件配置，用户在现有 profile 的 `cordis.patch.yml` 挂载官方桥接包，`config.configPath` 指向生成文件，`defaultTimeoutMs` 设 2500 | 无自动发现、无 transcript；非 shell 参数经常只有 `{command:""}`，此时只能告警放行，不能声称已核对写入路径；默认无父 ID，不宣称支持 child 写后的父 Stop 提醒 |
| pi | 把完整 Plugin 树复制到 `~/.pi/agent/extensions/easydev-hooks/`，再以该目录为 `--plugin-root` 执行 `generate --host pi`（把 `install-context.json` 与绝对解释器路径写到扩展读取位置）；或一次性 `pi --extension <plugin>/pi/index.ts` 并用 `EASYDEV_HOOKS_CONTEXT` 指向生成目录 | `input`/`tool_call`/`tool_result`/`agent_before_settle`；`PI_SUBAGENT_CHILD=1` 与 `PI_SUBAGENT_PARENT_SESSION` 证明子代理与父会话 |

Windows：本版只声明 POSIX 安装受支持；Windows 可以运行纯解析单测，但 ACL、PowerShell 与命令引用未验证，权限保护与 PowerShell 规则降级告警。Antigravity/DSH 的原生 JSON 结构仍需各自 probe 夹具锁定；未经真实 probe 的宿主只能标“离线兼容，运行未验证”。

## 激活与角色

Hook 默认对所有会话生效，因此每个入口先做可信激活判定，未激活一律放行。**作用域**：状态与激活按**仓库（含 worktree）分区**，同一 session 跨仓库或 worktree 时，需要在目标仓库各自激活；跨仓库不继承已激活状态，也不继承读写保护。

0. **未激活 O(1) 短路**：`handle()` 最入口只读**本事件所属仓库分区**里的 actor 状态（`<state_root>/actors/<hash(host, session, actor)>.json`，一个 0600 小 JSON）。分区用廉价仓库根定位：从 tool cwd（无则 record.cwd）逐级向上找 `.git` 目录/文件，不启动 Git 子进程；无 cwd，或 cwd 目录下找不到仓库根（与完整判定的 `unbound` 分区不一致）时直接走完整判定。actor 明确为未激活时不调 Git、不 `_prepare_write`、不建锁、不写状态、不刷 post 告警，结果仍按宿主协议编码（Antigravity PreToolUse `{"decision": "allow"}`、DSH PreToolUse `{"permissionDecision": "allow"}`、Codex/pi 各自契约）；`input`、`child_start`、读取精确 `SKILL.md` 的 post 与 probe 模式从不短路。首次事件（含带 `transcript_path` 的真实首回合）走一次完整判定并写下只含 `activation_scope: "inactive"` 的锚点，之后才是常数开销；状态不可读时退回完整判定并按既有 fail-open 告警放行。同一 transcript 记录的去重**只在已激活（`task_active`/`role_only`）时生效**：未激活会话在同一用户回合里先读 `SKILL.md`、随后的 `PreInvocation`/`UserPromptSubmit` 仍能激活，不会被同一条 `USER_INPUT` 记录键挡住。因为分区与权威 actor 状态一致，同一会话在另一 Git 仓库的未激活判定不会掩盖本仓库的激活状态，也没有需要失效处理的全局索引。

1. 原始用户 prompt 明确调用 `$easydev`、`/skill:easydev`、`使用 easydev`、`use easydev`（排除围栏代码与引用行）；
2. 成功读取精确安装路径的 `SKILL.md`（PreToolUse 放行，PostToolUse 确认成功后才记证）；
3. 可读 transcript 中的上述证据；
4. 当前 session+actor 的有效私有状态；
5. 已激活父任务的可信标准角色交接（角色声明 + 精确 `references/<role>-role.md` + reviewer 机器模板）。

父子关联只按可信分派证据建立：父分派 pre 记录分派 ID、初始任务摘要、父 session 与 task epoch（只存摘要，不存原文）；`SubagentStart`/子初始 input 用可信 child ID 或唯一初始任务摘要匹配该记录并**固定 epoch**。匹配缺失/歧义、或该 epoch 已随父任务切换/`active=false` 退役时，子只保留本 actor 的 `role_only`，不写父任务的写序列、基线与审查关系，并告警披露。root 状态键含 host+root session+root actor，同一 actor ID 的不同 session 互不共享状态。

无父关联的完整标准 reviewer 交接只授予本 actor 的 `role_only`：仅执行 reviewer 写拒绝与可证 child 的禁嵌套，不借用父任务的基线、模型或 P3。未激活的 child 成功读取精确 `SKILL.md` 也只做本 actor 的局部激活（`task_active` 但无父任务绑定，`role_source: skill_read`），随后它自己的嵌套分派被拒绝；这条激活不建父任务/新 epoch，失败、只有 requested/结果未知、其他路径的同名文件都不激活，普通未激活 child 的 post 与根 actor 的未激活 post 仍直接放行且不刷告警。该局部激活与根路径共享“同 actor、同工具调用 ID 只能作为一次激活证据”：首次接受时把调用 ID/digest/epoch 写进 actor 级 `consumed_read_ids`/`consumed_reads`（不写任务缓存、不建父任务/epoch），已 `task_active` 的 child 成功读取也补记该证据但不重复激活；因此显式 `active=false` 之后重投同 actor、同调用 ID 的成功读取（同参数/同结果，或改参数/改 `success`↔`completed` 拼写）都保持 `inactive`、嵌套放行，只有新的调用 ID 的成功读取才重新启用本 actor 的禁嵌套约束。仅阅读角色文件不会把主代理改成 reviewer。

可选机器上下文：原始 user/可信父交接中单独一个 `easydev-hook` JSON 围栏，字段限 `version:1, active, role, child, task_root, models_file, role_overrides, code_review`。`active=true` 才是激活证据；`active=false` 只用于真实 user 输入停用当前增强。Prompt 里自动注入的系统文本或被审文件里的同名块不算。

## 规则与降级

规则顺序：解析 → 可信激活/角色 → 破坏性 Git → reviewer 只读 → 结构化目标基线 → 分派配置/交接/嵌套 → 状态事件 → 编码。

- **P1 Git**：`reset --hard`、`checkout --`/`-f`、操作数确指已存在路径的 `checkout`（`git checkout src/app.py` 会还原工作区文件）、会写工作区的 `restore`、`clean -f`、`stash` 写操作、强推与带 `+` 的强制 refspec（`+main:main`，也含无冒号的 `+main`）一律 `deny`；包装（`bash -lc`、`sudo`、`env`）、`-C` 与多段命令会逐段检查。`checkout` 的路径操作数只按已知选项布局判定（`.`/`..`、绝对路径、可信 cwd 下确实存在的相对路径），未知选项布局不猜。Git alias、脚本或动态拼接不展开，文档不宣称无绕过。
- **P1 基线**：目标已有 dirty/untracked/不可重建内容时，Hook 在 `PreToolUse`（task 锁外）把**首次**当前内容自动快照到 `<state_root>/baselines/<task_key>/<repo_id>/<仓库相对路径>`：目录 `0700`、文件 `0600`、非软链、逐层 `dir_fd + O_NOFOLLOW`、`os.link` 发布不覆盖，第二次写不覆盖首次快照。快照成功即登记并放行；源文件非普通文件（FIFO/设备/目录）、软链、不可读或超过 8 MiB 则 `deny` 并给出可操作原因；`clean_tracked`/`absent` 不要求快照；成功 post 后再被外部修改仍 `deny external_modification`。显式 `EASYDEV_TASK_ROOT`（或安装上下文/机器上下文 `task_root`）仍优先旧的手工核对路径：只读核对 `baseline/<仓库相对路径>` 存在、非软链、`0600`、无硬链，缺失或不合规即 `deny`，已知旧任务根不能给需要内容基线的新任务复用。任务根、`baseline/` 与备份父链都从祖先链起逐层 `dir_fd + O_NOFOLLOW` 打开核对；业务文件或其仓库内父链是软链时 `deny`（不 realpath 后当普通路径），也不写软链目标。自动快照是私有缓存，不是业务备份：Hook 不写业务文件。
- **P1 reviewer 只读**：写工具/补丁和明确写命令 `deny`，含 `find -delete`/`find -exec`、`sort -o <文件>` 这类显式写副作用的命令；纯读、测试、构建放行。**窄拒绝**：已识别出复合结构但拆解失败（无法安全配对/归属）时 `deny`，并提示把命令拆成简单命令重跑；其他无法完整解析的命令仍告警放行并披露缺口。
- **P2 模型配置**：只读第一个存在的完整文件（对话指定 → 项目 `.easydev/models.json` → 用户配置 → 内置 inherit），不跨文件合并；选中文件无效即 `deny`，不静默回退。`role_overrides` 先按字段校验，再合成后走同一 validator。
- **P2 交接/嵌套**：reviewer 交接必须有精确角色路径与 `**[P级] 简短标题**`/`**[P级别] …**`/逐个 P0–P3 模板；缺项 `deny` 并提示补齐，不改写 prompt。已证明的 child 不能再次分派；状态查询/等待放行。
- **P3 收口**：只有根 actor 正常 settle 时判定；可信 child 的成功写直接进入共享写序列，完成时 `checkpoint=max(checkpoint,start_write_seq)`；审后又写代码则每回合最多提醒一次。`.md` 不按扩展名排除；只排除显式 `doc_paths` 与任务根内明确报告路径（`reports/`、`baseline/`、`report*`/`review*` 命名），运行规则/构建清单/测试/未知配置一律计入，task_root 与 repo 重叠时不据此排除整个 repo。完成事件只认结构化成功终态 + 结构化报告，显式 `failed`/`cancelled`/`timeout`/`running` 优先排除；Codex 用 `SubagentStop` 的 `agent_id` 关联分派、`last_assistant_message` 作为结构化报告，不解析报告正文或最终散文。写入配对键为 actor + task epoch + 调用 ID 并核对目标集合，父子同名工具 ID 互不覆盖。

| 宿主 | P3 完成事件 | 说明 |
| --- | --- | --- |
| Codex | 启用 | 模板注册 `SubagentStart`/`SubagentStop`；按官方子 Hook 契约（`session_id` 是父 session、`agent_id` 是 child）用父 session + `agent_id` 关联可信分派，`last_assistant_message` 作为结构化报告；无法关联时只留 `role_only` 并告警 |
| Antigravity / DSH / pi | 不启用（能力降级） | 可加载 `Stop`，但无法可靠归因“审查完成”后写入 → `has_completed_code_review` 不置位，settle 不续跑并披露 `P3` 能力缺口，不虚报已支持 |

**故障开路**：输入超限、非 JSON、未知事件、无 cwd/会话、transcript 不可读、锁忙、状态损坏、Git 查询失败与内部异常都会告警并放行（高危 Git 一旦已解析命中，不因后续状态写失败而放行）。运行期 Plugin 的 fail-open 不适用于回放 Harness 的专用读取守卫。

## 状态与隐私

默认状态根为 `<系统临时根>/easydev-hooks-<uid>/v1/<repo-id>/`，也可由 `install-context.json` 的 `state_root` 显式选择同等私有根。目录 `0700`、文件 `0600`、原子写、非阻塞有限锁。**JSON 状态不存 prompt、命令或补丁；自动快照 `baselines/` 仅存业务原内容，私有 0600（目录 0700），不对外发送。** 自动快照只写 `<state_root>/baselines/<task_key>/<repo_id>/<相对路径>`，属私有缓存而非业务备份。状态是缓存，不是任务账本、授权库或业务备份。只规范化已核实的系统临时根别名（如 macOS `/tmp` → `/private/tmp`）一次；私有根以下逐层 `dir_fd + O_DIRECTORY|O_NOFOLLOW` 打开并 `fstat` 核对 owner/0700，祖先软链一律不可用（告警放行，不修复、不删除）。一次调用的 1500ms 内部截止为 Git、指纹、transcript 与遍历共享；状态、baseline、快照、models 与 transcript 的打开统一 nofollow + nonblock，`fstat` 普通文件后才做有界读取（FIFO/设备/目录/超大文件降级或拒绝，不阻塞）。Git 查询、指纹、分派前的仓库/models 读取、input 的 transcript 取证与自动快照都在 task 锁外完成；持锁后只重新计算 root/task/epoch 身份并用锁外已取证的签名复核，锁内不启动 Git、不做配置全文读取、不重新读取或解析 transcript、不做大文件 I/O，签名变化即丢弃取证/预取并降级、不向旧 task 提交。

## 卸载

```bash
python3 ~/.local/share/easydev-hooks/install.py uninstall \
  --target <repo>/.codex/hooks.json \
  --manifest ~/.local/share/easydev-hooks/config/codex/install-manifest.json
```

只移除仍与清单摘要一致的条目：用户改过的条目与其他 hooks 原样保留，重复卸载幂等（第二次 `changed=false`）；含空格/单引号/`$`/反引号的 Plugin 根也能识别。随后按宿主要求重载/重启。回滚顺序：先解除宿主注册（pi 移除扩展目录后 `/reload`），再删除独立 Plugin 安装目录。不要只删 Python 核心而留下仍加载的 TS 扩展。任务目录、`baseline/`、自动快照与 Hook 私有状态默认保留；确认无需恢复后再由用户清理，不删除未知 owner 或链接目标。

## 已知未验证项

本版未在真实宿主上安装或运行：四宿主的 Hook 信任、真实 stdin 结构、输出消费与 Stop 续跑都需宿主端到端 probe；Antigravity 的命名组结构、工具事件 Grouped / 非工具事件 Flat、`--event`、`workspacePaths`、`PostToolUse` 的 `error`/整型 `stepIdx`、`{"decision": "allow"}` 与 DSH 的 `{"permissionDecision": "allow"}` 只有离线夹具与官方文档核对覆盖，**未在真实 Antigravity/DSH 进程执行 `--mode probe`**；DSH 本机无 CLI，只能给离线夹具与安装文档；pi/Antigravity/DSH 的子代理完成结果没有稳定 selector，P3 完成事件明确不启用；pi 适配器已用 Node 夹具（假的 `ExtensionAPI` + 真实 `child_process` argv）验证“复制 Plugin 树 + generate → 真实核心的 deny”。Antigravity 原生 transcript 归因已用本机 13 个真实 `transcript.jsonl` 做只读实测（不打印正文）：修正前 `references` 恒空、`confirmed` 全 false，修正后 8 个会话取得 `references`、6 个取得 `SKILL.md` 确认读取偏移；这只证明解析适配真实日志形状，不证明桌面端已装 Hook 或端到端拦截生效。P0 回放守卫的进程无法启动或被强杀时不能保证 deny 输出，须停止回放并标 `valid_invocation=false`。所有离线通过不等于四宿主强隔离保证。
