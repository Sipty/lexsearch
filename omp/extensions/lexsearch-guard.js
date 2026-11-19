import { spawnSync } from "node:child_process";
import { join } from "node:path";

// lexsearch guard for omp: hands each tool call to lexsearch-guard, which blocks scans of the
// notes vault and tells the model to search with lexsearch instead. Installed by install.sh.
const GUARD = process.env.HOME ? join(process.env.HOME, ".local", "bin", "lexsearch-guard") : "lexsearch-guard";

export default function lexsearchGuard(pi) {
  pi.on("tool_call", (event, ctx) => {
    const call = { tool_name: event.toolName, tool_input: event.input, cwd: (ctx && ctx.cwd) || process.cwd() };
    const run = spawnSync(GUARD, [], { input: JSON.stringify(call), encoding: "utf8", timeout: 10000 });
    // Anything but a deliberate "no" (exit 2) lets the call through: a broken guard must not stop the agent.
    if (run.status === 2) return { block: true, reason: (run.stderr || "").trim() };
  });
}
