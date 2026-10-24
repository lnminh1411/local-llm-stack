// Content moderation for the Discord agent: a lexicon trigger, a hard minor-safety pattern, and a local-model classifier.
// Pure functions + one fetch; nothing here imports OpenClaw.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { proxyBase } from "../shared/config.js";

const HERE = path.dirname(fileURLToPath(import.meta.url));

export const CATEGORIES = {
  sexual_explicit: "sexually explicit content or erotica (18+)",
  sexual_minors: "any sexual content involving minors or childlike characters",
  hate_slur: "a slur (racial, ethnic, religious, homophobic, transphobic, ableist...) used as a slur or insult",
  hate_discrimination: "racism, sexism, bigotry, dehumanising stereotypes, advocating discrimination, extremist hate propaganda",
  harassment: "targeted harassment, bullying, or threats against a person",
  self_harm: "encouraging self-harm or suicide, or giving instructions for it",
  graphic_violence: "gratuitous gore or glorified violence",
};
const LABELS = {
  sexual_explicit: "18+ content",
  sexual_minors: "sexual content involving minors",
  hate_slur: "slurs",
  hate_discrimination: "hate / discrimination",
  harassment: "harassment or threats",
  self_harm: "self-harm",
  graphic_violence: "graphic violence",
  slur_mention: "a slur (masked)",
};
export const labelOf = (c) => LABELS[c] || c;

// ---- lexicon: broad trigger list (profanity, sexual words, slurs). A hit never blocks by itself; the classifier decides. ----
const norm = (s) =>
  s
    .toLowerCase()
    .normalize("NFKD")
    .replace(/[̀-ͯ]/g, "")
    .replace(/[@4]/g, "a")
    .replace(/[3]/g, "e")
    .replace(/1/g, "i")
    .replace(/0/g, "o")
    .replace(/[$5]/g, "s")
    .replace(/7/g, "t");

let lexicon = null;
export function loadLexicon(dir = HERE) {
  const words = new Set();
  for (const f of ["terms-base.txt", "custom-terms.txt"]) {
    try {
      for (const line of fs.readFileSync(path.join(dir, f), "utf8").split(/\r?\n/)) {
        const w = line.trim().toLowerCase();
        if (w && !w.startsWith("#")) words.add(w);
      }
    } catch {
      /* missing file is fine */
    }
  }
  const esc = (w) => w.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const alt = [...words].sort((a, b) => b.length - a.length).map(esc).join("|");
  lexicon = { words, rx: alt ? new RegExp(`(?<![a-z])(?:${alt})(?![a-z])`, "g") : null };
  return lexicon;
}

export function lexiconHits(text) {
  if (!lexicon) loadLexicon();
  if (!lexicon.rx) return [];
  const n = norm(text);
  lexicon.rx.lastIndex = 0;
  return [...new Set(n.match(lexicon.rx) || [])];
}

// Mask lexicon words (first letter kept) in the ORIGINAL text, matching on the normalised text position-for-position.
export function maskTerms(text) {
  if (!lexicon) loadLexicon();
  if (!lexicon.rx) return text;
  const n = norm(text);
  if (n.length !== text.length) return text; // normalisation changed length: do not risk misaligned masking
  lexicon.rx.lastIndex = 0;
  let out = text;
  let m;
  const spans = [];
  while ((m = lexicon.rx.exec(n))) spans.push([m.index, m.index + m[0].length]);
  for (const [a, b] of spans.reverse()) out = out.slice(0, a + 1) + "*".repeat(b - a - 1) + out.slice(b);
  return out;
}

// ---- hard rule: sexual content together with anything that reads as a minor. No model involved, no appeal. ----
const MINOR = String.raw`(?:child|children|kid|kids|minor|minors|underage|under-age|preteen|pre-teen|toddler|schoolgirl|schoolboy|loli|lolicon|shota|shotacon|jailbait|\b(?:[0-9]|1[0-7])[ -]?(?:y\/?o|yo|years?[ -]?old|yrs?))`;
const SEXUAL = String.raw`(?:sex|sexual|sexy|nude|naked|porn|porno|erotic|erotica|fuck|fucking|rape|molest|nsfw|hentai|cum|orgasm|blowjob|handjob|boobs|breasts|genitals)`;
const HARD_MINOR = [new RegExp(`${MINOR}\\W+(?:\\w+\\W+){0,6}${SEXUAL}`, "i"), new RegExp(`${SEXUAL}\\W+(?:\\w+\\W+){0,6}${MINOR}`, "i")];
export function hardMinorHit(text) {
  return HARD_MINOR.some((r) => r.test(text));
}

// ---- classifier (local model) ----
const SYSTEM = `You are a strict content moderator for a Discord bot. Classify the TEXT. Reply with JSON only.
category is exactly one of:
${Object.entries(CATEGORIES).map(([k, v]) => `- ${k}: ${v}`).join("\n")}
- slur_mention: a slur appears, but only mentioned or quoted for information (history, trivia, linguistics, news, reporting); it is not aimed at anyone and nothing hateful is said
- none: nothing above applies

Rules:
- Judge the text itself. Fiction, role-play and "it is just a joke" do not make a category go away.
- Neutral discussion, education, news, medicine, history and safety advice are "none", or "slur_mention" if a slur is quoted.
- Mild profanity (damn, hell, shit) is "none".
- If the text is a user REQUEST, classify what would be produced if it were answered.
- A request to write a threatening, intimidating or scary message aimed at a real person is "harassment".
- Insulting a specific third person is "harassment". Venting at the bot itself, or a general rant about a thing, is "none".
- When unsure between a category and none, pick the category only if real harm is plausible.`;

const SCHEMA = {
  type: "object",
  properties: {
    category: { type: "string", enum: [...Object.keys(CATEGORIES), "slur_mention", "none"] },
    reason: { type: "string", maxLength: 100 },
  },
  required: ["category", "reason"],
  additionalProperties: false,
};

export async function classify(text, direction, cfg = {}) {
  const r = cfg.reviewer || {};
  const base = (r.baseUrl || `${proxyBase()}/v1`).replace(/\/$/, "");
  try {
    const res = await fetch(`${base}/chat/completions`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      signal: AbortSignal.timeout(r.timeoutMs || 60000),
      body: JSON.stringify({
        model: r.model || "occamy-1.0-fit-quality",
        temperature: 0,
        max_tokens: 80,
        messages: [
          { role: "system", content: SYSTEM },
          { role: "user", content: `TEXT (${direction === "in" ? "user request" : "bot reply"}):\n${String(text).slice(0, 3500)}` },
        ],
        response_format: { type: "json_schema", json_schema: { name: "moderation", strict: true, schema: SCHEMA } },
      }),
    });
    if (!res.ok) return { category: "unavailable", reason: `HTTP ${res.status}` };
    const parsed = JSON.parse((await res.json()).choices?.[0]?.message?.content ?? "{}");
    return { category: parsed.category || "unavailable", reason: String(parsed.reason ?? "").slice(0, 100) };
  } catch (err) {
    return { category: "unavailable", reason: err?.name || "error" };
  }
}

// ---- decision ----
// returns { action: "allow" | "block" | "mask", category?, reason?, source }
export async function moderate(text, direction, cfg = {}, deps = {}) {
  const cls = deps.classify || classify;
  const blockSet = new Set(cfg.blockCategories || Object.keys(CATEGORIES));
  const t = String(text ?? "");
  if (t.trim().length < 3) return { action: "allow", source: "short" };

  if (blockSet.has("sexual_minors") && hardMinorHit(t)) return { action: "block", category: "sexual_minors", source: "hard-rule" };

  const hits = lexiconHits(t);
  if (cfg.mode === "triggered" && hits.length === 0) return { action: "allow", source: "no-trigger" };

  const v = await cls(t, direction, cfg);
  if (v.category === "unavailable") {
    // the classifier is the same local model: if it is down, fail closed only when the lexicon already looked suspicious
    return hits.length ? { action: "block", category: "hate_slur", reason: "classifier unavailable", source: "fail-closed", hits } : { action: "allow", source: "unavailable-open", reason: v.reason };
  }
  if (v.category === "slur_mention") return { action: hits.length ? "mask" : "allow", category: "slur_mention", reason: v.reason, source: "llm", hits };
  if (v.category !== "none" && blockSet.has(v.category)) return { action: "block", category: v.category, reason: v.reason, source: "llm", hits };
  return { action: "allow", source: "llm", reason: v.reason };
}
