import { execFileSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

interface ToolCallEvent {
  toolName?: string;
  input?: Record<string, unknown>;
}

interface ExtensionContext {
  cwd?: string;
  hasUI?: boolean;
  ui?: {
    confirm(title: string, message: string): Promise<boolean>;
  };
}

interface ExtensionAPI {
  on(
    event: "tool_call",
    handler: (
      event: ToolCallEvent,
      ctx: ExtensionContext,
    ) => Promise<{ block: boolean; reason: string } | void>,
  ): void;
}

const CHECK_SCRIPT = join(homedir(), ".config", "agent-hooks", "check_command.py");
const GUARDED_TOOLS = new Set(["bash", "write", "edit"]);

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event, ctx) => {
    if (!event.toolName || !GUARDED_TOOLS.has(event.toolName)) {
      return;
    }

    let rawOut = "";
    try {
      rawOut = execFileSync(
        "python3",
        [CHECK_SCRIPT, "--mode", "json", "--host", "pi"],
        {
          input: JSON.stringify({
            toolName: event.toolName,
            input: event.input ?? {},
            cwd: ctx?.cwd ?? process.cwd(),
          }),
          encoding: "utf8",
          stdio: ["pipe", "pipe", "pipe"],
          timeout: 10_000,
        },
      ).trim();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      return {
        block: true,
        reason: `Hook 执行失败（fail-closed），已阻断工具调用：${msg}`,
      };
    }

    if (!rawOut) {
      return;
    }

    let decision = "";
    let reason = "";
    try {
      const parsed = JSON.parse(rawOut) as { decision?: string; reason?: string };
      decision = parsed.decision ?? "";
      reason = parsed.reason ?? "";
    } catch {
      return {
        block: true,
        reason: "Hook 输出非合法 JSON（fail-closed），已阻断工具调用。",
      };
    }

    if (decision === "deny") {
      return {
        block: true,
        reason: reason || "命令或写入内容违反安全/非交互规范。",
      };
    }

    if (decision === "force_ask") {
      if (ctx?.hasUI && ctx.ui?.confirm) {
        const approved = await ctx.ui.confirm("高风险操作确认", reason || "请确认是否继续执行。");
        if (approved) {
          return;
        }
      }
      return {
        block: true,
        reason: reason || "高风险操作未获用户确认，已阻断。",
      };
    }
  });
}
