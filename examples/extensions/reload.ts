/**
 * Expose the TUI's /reload as a discoverable command.
 *
 * Pi's built-in slash commands only exist in interactive mode; RPC clients
 * such as Agents Anywhere can only run commands that `get_commands` reports.
 * This extension registers /reload so remote clients (web / app) can reload
 * settings, extensions, instructions, and resources like the TUI does.
 *
 * ctx.reload() is terminal for the command handler (it replaces the extension
 * runtime), so the "finished" notification cannot be sent from the handler.
 * Instead the handler drops a timestamped marker and the reloaded extension
 * reports completion on the first lifecycle event. The timestamp expires
 * after RELOAD_NOTICE_TTL_MS so a marker left behind by a reload that never
 * produced an event cannot leak a "finished" notice into a later session or
 * a later process start; stale markers are cleared silently.
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const MARKER = path.join(os.homedir(), ".pi", "agent", ".reload-pending");
const RELOAD_NOTICE_TTL_MS = 10_000;

type NotifyContext = { ui: { notify: (message: string, kind?: string) => void } };

function reportCompletion(ctx: NotifyContext): void {
  if (!fs.existsSync(MARKER)) return;
  const raw = fs.readFileSync(MARKER, "utf8").trim();
  fs.rmSync(MARKER, { force: true });
  const stamp = Number(raw);
  if (!Number.isFinite(stamp) || Date.now() - stamp > RELOAD_NOTICE_TTL_MS) return;
  ctx.ui.notify("重载完成", "info");
}

export default function (pi: ExtensionAPI) {
  for (const event of ["session_start", "turn_start", "agent_start"] as const) {
    pi.on(event, async (_event, ctx) => {
      reportCompletion(ctx as unknown as NotifyContext);
    });
  }

  pi.registerCommand("reload", {
    description: "Reload settings, extensions, instructions, and resources",
    handler: async (_args, ctx) => {
      fs.writeFileSync(MARKER, String(Date.now()));
      ctx.ui.notify("正在重载配置、扩展、指令与资源…", "info");
      await ctx.reload();
    },
  });
}
