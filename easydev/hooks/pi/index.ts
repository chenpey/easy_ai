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
    const finish = (value: CoreResponse | null): void => {
      if (!settled) {
        settled = true;
        resolve(value);
      }
    };
    let stdout = "";
    let child;
    try {
      child = spawn(PYTHON, [CORE, "--host", "pi", "--context", CONTEXT], {
        timeout: EXEC_TIMEOUT_MS,
        killSignal: "SIGKILL",
        stdio: ["pipe", "pipe", "ignore"],
      });
    } catch {
      finish(null);
      return;
    }
    child.stdout?.on("data", (chunk: Buffer) => {
      stdout += chunk.toString("utf8");
      if (stdout.length > MAX_BUFFER) {
        child.kill("SIGKILL");
        finish(null);
      }
    });
    child.on("error", () => finish(null));
    child.on("close", () => {
      try {
        const value = JSON.parse(stdout.trim() || "{}");
        finish(value && typeof value === "object" ? (value as CoreResponse) : null);
      } catch {
        finish(null);
      }
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
