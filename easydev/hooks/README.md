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

`--host pi` 时生成器额外把同一份 `install-context.json` 写到 `<plugin-root>/install-context.json`：pi 扩展默认从该位置读取，复制完整 Plugin 树后即已接入。若把扩展单独放到别处，用 `EASYDEV_HOOKS_CONTEXT=<abs-install-context.json>` 指过去（`EASYDEV_HOOKS_PYTHON` 可覆盖解释器）。升级替换整套 Plugin 新文件后需 `/reload` 重载 TS 适配器（固定诊断类别表在 TS 里）；只有路径、模式或解释器变化才需要重新 `apply`。

解释器、`core.py` 与 context 路径都会逐项按 POSIX shell 规则引用；pi 走 `execFile`/`spawn` 的 argv，不经过 shell，并默认使用生成物记录的绝对解释器路径而不是 PATH 里的 `python3`。生成物含本机绝对路径，不能直接提交为可搬迁的发布配置。

**默认 `enforce`**：不带 `--mode` 就是真正拦截（危险 Git、reviewer 写、脏目标、模型配置与交接缺项等）；`--mode probe` 只记录事件名、工具名、字段名/类型、能力缺项和判定代码，不记录 prompt、命令、补丁、路径正文、工具结果或凭据，适合首次接入尚未真实 probe 的宿主。模式写在生成命令与 `install-context.json` 里：**切换模式后必须重新 `apply`**，否则宿主仍用旧命令。

### 升级与清理

升级分两步：用新版本替换插件根的文件，再用同一 `--output-root` 重新生成。命令路径不变时 `apply` 报告 `changed=false`，无需重跑；只有 `--mode`、解释器或插件根变了才需要重新 `apply`。pi 还需 `/reload`（或重启）重载扩展：固定诊断类别在 TS 适配器里，只替换 Python 核心而不重载会暂时仍显示旧的隐藏类别，不会自动生效。

若仍按版本号目录保存插件副本（如 `~/.local/share/easydev-hooks/1.4.7/`），或历史上用过 `<root>/<X.Y.Z>/` 布局，旧目录可显式清理：

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
5. 已激活父任务的可信标准角色交接（角色声明 + 精确 `references/<role>-role.md` + reviewer 机器模板）。角色声明按行首锚定，但容许行首的 Markdown 强调/标题/列表标记与 `角色声明`、`角色是|为` 写法：`角色：reviewer`、`**角色**：reviewer`、`## 角色：reviewer`、`1. 角色：reviewer`、`你的角色是 reviewer` 与交接模板自己的复合标签 `角色/读写边界/…：reviewer` 互相等价；值前的限定语（`角色：独立 reviewer`、`角色声明：你是 easydev 的独立 reviewer`）同样算声明——模板写法不可检出会让照文档写交接的模型被稳定拒绝。正文里顺带提到角色仍不算声明：叙述句 `审查由独立审查者完成` 没有引入语、`你是 main 还是独立审查者？`/`你是什么角色？独立审查者还是主代理？` 的限定语不在白名单，都不算；`你是独立 reviewer`/`你是独立设计审查者`/`你是独立施工审查实例` 只在行内有 `你是`/`你的角色是`/`作为` 且限定语只取白名单（`一名`/`一个`/`独立的?`/`easydev 的`）时成立；`独立 reviewer`/`reviewer 子代理` 只在行首算声明，`完成后由独立 reviewer 复核，不要自行宣称完成` 这类开发交接的叙述句不会因为“否定词只截断前缀”变成审查声明。否定词只否定它后面的话：同行尾部禁令（`… 以 **[P级] 简短标题** 起头；不要复述结论`）不吞掉声明、角色路径与模板证据，行首否定（`不要用 reviewer 模板`、`不是 reviewer`）仍不算证据（`识别`/`特别` 里的 `别` 不是否定）。路径 token 按空白与 ASCII 标点切分，再对候选剥全角包裹、在首个全角括号处截断或首次出现目标后缀处截断：`…/reviewer-role.md（先读它，再开始）` 与安装根含 `资料（备份）` 的路径都能检出（不把全角标点当分隔符，否则会 fail-closed）。

未实现者的只读约束只在**用户明确要求只读审查**时派生：`只读` 须修饰审查动作本身（`只读审查`、`只读地 review`、`review 一下，只读`、`只读模式`/`read-only mode`），或出现**全局**禁止修改指令——宽对象（`不要修改代码`、`别动文件`、`do not modify`、`no code changes`）或句尾裸命令（`不要修改`）；限定到具体路径的禁令（`不要改动 SKILL.md`）不是全局只读，不锁写权限。根用户输入的角色同样只按**完整**标准交接派生（声明 + 角色路径 + 模板）：单独一行 `角色：reviewer` 不再把主代理锁成只读，否则“角色：独立 reviewer 这条为什么被拒”这类讨论与修复请求会被整段拒掉。只是**讨论**只读规则（如“reviewer 只读是否合理”“审查 reviewer 角色的只读约束”）不触发；禁止用“全文任意同现”派生，否则 `review` 命中 `reviewer` 内部就会把正常修复请求锁成只读。该标记只约束主代理的写工具与写命令，不改变 reviewer 角色，也不影响基线/模型规则。

父子关联只按可信分派证据建立：父分派 pre 记录分派 ID、初始任务摘要、父 session 与 task epoch（只存摘要，不存原文）；`SubagentStart`/子初始 input 用可信 child ID 或唯一初始任务摘要匹配该记录并**固定 epoch**。匹配缺失/歧义、或该 epoch 已随父任务切换/`active=false` 退役时，子只保留本 actor 的 `role_only`，不写父任务的写序列、基线与审查关系，并告警披露。root 状态键含 host+root session+root actor，同一 actor ID 的不同 session 互不共享状态。

无父关联的完整标准 reviewer 交接只授予本 actor 的 `role_only`：仅执行 reviewer 写拒绝与可证 child 的禁嵌套，不借用父任务的基线、模型或 P3。未激活的 child 成功读取精确 `SKILL.md` 也只做本 actor 的局部激活（`task_active` 但无父任务绑定，`role_source: skill_read`），随后它自己的嵌套分派被拒绝；这条激活不建父任务/新 epoch，失败、只有 requested/结果未知、其他路径的同名文件都不激活，普通未激活 child 的 post 与根 actor 的未激活 post 仍直接放行且不刷告警。该局部激活与根路径共享“同 actor、同工具调用 ID 只能作为一次激活证据”：首次接受时把调用 ID/digest/epoch 写进 actor 级 `consumed_read_ids`/`consumed_reads`（不写任务缓存、不建父任务/epoch），已 `task_active` 的 child 成功读取也补记该证据但不重复激活；因此显式 `active=false` 之后重投同 actor、同调用 ID 的成功读取（同参数/同结果，或改参数/改 `success`↔`completed` 拼写）都保持 `inactive`、嵌套放行，只有新的调用 ID 的成功读取才重新启用本 actor 的禁嵌套约束。仅阅读角色文件不会把主代理改成 reviewer。

可选机器上下文：原始 user/可信父交接中单独一个 `easydev-hook` JSON 围栏，字段限 `version:1, active, role, child, task_root, models_file, role_overrides, code_review`。`active=true` 才是激活证据；`active=false` 只用于真实 user 输入停用当前增强。Prompt 里自动注入的系统文本或被审文件里的同名块不算。

## 规则与降级

规则顺序：解析 → 可信激活/角色 → 破坏性 Git → reviewer 只读 → 结构化目标基线 → 分派配置/交接/嵌套 → 状态事件 → 编码。

- **P1 Git**：`reset --hard`、`checkout --`/`-f`、操作数确指已存在路径的 `checkout`（`git checkout src/app.py` 会还原工作区文件）、会写工作区的 `restore`、`clean -f`、`stash` 写操作、强推与带 `+` 的强制 refspec（`+main:main`，也含无冒号的 `+main`）一律 `deny`；包装（`bash -lc`、`sudo`、`env`）、`-C` 与多段命令会逐段检查。`checkout` 的路径操作数只按已知选项布局判定（`.`/`..`、绝对路径、可信 cwd 下确实存在的相对路径），未知选项布局不猜。Git alias、脚本或动态拼接不展开，文档不宣称无绕过。heredoc 正文是否构成执行按消费者判定：`cat`/`tee`/`wc`/`sort` 等纯数据消费者，以及 Python/Node 等解释器 stdin 的正文是数据、不产生 Git 动作（解释器 stdin 仍不是沙箱）；`sh`/`bash`/`dash`/`zsh`/`ksh`（含 `-s`、argv 包装与 `bash -c` 的脚本阶段）收到的正文按壳脚本递归分析，引用 delimiter 也不阻止执行，`git reset --hard` 仍 `deny`；组尾重定向、fd 转接、管道与无法证明消费者的形态保守按脚本处理。未引用正文里的 `$()` 与简单反引号仍按真实命令替换检查，deny 条件不变。
- **P1 基线**：目标已有 dirty/untracked/不可重建内容时，Hook 在 `PreToolUse`（task 锁外）把**首次**当前内容自动快照到 `<state_root>/baselines/<task_key>/<repo_id>/<仓库相对路径>`：目录 `0700`、文件 `0600`、非软链、逐层 `dir_fd + O_NOFOLLOW`、`os.link` 发布不覆盖，第二次写不覆盖首次快照。快照成功即登记并放行；源文件非普通文件（FIFO/设备/目录）、软链、不可读或超过 8 MiB 则 `deny` 并给出可操作原因；`clean_tracked`/`absent` 不要求快照；成功 post 后再被外部修改仍 `deny external_modification`。显式 `EASYDEV_TASK_ROOT`（或安装上下文/机器上下文 `task_root`）仍优先旧的手工核对路径：只读核对 `baseline/<仓库相对路径>` 存在、非软链、`0600`、无硬链，缺失或不合规即 `deny`，已知旧任务根不能给需要内容基线的新任务复用。任务根、`baseline/` 与备份父链都从祖先链起逐层 `dir_fd + O_NOFOLLOW` 打开核对；业务文件或其仓库内父链是软链时 `deny`（不 realpath 后当普通路径），也不写软链目标。同一调用的重复目标按 `origin_key` 合并：任一条目观察到软链或 `nonregular`/`toolarge`/`unreadable` 时保留具体拒绝证据；两个可信指纹不同则在 `pre_tool` 以 `unstable_target` 拒绝并要求先协调并发变更。指纹取证超时（`deadline`）是降级不是状态变化：保留可用的真实指纹、告警 `基线指纹查询超时，基线检查降级`；只有超时、没有真实指纹的单条目标同样按目标集不完整记账。成功 post 不建立成功豁免：目标集不完整，或 post 阶段目标变成 `nonregular`/`toolarge`/`unreadable`（没有可信的写入后指纹）时，都不写成功指纹、不推进 `write_seq`（代价是这类写入不计入 P3 写后复审提醒，属于有意取舍）。执行型 stdin（解释器正文、herestring、`-c`/管道/fd、读脚本文件的壳、未知消费者）的记账缺口按「任务 + 原因」只提醒一次（换 epoch 或换原因重新提醒；记账仍是不完整）。自动快照是私有缓存，不是业务备份：Hook 不写业务文件。`rsync` 按真实语义单独解析（`-t` 是无值的 `--times`，没有 `--target-directory`，白名单外的选项一律按布局不可证明处理）：只有“单普通文件源 + 已有目录目的”展开为 `目的/basename` 做文件级检查；目录目的、目录源、多源、`--files-from`、`--read-batch` 都只记**不完整目的**，披露后放行，与 `cp -R` 的现有口径一致，**不宣称**目录级或批次级快照覆盖。`--remove-source-files` 把已知源追加为写目标，但多源时整段不完整、连已知源也不快照；`--read-batch` 的显式源被 rsync 忽略，批次内容与实际影响不枚举，只记不完整目的，不能宣称已保护。以 `~` 开头的 cwd（含包装器 cwd）不展开，后续相对目标按未知披露；`--write-batch`/`--only-write-batch` 的主目的与批次文件都不完整，整段不建立文件级快照。已证明数据消费者的 heredoc 正文按数据处理：`cat > out.txt <<'EOF' … EOF` 的外层静态重定向写目标完整（`complete=True`），照旧进入首次快照与基线检查；正文里的内容不生成命令、读写目标或 Git 动作。词法没能证明正文边界的 heredoc 回退到旧的 opaque 路径并按旧条件披露结构缺口，不宣称所有 heredoc 已被覆盖。
- **P1 reviewer 只读**：写工具/补丁和明确写命令 `deny`，含 `find -delete`/`find -exec`、`sort -o <文件>` 这类显式写副作用的命令；纯读、测试、构建放行。**窄拒绝**：已识别出复合结构但拆解失败（无法安全配对/归属）时 `deny`，并提示把命令拆成简单命令重跑；其他无法完整解析的命令仍告警放行并披露缺口。reviewer 会话的 `sh <<'EOF' … EOF` 同样按脚本分析，正文里的明确写仍 `deny`（进程内 `Decision.code` 为 `reviewer_command_write`），字面 heredoc 数据不构成写。
- **P2 模型配置**：只读第一个存在的完整文件（对话指定 → 项目 `.easydev/models.json` → 用户配置 → 内置 inherit），不跨文件合并；选中文件无效即 `deny`，不静默回退。`role_overrides` 先按字段校验，再合成后走同一 validator。
- **P2 交接/嵌套**：reviewer 交接必须有精确角色路径与 `**[P级] 简短标题**`/`**[P级别] …**`/`**[P0|P1|P2|P3] …**`/逐个 P0–P3 模板；缺项 `deny` 并在理由里直接给出可照抄的字面量（`角色：reviewer`、reviewer 角色文件绝对路径、`**[P级] 简短标题**`），不改写 prompt。已证明的 child 不能再次分派；状态查询/等待放行。
- **P3 收口**：只有根 actor 正常 settle 时判定；可信 child 的成功写直接进入共享写序列，完成时 `checkpoint=max(checkpoint,start_write_seq)`；审后又写代码则每回合最多提醒一次。`.md` 不按扩展名排除；只排除显式 `doc_paths` 与任务根内明确报告路径（`reports/`、`baseline/`、`report*`/`review*` 命名），运行规则/构建清单/测试/未知配置一律计入，task_root 与 repo 重叠时不据此排除整个 repo。完成事件只认结构化成功终态 + 结构化报告，显式 `failed`/`cancelled`/`timeout`/`running` 优先排除；Codex 用 `SubagentStop` 的 `agent_id` 关联分派、`last_assistant_message` 作为结构化报告，不解析报告正文或最终散文。写序列只在 `task_active` 作用域记账：配对键为 actor + task epoch + 调用 ID 并核对目标集合，父子同名工具 ID 互不覆盖；同一文件的多次写按 `origin_key` 去重为一个目标，成功写入只推进一次 `write_seq`。read/grep 等非写工具、已证明没有写目标的完整调用（如 `git status`、`rg`）与 `role_only`/未激活不建 pending、不产生配对告警。已有写 pre 而 post 改报非写工具、换成无写目标、失败/未知结果时，消费该 pending 并披露，不建立成功豁免。真实目标缺口（opaque/too_deep、目标集不完整、Git 不可用、执行型 stdin 未解析、软链/不安全指纹、预取指纹变化、指纹超时）仍按原文披露，但同一任务、同一原因集合只提醒一次（换原因或换 epoch 重新提醒）。只改 `.git`/索引/远端的 git 元数据命令（`add`/`commit`/`fetch`/`push`/`config`/`gc`/`prune`/`repack`/`update-index`、`branch`/`tag` 的 ref 写）与纯仓库外写入没有可记账的工作区目标：不建 pending、不告警（reviewer 只读仍拒绝这些 git 写）；仓库内与仓库外混合目标仍按缺口处理。不完整的写（如目录源 `rsync`、`--files-from`/`--read-batch`、`--write-batch`/`--only-write-batch`）在成功 post 时也不推进代码写序列：即使实际改了代码，也可能不触发审后复审提醒。这是当前能力边界，不声称 P3 覆盖。

| 宿主 | P3 完成事件 | 说明 |
| --- | --- | --- |
| Codex | 启用 | 模板注册 `SubagentStart`/`SubagentStop`；按官方子 Hook 契约（`session_id` 是父 session、`agent_id` 是 child）用父 session + `agent_id` 关联可信分派，`last_assistant_message` 作为结构化报告；无法关联时只留 `role_only` 并告警 |
| Antigravity / DSH / pi | 不启用（能力降级） | 可加载 `Stop`，但无法可靠归因“审查完成”后写入 → `has_completed_code_review` 不置位，settle 不续跑并披露 `P3` 能力缺口，不虚报已支持 |

**故障开路**：输入超限、非 JSON、未知事件、无 cwd/会话、transcript 不可读、锁忙、状态损坏、Git 查询失败与内部异常都会告警并放行（高危 Git 一旦已解析命中，不因后续状态写失败而放行）。运行期 Plugin 的 fail-open 不适用于回放 Harness 的专用读取守卫。

**诊断作用域**：四类基线结构缺口（命令结构无法完整解析、缺少可定位的仓库根或 cwd、写入目标不完整及其 gap 原句、命令内目录变化）保留原产生条件，但只在 `pre_tool` 披露一次；写入目标位于仓库外不再算结构缺口（基线只覆盖任务仓库，纯仓库外写入静默跳过）；opaque/too_deep 且没有写证据、找不到仓库的纯读命令仍按原条件告警（这是基线结构告警，与完整纯读调用的记账静默不同）。写序列仅在 `task_active` 作用域记账：没有对应写 pre 的非写 post、已证明无写目标的完整调用与 `role_only`/未激活不产生配对缺口告警；已有写 pre 而 post 改报非写工具、无写目标或失败/未知结果时，消费配对并披露，不建立成功豁免。缺少工具 ID 等身份/协议诊断仍保留。真实目标缺口仍保留记账诊断（按「任务 + 原因」去重）；reviewer 的 `命令结构未覆盖` 分支保持原条件；状态、锁、Git 查询/指纹、transcript、分派、激活、工具权限、输入协议与内部异常从不静默。

**诊断类别与隐私**：pi 适配器把核心 stderr 折成固定类别：`状态不可用`、`锁忙`、`Git 查询失败`、`命令结构未覆盖`、`写入目标未覆盖`、`写序列记账缺口`、`transcript 证据降级`、`分派/子代理降级`、`激活判定降级`、`工具权限未覆盖`、`输入或协议无效`、`内部异常`。一次核心调用至多一条汇总，只输出类别名，不回显命中的原句、路径或 stack；完全不匹配的新文案回退到 `core_stderr：核心有诊断，细节已隐藏`。指纹超时不再带路径。核心仍按原文去重只送前三条诊断，类别精确化不等于完整诊断传输协议。deny reason（如 `unstable_target`、`baseline_source_unsafe`）走响应，不属于 stderr 固定类别表。

**告警通道**：pi 适配器默认把降级类别写私有日志，不上屏——pi 会把扩展 stderr 直接展示在交互区（输入框上方），正常开发中的降级提示会干扰输入。日志路径取 `$EASYDEV_HOOKS_LOG`，否则 `$XDG_STATE_HOME/easydev-hooks/pi-warnings.log`（默认 `~/.local/state/easydev-hooks/pi-warnings.log`）：目录 `0700`、文件 `0600`、超过 1 MiB 截断，每行 `<ISO 时间>\t<事件>\t<固定类别汇总>`。`EASYDEV_HOOKS_WARNINGS=stderr` 恢复上屏（调试/冒烟用）。适配层自身故障（`core 启动失败`、`超时`、`信号终止`、`非 0 退出`、`输出超限`、`无输出`、`输出无效 JSON`、`输出不是 JSON 对象`）默认仍在 stderr 上屏并在日志留痕：它意味着守卫根本没生效，不属于“降级”。日志只含固定类别与事件名，不存 prompt、命令或路径；日志不可写时不回落到 stderr，也不影响 fail-open 返回值。

**共用分析器的读取守卫**：shell 分析给 heredoc 正文打 `stdin_program` 标记，含义是“正文不是可证明的数据消费者输入”——直接解释器、壳、组尾重定向、fd 转接、管道与未知消费者都为真。回放读取守卫（`tests/experiments/agent_runner/replay_read_guard.py`）对真值一律先以 `target_unresolved` 拒绝，即使没有已知读取目标；共用 helper 相应返回“非只读/未知”，同时保留已知读取。core 不直接消费 `stdin_program`；另消费 `stdin_unparsed`，将未能证明已完整解析的执行型 stdin（解释器、herestring、`-c`/管道/fd、读脚本文件的壳、未知消费者）纳入写目标 gap，成功 post 不建立成功豁免，但不据此新增 deny；已证明由壳从 stdin 读取并完成递归解析的正文，不因 `stdin_program` 单独产生该缺口。壳参数判定为有限规则，未识别布局按未解析（多披露，不少披露）。`cat > f <<'EOF' … EOF` 与普通 `cat` 数据 heredoc 为假（没有需要读取的 stdin 路径），守卫直接放行，`f` 的写目标仍进基线检查；transcript 证据仍只取已知读取。

## 状态与隐私

默认状态根为 `<系统临时根>/easydev-hooks-<uid>/v1/<repo-id>/`，也可由 `install-context.json` 的 `state_root` 显式选择同等私有根。目录 `0700`、文件 `0600`、原子写、非阻塞有限锁。**JSON 状态不存 prompt、命令或补丁；自动快照 `baselines/` 仅存业务原内容，私有 0600（目录 0700），不对外发送。** heredoc 的正文、delimiter 与命令替换源码只在本次分析的内存里短暂存在，不写入 JSON 状态、probe 日志、快照元数据或告警。 自动快照只写 `<state_root>/baselines/<task_key>/<repo_id>/<相对路径>`，属私有缓存而非业务备份。状态是缓存，不是任务账本、授权库或业务备份。只规范化已核实的系统临时根别名（如 macOS `/tmp` → `/private/tmp`）一次；私有根以下逐层 `dir_fd + O_DIRECTORY|O_NOFOLLOW` 打开并 `fstat` 核对 owner/0700，祖先软链一律不可用（告警放行，不修复、不删除）。一次调用的 1500ms 内部截止为 Git、指纹、transcript 与遍历共享；状态、baseline、快照、models 与 transcript 的打开统一 nofollow + nonblock，`fstat` 普通文件后才做有界读取（FIFO/设备/目录/超大文件降级或拒绝，不阻塞）。Git 查询、指纹、分派前的仓库/models 读取、input 的 transcript 取证与自动快照都在 task 锁外完成；持锁后只重新计算 root/task/epoch 身份并用锁外已取证的签名复核，锁内不启动 Git、不做配置全文读取、不重新读取或解析 transcript、不做大文件 I/O，签名变化即丢弃取证/预取并降级、不向旧 task 提交。

## 卸载

```bash
python3 ~/.local/share/easydev-hooks/install.py uninstall \
  --target <repo>/.codex/hooks.json \
  --manifest ~/.local/share/easydev-hooks/config/codex/install-manifest.json
```

只移除仍与清单摘要一致的条目：用户改过的条目与其他 hooks 原样保留，重复卸载幂等（第二次 `changed=false`）；含空格/单引号/`$`/反引号的 Plugin 根也能识别。随后按宿主要求重载/重启。回滚顺序：先解除宿主注册（pi 移除扩展目录后 `/reload`），再删除独立 Plugin 安装目录。不要只删 Python 核心而留下仍加载的 TS 扩展。任务目录、`baseline/`、自动快照与 Hook 私有状态默认保留；确认无需恢复后再由用户清理，不删除未知 owner 或链接目标。

## 已知未验证项

本版未在真实宿主上安装或运行：四宿主的 Hook 信任、真实 stdin 结构、输出消费与 Stop 续跑都需宿主端到端 probe；Antigravity 的命名组结构、工具事件 Grouped / 非工具事件 Flat、`--event`、`workspacePaths`、`PostToolUse` 的 `error`/整型 `stepIdx`、`{"decision": "allow"}` 与 DSH 的 `{"permissionDecision": "allow"}` 只有离线夹具与官方文档核对覆盖，**未在真实 Antigravity/DSH 进程执行 `--mode probe`**；DSH 本机无 CLI，只能给离线夹具与安装文档；pi/Antigravity/DSH 的子代理完成结果没有稳定 selector，P3 完成事件明确不启用；pi 适配器已用 Node 夹具（假的 `ExtensionAPI` + 真实 `child_process` argv）验证“复制 Plugin 树 + generate → 真实核心的 deny”。Antigravity 原生 transcript 归因已用本机 13 个真实 `transcript.jsonl` 做只读实测（不打印正文）：修正前 `references` 恒空、`confirmed` 全 false，修正后 8 个会话取得 `references`、6 个取得 `SKILL.md` 确认读取偏移；这只证明解析适配真实日志形状，不证明桌面端已装 Hook 或端到端拦截生效。P0 回放守卫的进程无法启动或被强杀时不能保证 deny 输出，须停止回放并标 `valid_invocation=false`。heredoc 支持是有限子集：只处理能证明正文边界的 Bash/POSIX 风格形态；嵌套执行 heredoc（含 `$()`/反引号内的 heredoc）、任意 `${...}`（含简单 `${NAME}`）、算术展开 `$((...))`、复杂或嵌套反引号、未引用正文里的反斜杠续行与跨行 `$()`/heredoc 交互，一律整次回退到旧词法路径并保留旧的 Git/写证据；`<<-` 只去前导 tab，delimiter 不做宽松 trim，CRLF 与 Windows/PowerShell 未支持。pre-only 是事件作用域，不是“已披露过”的记录：pre 未运行、宿主只投递 post、pre/post 参数不同且缺口只在 post 出现时，四类具体结构原因都可能完全没有披露，post 自己的配对/记账告警不能补回原因，这是已知信息损失。诊断仍是 `warning: …` 无转义文本协议，原则上无法消歧；当前 producer 值域有界（身份描述只有会话/线程/actor，终态只取固定失败集合），恶意 stderr 替身导致的多命中不等于真实 core 可达，两类验收分开。离线解析通过也不代表宿主真实加载或 shell 行为全覆盖。所有离线通过不等于四宿主强隔离保证。
