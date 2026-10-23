// Status Reporter: tells the local AI status hub what OpenClaw is doing (agent run start/end, which tool is running).
// Read-only observer; it never returns a decision, so it cannot change what the agent does.
import { clip, report } from "../shared/hub.js";

const channelOf = (event, ctx) => String(ctx?.channelId || event?.channelId || event?.channel || "").split(":")[0];

export default {
  id: "status-reporter",
  name: "Status Reporter",
  description: "Reports agent runs and tool calls to the local AI status hub.",
  register(api) {
    // agent_start is not a hook in this OpenClaw build; before_agent_run fires at the same point (this observer returns nothing, so it never blocks)
    api.on("before_agent_run", async (event, ctx) => {
      report({ kind: "agent", state: "start", channel: channelOf(event, ctx) });
    });
    api.on("agent_end", async (event, ctx) => {
      report({ kind: "agent", state: "end", channel: channelOf(event, ctx) });
    });
    api.on(
      "before_tool_call",
      async (event) => {
        const p = event?.params;
        const summary = p?.command ?? p?.path ?? p?.query ?? p?.url ?? p;
        report({ kind: "tool", state: "start", name: String(event?.toolName || "tool"), summary: clip(summary) });
      },
      { priority: 1 },
    );
    api.on("after_tool_call", async (event) => {
      report({ kind: "tool", state: event?.error ? "error" : "end", name: String(event?.toolName || "tool") });
    });
  },
};
