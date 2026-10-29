/**
 * pi 薄适配器的 Node 夹具（F05）。
 *
 * 用假的 ExtensionAPI 收集 handler，再按宿主调用顺序驱动 `input` → `tool_call`，
 * 每次调用都经真实 `node:child_process` argv（无 shell）启动生成目录里的 Python
 * 核心。夹具本身不实现任何 EasyDev 规则，只回报适配器返回值。
 *
 * 用法：node pi_adapter_fixture.mjs '<json spec>'
 * spec: {entry, plugin, repo, session, activation, transcript?, calls: [{toolName, input, toolCallId}]}
 * transcript 可指定为目录等非普通文件，用来制造核心的降级诊断。
 */
import { pathToFileURL } from "node:url";

const spec = JSON.parse(process.argv[2]);
const handlers = new Map();
const api = {
  on(name, handler) {
    handlers.set(name, handler);
  },
};

const ctx = {
  cwd: spec.repo,
  sessionManager: {
    getSessionId: () => spec.session,
    getSessionFile: () => spec.transcript || "",
  },
};

const module = await import(pathToFileURL(spec.entry).href);
module.default(api);

const results = [];
const inputHandler = handlers.get("input");
if (inputHandler) {
  await inputHandler({ text: spec.activation, source: "user" }, ctx);
}
for (const call of spec.calls) {
  const handler = handlers.get("tool_call");
  const value = handler
    ? await handler(
        {
          toolName: call.toolName,
          toolCallId: call.toolCallId,
          input: call.input,
          parentToolCallId: "",
        },
        ctx,
      )
    : { missing: true };
  results.push({ call, value: value === undefined ? null : value });
}

process.stdout.write(
  JSON.stringify({
    handlers: [...handlers.keys()].sort(),
    results,
  }),
);
