// Exec Guard: tiered gate in front of the agent's shell.
//
//   catastrophic  -> two red confirmations in a row (trusted policy, then an ordinary hook), never saved as "always"
//   danger        -> one red confirmation, never saved
//   sensitive     -> asks the owner, never saved
//   write         -> asks the owner; "Allow always" saves this exact command
//   readonly      -> runs
//   unknown       -> local AI reviewer: only "read_only" runs, anything else asks the owner
//
// The trusted policy fails closed (an error blocks the call). exec itself must be mode "full" so this is the only prompt.
import { classify, normalize } from "./classify.js";
import { review } from "./reviewer.js";
import { audit, isApproved, saveApproved } from "./store.js";
import { report } from "../shared/hub.js";

const EXEC_TOOLS = new Set(["exec", "bash"]);
const PROCESS_INPUT_ACTIONS = new Set(["send-keys", "submit", "paste", "write"]);
const POLICY_ID = "exec-guard-gate";

const clip = (s, n) => (s.length > n ? s.slice(0, n - 1) + "…" : s);
const oneLine = (s) => normalize(s).replace(/\s*\n\s*/g, " ⏎ ");

// description is "<reason>" + newline + "<command>": the Discord card patch (patch_discord_card.py) renders the two parts separately.
const resolved = (d) => (/time/i.test(String(d)) ? "timed_out" : /^(deny|denied|reject)/i.test(String(d)) ? "denied" : "approved");

function approval(cfg, { title, reason, command, severity, always, onResolution, tier }) {
  report({ kind: "guard", guard: "exec", state: "waiting_approval", tier: tier || severity, detail: `${clip(title, 40)}: ${clip(command || "", 80)}` });
  const done = onResolution;
  onResolution = (d) => {
    report({ kind: "guard", guard: "exec", state: resolved(d), tier: tier || severity, detail: `${clip(command || "", 90)} (${String(d)})` });
    return done?.(d);
  };
  return {
    requireApproval: {
      pluginId: "exec-guard",
      title: clip(title, 80),
      description: `${clip(reason || "", 90)}\n${clip(command || "", 160)}`,
      severity,
      allowedDecisions: always ? ["allow-once", "allow-always", "deny"] : ["allow-once", "deny"],
      timeoutMs: cfg.approvalTimeoutMs || 300000,
      timeoutBehavior: "deny",
      onResolution,
    },
  };
}

async function decide(event, cfg, stage) {
  const tool = String(event.toolName || "");
  if (tool === "process") {
    if (stage === 1 && PROCESS_INPUT_ACTIONS.has(String(event.params?.action || ""))) {
      return approval(cfg, {
        title: "Type into a running shell",
        reason: `The agent wants to send input (${event.params.action}) to a running process`,
        command: String(event.params?.keys ?? event.params?.text ?? ""),
        severity: "warning",
        always: false,
      });
    }
    return;
  }
  if (!EXEC_TOOLS.has(tool)) return;

  const command = typeof event.params?.command === "string" ? event.params.command : "";
  const cls = classify(command);
  const shown = oneLine(command);
  const log = (decision, extra = {}) => audit(cfg, { tool, tier: cls.tier, decision, reasons: cls.reasons, command, ...extra });
  if (stage === 1) report({ kind: "guard", guard: "exec", state: "checking", tier: cls.tier, detail: clip(shown, 90) });

  if (cls.tier === "catastrophic") {
    if (stage === 2) {
      return approval(cfg, {
        title: "🛑 FINAL CONFIRMATION 2/2: catastrophic command",
        reason: `Last chance. ${cls.reasons.join("; ")}`,
        command: shown,
        severity: "critical",
        always: false,
        onResolution: (d) => log(`final:${d}`),
      });
    }
    return approval(cfg, {
      title: "🛑 CATASTROPHIC command: confirmation 1/2",
      reason: `${cls.reasons.join("; ")}. A second confirmation follows`,
      command: shown,
      severity: "critical",
      always: false,
      onResolution: (d) => log(`first:${d}`),
    });
  }
  if (stage === 2) return; // the second confirmation exists only for catastrophic commands

  if (cls.tier === "danger") {
    return approval(cfg, {
      title: "⚠️ Dangerous command",
      reason: cls.reasons.join("; "),
      command: shown,
      severity: "critical",
      always: false,
      onResolution: (d) => log(d),
    });
  }
  if (cls.tier === "sensitive") {
    return approval(cfg, {
      title: "🔐 Command touches secrets",
      reason: cls.reasons.join("; "),
      command: shown,
      severity: "warning",
      always: false,
      onResolution: (d) => log(d),
    });
  }
  if (isApproved(cfg, command)) {
    log("allow:saved");
    report({ kind: "guard", guard: "exec", state: "clear", tier: cls.tier, detail: "allowed: saved approval" });
    return;
  }
  if (cls.tier === "readonly") {
    log("allow:readonly");
    report({ kind: "guard", guard: "exec", state: "clear", tier: cls.tier, detail: "allowed: read-only" });
    return;
  }

  let ai = null;
  let why = cls.reasons.join("; ");
  if (cls.tier === "unknown") {
    ai = await review(command, cfg);
    if (ai.verdict === "read_only") {
      log("allow:ai", { ai });
      report({ kind: "guard", guard: "exec", state: "clear", tier: cls.tier, detail: "allowed: AI review read-only" });
      return;
    }
    why = ai.verdict === "unavailable" ? `AI check unavailable (${ai.reason})` : `AI: ${ai.verdict}: ${ai.reason}`;
  }
  const risky = ai?.verdict === "dangerous";
  return approval(cfg, {
    tier: cls.tier,
    title: risky ? "⚠️ AI flagged this command" : "✏️ Command that changes something",
    reason: why,
    command: shown,
    severity: risky ? "critical" : "warning",
    always: !risky,
    onResolution: (d) => {
      log(d, ai ? { ai } : {});
      if (d === "allow-always" && !risky) saveApproved(cfg, command);
    },
  });
}

export default {
  id: "exec-guard",
  name: "Exec Guard",
  description: "Tiered gate for shell commands: double-confirm catastrophes, auto-run read-only, ask the owner for anything that writes.",
  register(api) {
    const cfgOf = () => api.pluginConfig || {};
    // stage 1: trusted policy, fails closed
    api.registerTrustedToolPolicy({
      id: POLICY_ID,
      description: "Classify every exec / process call and ask the owner when it is not plainly read-only.",
      evaluate: async (event) => decide(event, cfgOf(), 1),
    });
    // stage 2: second, independent confirmation for catastrophic commands only
    api.on("before_tool_call", async (event) => decide(event, cfgOf(), 2), { priority: 100 });
  },
};
