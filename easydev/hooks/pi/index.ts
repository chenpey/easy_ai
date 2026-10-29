/**
 * EasyDev Hook 的 pi 薄适配器。
 *
 * 只负责构造 envelope、调用 Python 核心、把统一 decision 编码成宿主结果；
 * 安全规则、状态与协议都在 core.py。任何异常（含超时/JSON 失败）都返回放行，
 * 避免宿主默认 fail-closed 阻断正常工具。
 */
import { spawn } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
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

// 告警白名单：只输出类别名，不输出命中的原句。
const STDERR_CATEGORIES: Array<{ label: string; pattern: RegExp }> = [
  { label: "状态不可用", pattern: /状态(不可用|读取失败|写失败|处理异常)/ },
  { label: "锁忙", pattern: /锁忙|状态锁不可用/ },
  { label: "Git 查询失败", pattern: /Git (状态查询|查询)失败|基线指纹查询超时/ },
  { label: "命令结构未覆盖", pattern: /命令结构(未覆盖|无法完整解析)|(写入目标|post 写入目标)不完整/ },
  { label: "输入或协议无效", pattern: /协议字段类型不符|输入超限|非法 JSON/ },
  { label: "内部异常", pattern: /内部异常/ },
];

function clampBytes(text: string, limit: number): string {
  const buffer = Buffer.from(text, "utf8");
  return buffer.length <= limit ? text : buffer.subarray(0, limit).toString("utf8");
}

/** 把核心 stderr 折成白名单类别；无可公开类别时只披露“有诊断”。 */
function summarizeCoreStderr(text: string): string | null {
  if (!text.trim()) return null;
  const labels = STDERR_CATEGORIES.filter(({ pattern }) => pattern.test(text)).map(
    ({ label }) => label,
  );
  if (labels.length === 0) return HIDDEN_STDERR_TEXT;
  return "core 降级：" + labels.join("；");
}

/** 一次调用最多一条汇总告警；输出失败不得影响 fail-open 返回值。 */
function emitWarning(parts: string[]): void {
  if (parts.length === 0) return;
  const line = `${WARNING_PREFIX}: ${parts.join("；")}`;
  try {
    console.error(clampBytes(line, WARNING_MAX));
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
      emitWarning(diagnostics);
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
