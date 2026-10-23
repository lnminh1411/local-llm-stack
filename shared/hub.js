// Fire-and-forget status reports to the wake proxy's live status hub (ai_hub.py next to llama-wake.py).
// Never throws, never delays the caller: a dead proxy just means nobody sees the status.
const URL_ = process.env.AI_HUB_URL || "http://127.0.0.1:8080/_gate/event";

export function report(ev) {
  try {
    fetch(URL_, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ source: "openclaw", ...ev }),
      signal: AbortSignal.timeout(800),
    }).catch(() => {});
  } catch {
    /* ignore */
  }
}

export const clip = (v, n = 100) => {
  const s = typeof v === "string" ? v : JSON.stringify(v ?? "");
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
};
