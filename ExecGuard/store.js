// Persistent bits: the "always allow this exact command" list and the audit log. Plain JSON / JSONL under the data dir.
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { normalize } from "./classify.js";

export function dataDir(cfg) {
  const d = (cfg && cfg.dataDir) || path.join(os.homedir(), ".openclaw", "exec-guard");
  fs.mkdirSync(d, { recursive: true });
  return d;
}

const approvedFile = (cfg) => path.join(dataDir(cfg), "approved.json");

export function loadApproved(cfg) {
  try {
    return JSON.parse(fs.readFileSync(approvedFile(cfg), "utf8"));
  } catch {
    return {};
  }
}

export function isApproved(cfg, command) {
  return Object.prototype.hasOwnProperty.call(loadApproved(cfg), normalize(command));
}

function writeApproved(cfg, all) {
  const tmp = approvedFile(cfg) + ".tmp";
  fs.writeFileSync(tmp, JSON.stringify(all, null, 2));
  fs.renameSync(tmp, approvedFile(cfg));
}

export function saveApproved(cfg, command) {
  const all = loadApproved(cfg);
  all[normalize(command)] = { at: new Date().toISOString() };
  writeApproved(cfg, all);
}

export function removeApproved(cfg, command) {
  const all = loadApproved(cfg);
  const had = delete all[normalize(command)];
  writeApproved(cfg, all);
  return had;
}

export function audit(cfg, entry) {
  try {
    const line = JSON.stringify({ ts: new Date().toISOString(), ...entry, command: String(entry.command ?? "").slice(0, 2000) });
    fs.appendFileSync(path.join(dataDir(cfg), "audit.jsonl"), line + "\n");
  } catch {
    /* auditing must never break a tool call */
  }
}
