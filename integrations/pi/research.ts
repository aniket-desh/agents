// Same-process continuation for Pi. No child agent or model API is started.
// API: https://github.com/earendil-works/pi/blob/main/packages/coding-agent/src/core/extensions/types.ts
import { homedir } from "node:os";
import { join } from "node:path";

/** @param {import("@earendil-works/pi-coding-agent").ExtensionAPI} pi */
export default function (pi) {
  let timer;
  let generation = 0;

  function cancel() {
    generation += 1;
    if (timer) clearTimeout(timer);
    timer = undefined;
  }

  async function decisionFor(ctx) {
    const command = process.env.RESEARCH_COMPUTE_BIN || join(homedir(), ".local/bin/research-compute");
    const result = await pi.exec(command, ["session", "continuation", "--agent", "pi",
      "--conversation-id", ctx.sessionManager.getSessionId(), "--workspace", ctx.cwd], { timeout: 5000 });
    return result.code === 0 ? JSON.parse(result.stdout) : {};
  }

  function hasPrompt(decision) {
    return decision?.continue === true && typeof decision.prompt === "string" &&
      decision.prompt.trim() && decision.prompt.length <= 8000;
  }

  /** @param {import("@earendil-works/pi-coding-agent").ExtensionContext} ctx */
  function schedule(ctx, delay = 0) {
    const epoch = generation;
    const nativeId = ctx.sessionManager.getSessionId();
    timer = setTimeout(async () => {
      timer = undefined;
      if (generation !== epoch || ctx.sessionManager.getSessionId() !== nativeId ||
          !ctx.isIdle() || ctx.hasPendingMessages()) return;
      try {
        const decision = await decisionFor(ctx);
        if (generation !== epoch || ctx.sessionManager.getSessionId() !== nativeId ||
            !ctx.isIdle() || ctx.hasPendingMessages()) return;
        if (hasPrompt(decision)) {
          pi.sendUserMessage(decision.prompt, { deliverAs: "followUp" });
        } else if (decision.waiting === true) {
          // Poll only the local controller while admitted jobs run, without
          // model turns. Pause/finish/budget expiry removes this wait state.
          schedule(ctx, 15000);
        }
      } catch {
        // Fail closed for continuation; the independent watchdog owns GPUs.
      }
    }, delay);
  }

  pi.on("before_agent_start", async (_event, ctx) => ({
    message: {
      customType: "research-compute-binding",
      content: "Research compute binding context (not compute authorization): " + JSON.stringify({
        agent: "pi", conversation_id: ctx.sessionManager.getSessionId(), workspace: ctx.cwd,
      }) + ". Bind a user-requested investigation to this exact conversation and workspace.",
      display: false,
    },
  }));
  pi.on("agent_start", async () => { cancel(); });
  // agent_end can precede automatic retry/compaction. Current Pi exposes this
  // final actionable boundary specifically for safe extension continuation.
  pi.on("agent_before_settle", async (event, ctx) => {
    cancel();
    if (event.outcome !== "completed" || event.continue || !event.context.canContinue ||
        event.context.pendingMessages.length || ctx.hasPendingMessages()) return;
    const epoch = generation;
    try {
      const decision = await decisionFor(ctx);
      if (generation !== epoch || ctx.hasPendingMessages()) return;
      if (hasPrompt(decision)) return {
        entries: [{ type: "custom_message", customType: "research-compute-continuation",
          content: decision.prompt, display: true }],
        continue: true,
      };
      if (decision.waiting === true) schedule(ctx, 15000);
    } catch {
      // A missing/unavailable controller must not cause an autonomous turn.
    }
  });
  pi.on("session_start", async () => { cancel(); });
  pi.on("session_before_switch", async () => { cancel(); });
  pi.on("session_before_fork", async () => { cancel(); });
  pi.on("session_shutdown", async () => { cancel(); });
}
