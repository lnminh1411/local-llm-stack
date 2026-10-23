// AI reviewer: asks the local model whether a command is read-only. It can only ever say "read_only" or hand the command to the owner.
const SYSTEM = `You classify one Windows PowerShell / shell command for a safety gate. Reply with JSON only.
verdict:
- "read_only": the command only reads or displays information. It creates, changes, deletes, moves, downloads, installs, starts, stops or sends nothing, and writes no file (no redirection, no output files).
- "writes": it changes or creates anything, even a temp file or a setting, or you cannot tell.
- "dangerous": it could delete data, change the system, stop processes or reveal secrets.
When in doubt answer "writes". Never answer read_only for a command you do not recognise.`;

const SCHEMA = {
  type: "object",
  properties: {
    verdict: { type: "string", enum: ["read_only", "writes", "dangerous"] },
    reason: { type: "string", maxLength: 100 },
  },
  required: ["verdict", "reason"],
  additionalProperties: false,
};

export async function review(command, cfg = {}) {
  const r = cfg.reviewer || {};
  if (r.enabled === false) return { verdict: "unavailable", reason: "reviewer disabled" };
  const base = (r.baseUrl || "http://127.0.0.1:8080/v1").replace(/\/$/, "");
  try {
    const res = await fetch(`${base}/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      signal: AbortSignal.timeout(r.timeoutMs || 90000),
      body: JSON.stringify({
        model: r.model || "occamy-1.0-fit-quality",
        temperature: 0,
        max_tokens: 80,
        messages: [
          { role: "system", content: SYSTEM },
          { role: "user", content: `Command:\n${String(command).slice(0, 1500)}` },
        ],
        response_format: { type: "json_schema", json_schema: { name: "verdict", strict: true, schema: SCHEMA } },
      }),
    });
    if (!res.ok) return { verdict: "unavailable", reason: `reviewer HTTP ${res.status}` };
    const body = await res.json();
    const parsed = JSON.parse(body.choices?.[0]?.message?.content ?? "{}");
    if (!["read_only", "writes", "dangerous"].includes(parsed.verdict)) return { verdict: "unavailable", reason: "bad reviewer output" };
    return { verdict: parsed.verdict, reason: String(parsed.reason ?? "").slice(0, 100) };
  } catch (err) {
    return { verdict: "unavailable", reason: `reviewer error: ${err?.name || "error"}` };
  }
}
