// Reader for the central config.env (see config.env.example in the repo root). Sync, cached, never throws.
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const here = path.dirname(fileURLToPath(import.meta.url));
let cache = null;

export function load() {
  if (cache) return cache;
  cache = {};
  const cands = [process.env.LOCALAI_CONFIG, path.join(here, "config.env"), path.join(here, "..", "config.env")];
  for (const c of cands) {
    try {
      if (!c || !fs.statSync(c).isFile()) continue;
      for (const line of fs.readFileSync(c, "utf8").split(/\r?\n/)) {
        const t = line.trim();
        const i = t.indexOf("=");
        if (t && !t.startsWith("#") && i > 0) cache[t.slice(0, i).trim()] = t.slice(i + 1).trim();
      }
      break;
    } catch {}
  }
  return cache;
}

export function get(key, def = "") {
  const v = load()[key];
  return v ? v : def;
}

export function proxyBase() {
  return `http://${get("HOST", "127.0.0.1")}:${get("PROXY_PORT", "8080")}`;
}
