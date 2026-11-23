import { lstatSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// 让独立运行的 pi 也遵循 easydev 用户级 models.json 的 windows（北京时间 UTC+8，左闭右开，
// 支持跨午夜，按键序命中首项）。只在 reason 为 startup/new 时生效：resume/fork/reload 沿用
// 会话已锁定的模型；命令行显式 --model/-m 一律不覆盖。校验范围小于 scripts/models.py
// （只做软链/大小/JSON/基础结构），windows 键格式错误静默跳过，差异见 easypi/README.md。
const MAX_CONFIG_BYTES = 512 * 1024;
const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000;
const WINDOW_KEY = /^([01][0-9]|2[0-3]):([0-5][0-9])-([01][0-9]|2[0-3]):([0-5][0-9])$/;
const THINKING_LEVELS = new Set(["off", "minimal", "low", "medium", "high", "xhigh", "max"]);

interface SessionStartEvent {
  reason?: string;
}

interface ExtensionContext {
  modelRegistry?: { find(provider: string, modelId: string): unknown };
  ui?: { notify(message: string, level?: string): void };
}

interface ExtensionAPI {
  on(
    event: "session_start",
    handler: (event: SessionStartEvent, ctx: ExtensionContext) => Promise<void>,
  ): void;
  setModel(model: unknown): Promise<boolean>;
  setThinkingLevel(level: string): void;
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return typeof value === "object" && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export default function (pi: ExtensionAPI) {
  pi.on("session_start", async (event, ctx) => {
    if (event.reason !== "startup" && event.reason !== "new") {
      return;
    }
    const argv = process.argv;
    if (argv.some((arg) => /^(--model|-m)(=|$)/.test(arg))) {
      return;
    }
    const thinkingGiven = argv.some((arg) => /^--thinking(=|$)/.test(arg));

    const warn = (message: string) => {
      console.error(`[apply-model-windows] ${message}`);
      ctx.ui?.notify(message, "warning");
    };

    const appData = process.env.APPDATA;
    const configPath =
      process.platform === "win32" && appData
        ? join(appData, "easydev", "models.json")
        : join(homedir(), ".config", "easydev", "models.json");

    let raw: string;
    try {
      const info = lstatSync(configPath);
      if (!info.isFile() || info.isSymbolicLink()) {
        warn(`用户级 models.json 不是普通文件，已跳过 windows 解析: ${configPath}`);
        return;
      }
      if (info.size > MAX_CONFIG_BYTES) {
        warn(`用户级 models.json 超过 512 KiB，已跳过 windows 解析: ${configPath}`);
        return;
      }
      raw = readFileSync(configPath, "utf8");
    } catch (error) {
      if ((error as { code?: string }).code !== "ENOENT") {
        warn(`用户级 models.json 不可读，已跳过 windows 解析: ${configPath}`);
      }
      return;
    }

    let config: unknown;
    try {
      config = JSON.parse(raw);
    } catch {
      warn(`用户级 models.json 不是合法 JSON，已跳过 windows 解析: ${configPath}`);
      return;
    }
    const root = asRecord(config);
    if (root === null) {
      warn(`用户级 models.json 顶层不是对象，已跳过 windows 解析: ${configPath}`);
      return;
    }

    // 基座 developer + 命中时段局部覆盖：只覆写 reasoning_effort 时保留基座 model。
    let developer: Record<string, unknown> = asRecord(root.developer) ?? {};
    const windows = asRecord(root.windows);
    if (windows !== null) {
      const now = new Date(Date.now() + BEIJING_OFFSET_MS);
      const minuteOfDay = now.getUTCHours() * 60 + now.getUTCMinutes();
      for (const [key, value] of Object.entries(windows)) {
        const match = WINDOW_KEY.exec(key);
        if (match === null) {
          continue;
        }
        const start = Number(match[1]) * 60 + Number(match[2]);
        const end = Number(match[3]) * 60 + Number(match[4]);
        if (start === end) {
          continue;
        }
        const hit = start < end
          ? minuteOfDay >= start && minuteOfDay < end
          : minuteOfDay >= start || minuteOfDay < end;
        if (!hit) {
          continue;
        }
        const entry = asRecord(value);
        const override = entry === null ? null : asRecord(entry.developer);
        if (override !== null) {
          developer = { ...developer, ...override };
        }
        break;
      }
    }

    const model = developer.model;
    if (typeof model === "string" && model !== "inherit") {
      const slash = model.indexOf("/");
      const found = slash > 0 && ctx.modelRegistry !== undefined
        ? ctx.modelRegistry.find(model.slice(0, slash), model.slice(slash + 1))
        : undefined;
      if (found === undefined) {
        warn(`windows 解析出的模型未在当前 pi 注册，保留原模型: ${model}`);
      } else if (!(await pi.setModel(found))) {
        warn(`pi 未配置该模型的凭证，保留原模型: ${model}`);
      }
    }

    const effort = developer.reasoning_effort;
    if (!thinkingGiven && typeof effort === "string" && effort !== "inherit") {
      if (THINKING_LEVELS.has(effort)) {
        pi.setThinkingLevel(effort);
      } else {
        warn(`windows 解析出的 reasoning_effort 不在 Pi 允许档位内: ${effort}`);
      }
    }
  });
}
