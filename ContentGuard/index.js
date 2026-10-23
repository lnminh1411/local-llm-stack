// Content Guard: Discord-only guardrails for an uncensored model.
//   inbound  (before_agent_run)       a request for blocked content never reaches the model; the user gets a short refusal
//   outbound (reply_payload_sending,
//             message_sending)        a blocked reply is replaced by a notice; a quoted slur is masked (n*****)
// Only the Discord channel is touched. Web UI, the bridge and the CLI are not affected.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { labelOf, moderate } from "./moderate.js";
import { report } from "../shared/hub.js";

const isDiscord = (...v) => v.some((x) => typeof x === "string" && x.toLowerCase().startsWith("discord"));

function audit(cfg, entry) {
  try {
    const dir = cfg.dataDir || path.join(os.homedir(), ".openclaw", "content-guard");
    fs.mkdirSync(dir, { recursive: true });
    fs.appendFileSync(path.join(dir, "audit.jsonl"), JSON.stringify({ ts: new Date().toISOString(), ...entry }) + "\n");
  } catch {
    /* never break delivery over a log line */
  }
}
// verdict -> hub: "allow" clears, anything else shows as tripped for a while
const gstate = (dir, v, text) =>
  report({
    kind: "guard",
    guard: "content",
    state: v.action === "allow" ? "clear" : "tripped",
    tier: v.category || "",
    detail: v.action === "allow" ? `${dir} ok` : `${dir} ${v.action}: ${labelOf(v.category)}: ${String(text ?? "").replace(/\s+/g, " ").slice(0, 60)}`,
  });
const snippet =(t) => String(t ?? "").replace(/\s+/g, " ").slice(0, 120);

const replyNotice = (v) => `🚫 Content guard: that reply was held back (${labelOf(v.category)}). Ask me something else.`;
const inboundNotice = (v) => `🚫 I won't do that on Discord (${labelOf(v.category)}). The content guard is on.`;

export default {
  id: "content-guard",
  name: "Content Guard",
  description: "Discord-only content guardrails (18+, slurs, hate, harassment, self-harm, gore) in front of an uncensored local model.",
  register(api) {
    const cfgOf = () => api.pluginConfig || {};

    // ---- inbound ----
    api.on(
      "before_agent_run",
      async (event) => {
        const cfg = cfgOf();
        if (!isDiscord(event.channelId) || cfg.enabled === false) return;
        if (event.senderIsOwner && cfg.ownerExempt === true) return;
        report({ kind: "guard", guard: "content", state: "checking", detail: "inbound" });
        const v = await moderate(event.prompt, "in", cfg);
        gstate("in", v, event.prompt);
        if (v.action === "block") {
          audit(cfg, { dir: "in", ...v, text: snippet(event.prompt) });
          return { outcome: "block", reason: `content-guard:${v.category}`, message: inboundNotice(v), category: v.category };
        }
      },
      { priority: 90 },
    );

    // ---- outbound: normal replies ----
    api.on(
      "reply_payload_sending",
      async (event, ctx) => {
        const cfg = cfgOf();
        if (!isDiscord(event.channel, ctx?.channelId) || cfg.enabled === false) return;
        const text = event.payload?.text;
        if (!text || !String(text).trim()) return;
        report({ kind: "guard", guard: "content", state: "checking", detail: "outbound reply" });
        const v = await moderate(text, "out", cfg);
        gstate("out", v, text);
        if (v.action === "block") {
          audit(cfg, { dir: "out", ...v, text: snippet(text) });
          const { mediaUrl, mediaUrls, ...rest } = event.payload;
          return { payload: { ...rest, text: replyNotice(v) } };
        }
        if (v.action === "mask") {
          audit(cfg, { dir: "out", ...v, text: snippet(text) });
          const { maskTerms } = await import("./moderate.js");
          return { payload: { ...event.payload, text: maskTerms(String(text)) } };
        }
      },
      { priority: 90 },
    );

    // ---- outbound: messages the agent sends with the message tool ----
    api.on(
      "message_sending",
      async (event, ctx) => {
        const cfg = cfgOf();
        if (!isDiscord(ctx?.channelId) || cfg.enabled === false) return;
        report({ kind: "guard", guard: "content", state: "checking", detail: "outbound message tool" });
        const v = await moderate(event.content, "out", cfg);
        gstate("out-tool", v, event.content);
        if (v.action === "block") {
          audit(cfg, { dir: "out-tool", ...v, text: snippet(event.content) });
          return { cancel: true, cancelReason: `content-guard:${v.category}` };
        }
        if (v.action === "mask") {
          audit(cfg, { dir: "out-tool", ...v, text: snippet(event.content) });
          const { maskTerms } = await import("./moderate.js");
          return { content: maskTerms(String(event.content)) };
        }
      },
      { priority: 90 },
    );
  },
};
