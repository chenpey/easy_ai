import { execFileSync } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

interface ToolCallEvent {
  toolName?: string;
  input?: { command?: string };
}

interface ExtensionAPI {
  on(
    event: "tool_call",
    handler: (event: ToolCallEvent) => Promise<{ block: boolean; reason: string } | void>,
  ): void;
}

const CHECK_SCRIPT = join(homedir(), ".config", "agent-hooks", "check_command.py");

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", async (event) => {
    if (event.toolName !== "bash") {
      return;
    }
    const command = typeof event.input?.command === "string" ? event.input.command : "";
    try {
      execFileSync("python3", [CHECK_SCRIPT, "--mode", "exit2"], {
        input: JSON.stringify({ command }),
        encoding: "utf8",
        stdio: ["pipe", "pipe", "pipe"],
      });
    } catch (err: unknown) {
      const e = err as { status?: number; stderr?: string };
      if (e.status === 2) {
        return {
          block: true,
          reason: (e.stderr || "命令未显式封闭 stdin，请添加 </dev/null 后重试。").trim(),
        };
      }
      console.error("[enforce-noninteractive] check_command failed:", err);
      return;
    }
  });
}
