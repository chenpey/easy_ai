/**
 * EasyDev Hook 的 pi 薄适配器。
 *
 * 只负责构造 envelope、调用 Python 核心、把统一 decision 编码成宿主结果；
 * 安全规则、状态与协议都在 core.py。任何异常（含超时/JSON 失败）都返回放行，
 * 避免宿主默认 fail-closed 阻断正常工具。
 */
import { spawn } from "node:child_process";
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  statSync,
  truncateSync,
} from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import type {
  AgentBeforeSettleEvent,
  BoundaryResult,
  ExtensionAPI,
  ExtensionContext,
  InputEvent,
  ToolCallEvent,
  ToolResultEvent,
} from "@earendil-works/pi-coding-agent";

const HERE = dirname(fileURLToPath(import.meta.url));
const PLUGIN_ROOT = join(HERE, "..");
const CORE = join(PLUGIN_ROOT, "core.py");
// 扩展默认读 `<plugin-root>/install-context.json`；生成器把同一份 context
// 写到那里，因此按文档“复制完整 Plugin 树 + generate”后即为已接入状态。
const CONTEXT = process.env.EASYDEV_HOOKS_CONTEXT || join(PLUGIN_ROOT, "install-context.json");
const EXEC_TIMEOUT_MS = 2000;
const MAX_BUFFER = 256 * 1024;
// 核心 stderr 只作有界缓冲：原文可能含路径、payload 或 stack，绝不直接进宿主日志。
const STDERR_CAPTURE_MAX = 8192;
const WARNING_MAX = 2048;
const WARNING_PREFIX = "[easydev-hooks] warning";
const HIDDEN_STDERR_TEXT = "core_stderr：核心有诊断，细节已隐藏";
// 告警默认只写私有日志：pi 会把扩展的 stderr 直接展示在交互区（输入框上方），
// 每轮开发都刷“降级”类提示会干扰输入。`EASYDEV_HOOKS_WARNINGS=stderr` 恢复
// 旧的上屏行为（调试/冒烟用）。传输层故障始终上屏：它意味着守卫根本没跑起来。
const WARNINGS_MODE =
  process.env.EASYDEV_HOOKS_WARNINGS === "stderr" ? "stderr" : "log";
const WARNING_LOG_MAX = 1024 * 1024;
const WARNING_LOG =
  process.env.EASYDEV_HOOKS_LOG ||
  join(
    process.env.XDG_STATE_HOME || join(homedir(), ".local", "state"),
    "easydev-hooks",
    "pi-warnings.log",
  );

// 诊断边界：行首、核心的 `warning: ` 前缀，或同一条汇总里的全角分号。
// 无转义文本协议原则上不能消歧，但当前 core 的生产者值域有界；边界只是
// 把“整条固定文案”与普通正文区分开，不能当作字段边界的证明。
const DIAGNOSTIC_BOUNDARY = "(?:^|warning:\\s*|；)";

function diagnosticPattern(family: string): RegExp {
  // 每个类别族整体包成非捕获组：`DIAGNOSTIC_BOUNDARY + family` 的语义是
  // “边界 + 族内任一支”，而不是“边界只约束第一支”。
  return new RegExp(DIAGNOSTIC_BOUNDARY + "(?:" + family + ")", "m");
}

// 固定类别：只输出类别名，不回显命中的原句、路径与 stack。
// 数组顺序即输出顺序；每类只匹配 core 的固定前缀/有限中间字段，
// 绝不把分组值拼进输出，工具输入也不会进入告警文案。
const STDERR_CATEGORIES: Array<{ label: string; pattern: RegExp }> = [
  {
    label: "状态不可用",
    pattern: diagnosticPattern(
      "私有状态不可用|状态(?:读取失败|写失败|处理异常)" +
        "|(?:transcript 输入取证的|分派预取)状态不可用|缺少会话标识，状态判定降级|probe 记录不可写",
    ),
  },
  { label: "锁忙", pattern: diagnosticPattern("锁忙|状态锁不可用") },
  {
    label: "Git 查询失败",
    pattern: diagnosticPattern(
      "(?:Git 状态查询失败，基线检查降级|基线指纹查询超时，基线检查降级)(?=；|$)",
    ),
  },
  {
    label: "命令结构未覆盖",
    pattern: diagnosticPattern("(?:reviewer )?命令结构(?:未覆盖|无法完整解析)"),
  },
  {
    label: "写入目标未覆盖",
    pattern: diagnosticPattern(
      "写入目标(?:不完整|位于仓库外)|命令写入目标不完整|命令内目录变化无法确定" +
        "|缺少可定位的仓库根或 cwd|补丁目标解析不完整|宿主未提供结构化非 sh[e]ll 参数" +
        "|写工具缺少可识别路径字段|已有任务状态缺少任务根归属索引",
    ),
  },
  {
    label: "写序列记账缺口",
    pattern: diagnosticPattern(
      "post (?:缺少可信成功结果|未找到|写入目标不完整|目标含符号链接|目标指纹不可信|目标与 pre|目标缺少 pre 指纹)" +
        "|缺少工具 ID，写事件无法幂等记账|锁内复核发现任务绑定已变化|锁外预取结果与当前任务绑定不一致",
    ),
  },
  {
    label: "transcript 证据降级",
    pattern: diagnosticPattern(
      "transcript (?:不是普通文件|身份核验失败|原生.*?身份与当前会话不一致|缺原生会话身份" +
        "|证据抽取失败|读取证据抽取失败|输入取证失败)" +
        "|无法从 transcript 取得最新用户记录|该宿主无 transcript 能力",
    ),
  },
  {
    label: "分派/子代理降级",
    pattern: diagnosticPattern(
      "子代理(?:未匹配到有效分派记录|绑定与 root 当前 epoch 不匹配|启动未能匹配可信分派记录" +
        "|完成缺少可关联分派|终态为)" +
        "|分派预取失败|分派预检的锁外取证与当前上下文不一致|同一分派 ID 的内容或角色已变化" +
        "|该分派 ID 的观测已因内容变化失效|宿主未提供 reviewer 交接正文|该宿主无法可靠归因审查完成事件",
    ),
  },
  {
    label: "激活判定降级",
    pattern: diagnosticPattern(
      "同一工具调用 ID 的成功读取内容已变化|SKILL 读取缺少工具 ID|缺少 root 绑定，SKILL 读取恢复降级" +
        "|同一 SKILL 读取调用已消费|SKILL 读取恢复的 root 绑定发生变化|输入取证的签名在锁内复核时已变化",
    ),
  },
  { label: "工具权限未覆盖", pattern: diagnosticPattern("未知自定义工具的写权限未推断") },
  {
    label: "输入或协议无效",
    pattern: diagnosticPattern(
      "协议字段类型不符|输入超过上限|输入不是合法 JSON|输入不是 JSON 对象|输入超限|非法 JSON" +
        "|未知事件|缺少命令文本|Antigravity 提供 [0-9]+ 个 workspacePaths",
    ),
  },
  { label: "内部异常", pattern: diagnosticPattern("hook 内部异常|内部异常") },
];

function clampBytes(text: string, limit: number): string {
  const buffer = Buffer.from(text, "utf8");
  return buffer.length <= limit ? text : buffer.subarray(0, limit).toString("utf8");
}

/** 把核心 stderr 折成固定类别；同一类别只列一次，无可公开类别时只披露“有诊断”。 */
function summarizeCoreStderr(text: string): string | null {
  if (!text.trim()) return null;
  const labels: string[] = [];
  for (const { label, pattern } of STDERR_CATEGORIES) {
    if (!labels.includes(label) && pattern.test(text)) labels.push(label);
  }
  if (labels.length === 0) return HIDDEN_STDERR_TEXT;
  return "core 降级：" + labels.join("；");
}

/** 汇总告警写私有日志（有界、不存 prompt/命令/路径）；失败不得影响放行。 */
function appendWarningLog(line: string, event: string): void {
  try {
    mkdirSync(dirname(WARNING_LOG), { recursive: true, mode: 0o700 });
    try {
      if (statSync(WARNING_LOG).size >= WARNING_LOG_MAX) truncateSync(WARNING_LOG, 0);
    } catch {
      // 日志还不存在：直接追加。
    }
    const stamp = new Date().toISOString();
    appendFileSync(WARNING_LOG, `${stamp}\t${event}\t${line}\n`, { mode: 0o600 });
  } catch {
    // 日志不可用时不回落到 stderr：宿主交互区不应因告警通道故障被刷屏。
  }
}

/** 一次调用最多一条汇总；默认只把传输层故障上屏，降级类别进日志。 */
function emitWarning(parts: string[], event: string): void {
  if (parts.length === 0) return;
  const line = clampBytes(`${WARNING_PREFIX}: ${parts.join("；")}`, WARNING_MAX);
  appendWarningLog(line, event);
  if (WARNINGS_MODE === "stderr") {
    try {
      console.error(line);
    } catch {
      // 告警通道本身不可用时保持静默放行。
    }
    return;
  }
  // 传输层故障（spawn/超时/非 0/JSON/空输出）说明 Hook 未生效，必须让人看见。
  const transport = parts.filter((part) => !part.startsWith("core 降级："));
  if (transport.length === 0) return;
  try {
    console.error(clampBytes(`${WARNING_PREFIX}: ${transport.join("；")}`, WARNING_MAX));
  } catch {
    // 告警通道本身不可用时保持静默放行。
  }
}

interface CoreContext {
  python?: unknown;
}

function readContext(): CoreContext | null {
  try {
    const value = JSON.parse(readFileSync(CONTEXT, "utf8")) as unknown;
    return value && typeof value === "object" ? (value as CoreContext) : null;
  } catch {
    return null;
  }
}

const CONTEXT_VALUE = readContext();
// 生成物记录的绝对解释器路径优先；环境变量可显式覆盖；都没有才回落 PATH。
const PYTHON =
  process.env.EASYDEV_HOOKS_PYTHON ||
  (typeof CONTEXT_VALUE?.python === "string" && CONTEXT_VALUE.python
    ? CONTEXT_VALUE.python
    : "python3");

interface CoreResponse {
  block?: boolean;
  reason?: string;
  decision?: string;
  continue?: boolean;
  action?: string;
  entries?: unknown[];
}

function pluginAvailable(): boolean {
  return existsSync(CORE) && existsSync(CONTEXT);
}

function sessionId(ctx: ExtensionContext): string {
  try {
    return ctx.sessionManager.getSessionId();
  } catch {
    return "";
  }
}

function envelope(
  event: string,
  ctx: ExtensionContext,
  extra: Record<string, unknown>,
): Record<string, unknown> {
  const id = sessionId(ctx);
  const child = process.env.PI_SUBAGENT_CHILD === "1";
  return {
    protocol: 1,
    event,
    sessionId: id,
    actorId: id,
    parentId: process.env.PI_SUBAGENT_PARENT_SESSION || "",
    child,
    cwd: ctx.cwd,
    transcriptPath: ctx.sessionManager.getSessionFile() || "",
    ...extra,
  };
}

async function callCore(payload: Record<string, unknown>): Promise<CoreResponse | null> {
  if (!pluginAvailable()) return null;
  const text = JSON.stringify(payload);
  const event = typeof payload.event === "string" ? payload.event : "";
  return new Promise((resolve) => {
    let settled = false;
    let stderrBytes = Buffer.alloc(0);
    let timedOut = false;
    // 每次调用只有一条汇总：结构故障与核心诊断合并，先置 settled 再输出。
    const diagnostics: string[] = [];
    const finish = (value: CoreResponse | null): void => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      const summary = summarizeCoreStderr(stderrBytes.toString("utf8"));
      if (summary) diagnostics.push(summary);
      emitWarning(diagnostics, event);
      resolve(value);
    };
    const timer = setTimeout(() => {
      timedOut = true;
    }, EXEC_TIMEOUT_MS);
    if (typeof timer.unref === "function") timer.unref();
    let stdout = "";
    let child;
    try {
      child = spawn(PYTHON, [CORE, "--host", "pi", "--context", CONTEXT], {
        timeout: EXEC_TIMEOUT_MS,
        killSignal: "SIGKILL",
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch {
      diagnostics.push("core 启动失败，已放行");
      finish(null);
      return;
    }
    child.stderr?.on("data", (chunk: Buffer) => {
      if (stderrBytes.length >= STDERR_CAPTURE_MAX) return;
      stderrBytes = Buffer.concat([stderrBytes, chunk]).subarray(0, STDERR_CAPTURE_MAX);
    });
    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
      if (stdout.length > MAX_BUFFER) {
        diagnostics.push("core 输出超限，已放行");
        child.kill("SIGKILL");
        finish(null);
      }
    });
    child.on("error", () => {
      diagnostics.push("core 启动失败，已放行");
      finish(null);
    });
    child.on("close", (code: number | null, signal: string | null) => {
      if (settled) return;
      // 异常退出（信号/超时/非 0）时 stdout 至多是残缺输出：不能据它返回
      // block=true，否则会一边拦工具一边对外说“已放行”。
      if (signal) {
        if (timedOut) diagnostics.push(`core 调用超时（${EXEC_TIMEOUT_MS}ms），已放行`);
        else diagnostics.push(`core 被信号终止（${signal}），已放行`);
        finish(null);
        return;
      }
      if (code !== 0) {
        diagnostics.push(`core 非 0 退出（${code}），已放行`);
        finish(null);
        return;
      }
      const text = stdout.trim();
      if (!text) {
        diagnostics.push("core 无输出，已放行");
        finish(null);
        return;
      }
      let parsed: unknown;
      try {
        parsed = JSON.parse(text);
      } catch {
        diagnostics.push("core 输出无效 JSON，已放行");
        finish(null);
        return;
      }
      if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
        // 只有 JSON 对象才是核心的 decision；`null`/`[]`/`0` 静默放行会掩盖故障。
        diagnostics.push("core 输出不是 JSON 对象，已放行");
        finish(null);
        return;
      }
      finish(parsed as CoreResponse);
    });
    if (child.stdin) {
      child.stdin.on("error", () => undefined);
      child.stdin.end(text, "utf8");
    }
  });
}

function allow(): undefined {
  return undefined;
}

export default function easydevHooks(pi: ExtensionAPI): void {
  pi.on("input", async (event: InputEvent, ctx: ExtensionContext) => {
    try {
      await callCore(envelope("input", ctx, { prompt: event.text, source: event.source }));
    } catch {
      // 输入阶段绝不阻断。
    }
    return { action: "continue" };
  });

  pi.on("tool_call", async (event: ToolCallEvent, ctx: ExtensionContext) => {
    try {
      const result = await callCore(
        envelope("tool_call", ctx, {
          toolName: event.toolName,
          toolCallId: event.toolCallId,
          toolInput: (event as { input?: unknown }).input ?? {},
          parentToolCallId: event.parentToolCallId || "",
        }),
      );
      if (result && result.block === true) {
        return { block: true, reason: result.reason || "EasyDev hook denied this call" };
      }
      return allow();
    } catch {
      return allow();
    }
  });

  pi.on("tool_result", async (event: ToolResultEvent, ctx: ExtensionContext) => {
    try {
      await callCore(
        envelope("tool_result", ctx, {
          toolName: event.toolName,
          toolCallId: event.toolCallId,
          toolInput: event.input ?? {},
          outcome: event.isError === true ? "error" : "success",
        }),
      );
    } catch {
      // 观测失败不影响工具结果。
    }
    return undefined;
  });

  pi.on("agent_before_settle", async (event: AgentBeforeSettleEvent, ctx: ExtensionContext) => {
    try {
      if (
        event.outcome !== "completed" ||
        event.context?.canContinue !== true ||
        event.continue === true
      ) {
        return allow();
      }
      const result = await callCore(
        envelope("settle", ctx, {
          outcome: event.outcome,
          canContinue: event.context?.canContinue === true,
        }),
      );
      if (result && result.continue === true) {
        const boundary: BoundaryResult = { continue: true };
        if (Array.isArray(result.entries)) {
          boundary.entries = result.entries as BoundaryResult["entries"];
        }
        return boundary;
      }
      return allow();
    } catch {
      return allow();
    }
  });

  pi.on("session_start", async (_event, ctx: ExtensionContext) => {
    try {
      await callCore(envelope("session_start", ctx, {}));
    } catch {
      // SessionStart 只核对环境，不注入文本。
    }
    return undefined;
  });
}
