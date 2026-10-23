// Pure command classifier for the exec guard. No I/O, no OpenClaw imports, so it is unit-testable on its own.
//
// classify(command) -> { tier, reasons[] }
//   tier "catastrophic"  can wreck the machine or its data: double confirmation (red), never saved as "always"
//   tier "danger"        destructive or system-changing: single red confirmation, never saved as "always"
//   tier "sensitive"     touches secrets or credentials: asks the owner, never saved as "always"
//   tier "write"         writes / changes something: asks the owner, may be saved as "always"
//   tier "readonly"      provably read-only by the allowlist below: runs without asking
//   tier "unknown"       nothing alarming, but not on the allowlist: the AI reviewer decides (it can only say "read only")
//
// Windows-first: exec runs under PowerShell 7 / 5.1 on this host.

const rx = (s, f = "i") => new RegExp(s, f);

// ---- catastrophic: format a disk, wipe a root, reboot, tamper with boot/backup/accounts/firewall ------------------
const ROOT = String.raw`(?:[a-z]:[\\/]?|[a-z]:[\\/](?:windows|users|program files(?: \(x86\))?|programdata|system32)[\\/]?|~[\\/]?|\$home[\\/]?|\$env:(?:systemroot|windir|userprofile|homedrive|programfiles|programdata|systemdrive)[\\/]?|%(?:systemroot|windir|userprofile|systemdrive)%[\\/]?|[\\/])`;
const DELETE_VERB = String.raw`(?:remove-item|rm|ri|del|erase|rd|rmdir)`;
const CATASTROPHIC = [
  [rx(String.raw`(?:^|[\s;|&(])format(?:\.com)?\s+[a-z]:`), "formats a drive"],
  [rx(String.raw`\b(?:Format-Volume|Clear-Disk|Remove-Partition|Initialize-Disk|Set-Disk|Remove-Volume|Resize-Partition|New-Partition)\b`), "changes disk partitions or volumes"],
  [rx(String.raw`\b(?:diskpart|bcdedit|bootrec|mbr2gpt|mountvol|diskshadow)\b`), "edits disks or the boot configuration"],
  [rx(String.raw`\bvssadmin\b.*\bdelete\b|\bwbadmin\b.*\bdelete\b|\bwmic\b.*\bshadowcopy\b.*\bdelete\b`), "deletes backups / shadow copies"],
  [rx(String.raw`\bcipher\b.*\s/w`), "wipes free disk space"],
  [rx(String.raw`\b(?:shutdown(?:\.exe)?|Restart-Computer|Stop-Computer|Suspend-Computer|logoff)\b`), "shuts down, restarts or logs off the PC"],
  [rx(String.raw`\b(?:rundll32\b.*(?:shutdown|SetSuspendState)|Add-Computer|Remove-Computer|Rename-Computer)\b`), "suspends the PC or changes its domain / name"],
  [rx(String.raw`\bnet1?\s+(?:user|localgroup|accounts)\b|\b(?:New|Remove|Set|Disable)-Local(?:User|Group)\b|\bnet\s+user\b`), "changes user accounts or groups"],
  [rx(String.raw`\bSet-ExecutionPolicy\b|\b(?:Set|Add|Remove)-MpPreference\b|\bDisable-(?:NetFirewallRule|WindowsOptionalFeature)\b|\bSet-NetFirewallProfile\b|\bnetsh\s+advfirewall\b`), "weakens Windows security or the firewall"],
  [rx(String.raw`\breg(?:\.exe)?\s+(?:delete|add|import|load|unload|restore)\s+["']?hk(?:lm|ey_local_machine)\b|\b(?:Set|New|Remove)-(?:Item)?Property\b.*\bhklm:`), "writes the machine registry"],
  [rx(String.raw`\b(?:dism|sfc)\b.*\s/(?:remove|cleanup|revert)`), "removes Windows components"],
  [rx(String.raw`\bwevtutil\b.*\bcl\b|\bClear-EventLog\b`), "clears Windows event logs"],
  // recursive / forced delete aimed at a drive root, Windows, Users, Program Files, or the home folder
  [rx(String.raw`\b${DELETE_VERB}\b[^|;&]*?(?:^|[\s'"])${ROOT}(?:\*)?(?=['"\s;|&]|$)`), "deletes a drive root or a system / home folder"],
  [rx(String.raw`\b(?:rd|rmdir)\b[^|;&]*\s/s\b[^|;&]*[a-z]:[\\/]?(?=['"\s;|&]|$)`), "deletes a drive root"],
  [rx(String.raw`\brobocopy\b.*\s/(?:mir|purge)\b`), "mirror / purge copy deletes files at the destination"],
];

// ---- danger: destructive or system-changing, but scoped -------------------------------------------------------------
const DANGER = [
  [rx(String.raw`\b${DELETE_VERB}\b[^|;&]*(?:\s-recurse\b|\s-r\b|\s-force\b|\s/s\b|\s/q\b|[*?])`), "recursive / forced / wildcard delete"],
  [rx(String.raw`\b(?:Stop-Process|taskkill|pkill|kill|Stop-Service|Suspend-Process|Restart-Service)\b`), "stops processes or services"],
  [rx(String.raw`\b(?:sc(?:\.exe)?\s+(?:stop|delete|config|create)|New-Service|Set-Service|Remove-Service)\b`), "changes Windows services"],
  [rx(String.raw`\b(?:schtasks|Register-ScheduledTask|Unregister-ScheduledTask|Set-ScheduledTask)\b`), "changes scheduled tasks"],
  [rx(String.raw`\breg(?:\.exe)?\s+(?:delete|add|import)\b|\b(?:Set|New|Remove)-(?:Item)?Property\b.*\bhk(?:cu|cr|u):|\bregedit\b`), "writes the registry"],
  [rx(String.raw`\b(?:takeown|icacls|cacls|Set-Acl)\b|\battrib\b.*[+-][sh]\b`), "changes permissions or hides files"],
  [rx(String.raw`\b(?:Invoke-Expression|iex)\b|-enc(?:odedcommand)?\b|\bFromBase64String\b`), "runs dynamically built or encoded code"],
  [rx(String.raw`\b(?:Invoke-WebRequest|iwr|Invoke-RestMethod|irm|curl|wget|Start-BitsTransfer|bitsadmin|certutil)\b.*(?:\|\s*(?:iex|Invoke-Expression|sh|bash|powershell|pwsh|cmd))`), "downloads and runs code"],
  [rx(String.raw`\b(?:Start-Process\b.*-Verb\s+runas|runas)\b`), "runs something as administrator"],
  [rx(String.raw`\b(?:Uninstall-\w+|winget\s+uninstall|msiexec\b.*\s/x|choco\s+uninstall|pip3?\s+uninstall|npm\s+uninstall)\b`), "uninstalls software"],
  [rx(String.raw`\bgit\b.*\b(?:push\b.*--force|push\b.*\s-f\b|reset\s+--hard|clean\s+-[a-z]*f)`), "rewrites or discards git history / work"],
  [rx(String.raw`\b(?:mshta|wscript|cscript|regsvr32|rundll32)\b`), "runs a Windows script host / loader"],
  [rx(String.raw`\b(?:ollama\s+(?:rm|stop)|openclaw\s+(?:gateway|plugins|config|approvals|exec-policy|uninstall|reset|update))\b`), "changes the local AI stack itself"],
];

// ---- sensitive: reads or touches secrets ------------------------------------------------------------------------------
const SENSITIVE = [
  [rx(String.raw`(?:^|[\\/\s'"=])\.env(?:\.\w+)?\b|openclaw\.json|exec-approvals|\.openclaw[\\/](?:credentials|identity|agents)`), "touches env files or OpenClaw secrets"],
  [rx(String.raw`[\\/]\.ssh\b|\bid_(?:rsa|ed25519|ecdsa)\b|\.(?:pem|pfx|ppk|kdbx|p12)\b|\.aws[\\/]|\.npmrc|\.pypirc|\.netrc|\.git-credentials|\.docker[\\/]config`), "touches keys or credential files"],
  [rx(String.raw`\b(?:Login Data|Cookies|Web Data|Local State|logins\.json|key[34]\.db)\b`), "touches browser credential stores"],
  [rx(String.raw`\b(?:Get-Credential|cmdkey|vaultcmd|mimikatz|lsass|ntds|procdump)\b|[\\/]config[\\/]sam\b|\bsecurity\s+find-`), "touches the credential store or LSASS"],
  [rx(String.raw`\b(?:password|passwd|secret|api[_-]?key|private[_-]?key|wallet|seed[_ ]?phrase|token)s?\b`), "mentions passwords, keys, tokens or wallets"],
];

// ---- write indicators (global veto: anything here is never auto-allowed) ---------------------------------------------
const PS_VERBS = ["Set", "New", "Remove", "Add", "Clear", "Copy", "Move", "Rename", "Export", "Install", "Uninstall", "Update", "Enable",
  "Disable", "Start", "Stop", "Restart", "Suspend", "Resume", "Register", "Unregister", "Invoke", "Send", "Write", "Save", "Grant", "Revoke",
  "Reset", "Lock", "Unlock", "Mount", "Dismount", "Optimize", "Repair", "Expand", "Compress", "Publish", "Sync", "Unblock", "Block", "Enter",
  "Exit", "Debug", "Edit", "Import", "Out", "Wait", "Push", "Pop", "Use", "Join", "Split", "Show", "Open", "Close", "Read", "Request", "Submit"];
// verb-noun cmdlets that are fine even though the verb is on the list
const PS_HARMLESS = new Set([
  "out-null", "out-string", "out-host", "write-output", "write-host", "write-verbose", "write-warning", "write-error", "write-debug",
  "write-information", "set-location", "push-location", "pop-location", "set-variable", "set-strictmode", "import-csv", "import-clixml",
  "import-powershelldatafile", "join-path", "split-path", "read-host", "wait-process", "show-command"].filter((n) => n !== "read-host" && n !== "show-command"));
const WRITE_VERB_RX = rx(String.raw`(?:^|[\s;|&(\{=])((?:${PS_VERBS.join("|")})-[a-z][\w]*)`, "ig");
const WRITE_NATIVE = rx(String.raw`(?:^|[\s;|&(\{=])(?:mkdir|md|rmdir|rd|del|erase|rm|ri|mv|move|mi|cp|copy|cpi|ren|rni|rename|touch|ni|sc|sv|tee|tee-object|sed|truncate|dd|fsutil|robocopy|xcopy|expand|compact|attrib|icacls|takeown|mklink|ln|chmod|chown|setx|assoc|ftype|msiexec|winget|choco|scoop|gcc|clang|cl|make|cmake|powershell|pwsh|cmd|bash|sh|wsl|start|call|schtasks|net1?|netsh|reg|regedit|taskkill|kill|pkill|curl|wget|iwr|irm|ssh|scp|sftp|ftp|rsync|code|notepad|explorer|shutdown|bitsadmin|certutil|mshta|wscript|cscript|rundll32|regsvr32|llama-server|agy|claude|ipconfig\s+/(?:release|renew|flushdns|registerdns)|nvidia-smi\s+-(?:pm|pl|ac|r|lgc|lmc|rgc)\b|clip|msg|sendkeys|taskmgr|diskpart|sfc|dism|chkdsk|defrag|powercfg|sc\.exe|wmic|gpupdate|wuauclt|usoclient)(?:\.exe|\.cmd|\.bat|\.ps1)?(?=[\s;|&)\}]|$)`, "i");
const REDIRECT_SAFE = /\d?>\s*&\d|\*>\s*&\d|\d?>>?\s*\$null\b|\*>\s*\$null\b|\d?>>?\s*nul\b/gi;
const HARD_CONSTRUCTS = [
  [/`/, "backtick escapes"],
  [/\$\(/, "subexpression $()"],
  [/(?:^|[;|&(\s])&\s*[\w"'$.\\/(]/, "call operator &"],
  [/(?:^|[;|&])\s*\.\s+[\w"'$.\\/]/, "dot-sourcing"],
  [/::/, "static .NET call"],
  [/\[(?:io|system|reflection|diagnostics|net|microsoft|runtime)[.\w]*\]/i, ".NET type access"],
  [/\bAdd-Type\b|\bNew-Object\b|\bInvoke-Command\b|\bicm\b/i, "dynamic objects / remoting"],
  [/<<|@['"]/, "here-string"],
];

// ---- read-only allowlist --------------------------------------------------------------------------------------------
// PowerShell cmdlet name prefixes that only read or format.
const RO_PS_PREFIX = /^(?:get|select|where|sort|measure|group|format|convertto|convertfrom|compare|test|resolve|split|join|out|write|foreach|tee-object)-?/i;
const RO_PS_EXACT = new Set([
  "get-childitem", "get-content", "get-item", "get-itemproperty", "get-process", "get-service", "get-date", "get-location", "get-command", "get-help",
  "select-object", "select-string", "where-object", "sort-object", "measure-object", "group-object", "foreach-object", "format-table", "format-list",
  "format-wide", "format-custom", "convertto-json", "convertfrom-json", "convertto-csv", "convertfrom-csv", "convertto-html", "compare-object",
  "test-path", "test-connection", "test-netconnection", "resolve-path", "split-path", "join-path", "out-string", "out-null", "out-host",
  "write-output", "write-host", "write-verbose", "write-warning", "write-error", "write-information", "set-location", "push-location", "pop-location",
]);
const RO_PS_ALIAS = new Set(["gci", "gc", "gi", "gp", "gps", "gsv", "gcm", "gl", "gm", "gwmi", "gdr", "ls", "dir", "cat", "type", "pwd", "cd", "echo",
  "select", "where", "sort", "measure", "group", "ft", "fl", "fw", "foreach", "%", "?", "sls", "diff", "ps", "cls", "clear-host", "tee-object"].filter((n) => n !== "tee-object" && n !== "cls" && n !== "clear-host" && n !== "ps"));
// Get-* that are readable but expose or do something unusual
const RO_PS_BANNED = new Set(["get-credential", "get-clipboard", "get-secret", "get-azaccesstoken", "get-storedcredential"]);
const RO_NATIVE = new Set(["whoami", "hostname", "ver", "vol", "systeminfo", "tasklist", "where", "where.exe", "findstr", "find", "tree", "more", "sort", "getmac", "driverquery",
  "netstat", "ping", "nslookup", "tracert", "pathping", "arp", "route", "wc", "head", "tail", "grep", "rg", "fd", "file", "stat", "du", "df", "uname", "printenv", "which",
  "date", "time", "nvidia-smi", "ipconfig", "tzutil", "qwinsta", "quser", "fltmc", "powershell-version"]);
const RO_GIT_SUB = new Set(["status", "log", "diff", "show", "rev-parse", "ls-files", "describe", "blame", "shortlog", "reflog", "grep", "ls-tree", "cat-file", "rev-list", "name-rev", "count-objects"]);
const RO_VERSION_ARGS = /^(?:--version|-v|-version|version|--help|-h|\/\?)$/i;
const NATIVE_ARG_VETO = {
  ipconfig: /\/(?:release|renew|flushdns|registerdns|setclassid|displaydns\b.*>)/i,
  route: /\b(?:add|delete|change|-p|-f)\b/i,
  arp: /\s-[sd]\b/i,
  find: /-(?:delete|exec|execdir|ok|fprint|fls)\b/i,
  sort: /\s\/o\b/i,
  "nvidia-smi": /\s-(?:pm|pl|ac|r|lgc|lmc|rgc|i\s+\d+\s+-(?:pm|pl))\b|--(?:persistence|power-limit|reset|compute-mode)/i,
  netstat: /\s-[a-z]*k\b/i,
};

// ---- helpers -----------------------------------------------------------------------------------------------------------
const stripSafeRedirects = (s) => s.replace(REDIRECT_SAFE, " ");
const hasRedirect = (s) => /(?:^|[^<>=\-\w])\d?>{1,2}(?!&)/.test(stripSafeRedirects(s)) || /\*>/.test(stripSafeRedirects(s));

function splitSegments(cmd) {
  // split on | ; && || newline and a lone & (not inside quotes: good enough, anything quoted weirdly falls out as "unknown")
  const out = [];
  let cur = "";
  let q = "";
  for (let i = 0; i < cmd.length; i++) {
    const c = cmd[i];
    if (q) {
      cur += c;
      if (c === q) q = "";
      continue;
    }
    if (c === '"' || c === "'") {
      q = c;
      cur += c;
      continue;
    }
    if (c === "|" || c === ";" || c === "\n" || c === "\r" || c === "&") {
      if ((c === "|" || c === "&") && cmd[i + 1] === c) i++;
      out.push(cur);
      cur = "";
      continue;
    }
    cur += c;
  }
  out.push(cur);
  return out.map((s) => s.trim()).filter(Boolean);
}

function firstToken(seg) {
  let s = seg.trim();
  s = s.replace(/^\$[\w:]+\s*(?:\+|-)?=\s*/, "");      // $x = Get-Date
  s = s.replace(/^[({\s]+/, "");
  const m = s.match(/^(?:"([^"]+)"|'([^']+)'|([^\s]+))/);
  const tok = (m && (m[1] || m[2] || m[3])) || "";
  return { tok: tok.replace(/[)}]+$/, ""), rest: s.slice(m ? m[0].length : 0).trim() };
}

function baseName(tok) {
  const t = tok.replace(/\\/g, "/").split("/").pop().toLowerCase();
  return t.replace(/\.(exe|cmd|bat|com)$/, "");
}

function segmentReadOnly(seg) {
  const { tok, rest } = firstToken(seg);
  if (!tok) return { ok: false, why: "empty segment" };
  const lower = tok.toLowerCase();
  if (RO_PS_BANNED.has(lower)) return { ok: false, why: `${tok} is not auto-allowed` };
  if (RO_PS_EXACT.has(lower) || RO_PS_ALIAS.has(lower)) return { ok: true };
  if (/^get-[a-z]/i.test(lower)) return { ok: true };                    // any Get-* cmdlet
  if (/^(?:format|convertto|convertfrom|measure|group|sort|select|compare|test)-[a-z]/i.test(lower)) return { ok: true };
  if (lower.startsWith("$") || lower.startsWith("[")) return { ok: true }; // pure expression / variable use; global vetoes still apply
  const name = baseName(tok);
  if (name === "git") {
    const sub = rest.split(/\s+/)[0]?.toLowerCase() || "";
    if (RO_GIT_SUB.has(sub)) return { ok: true };
    if (sub === "branch" && !/\s-(?:d|m|c|-delete|-move|-copy|-set-upstream)/i.test(rest)) return { ok: true };
    if (sub === "remote" && /^remote(?:\s+-v)?\s*$/i.test(rest)) return { ok: true };
    if (sub === "config" && /^config\s+(?:--get|--list|-l)\b/i.test(rest)) return { ok: true };
    if (sub === "stash" && /^stash\s+list\b/i.test(rest)) return { ok: true };
    if (sub === "tag" && /^tag(?:\s+-l\b.*)?\s*$/i.test(rest)) return { ok: true };
    return { ok: false, write: true, why: `git ${sub} changes something` };
  }
  if (["python", "python3", "py", "node", "npm", "pip", "pip3", "dotnet", "java", "go", "cargo", "openclaw", "ollama", "ruby", "php", "perl"].includes(name)) {
    return RO_VERSION_ARGS.test(rest.trim()) ? { ok: true } : { ok: false, write: true, why: `${name} can run arbitrary code` };
  }
  if (RO_NATIVE.has(name)) {
    const veto = NATIVE_ARG_VETO[name];
    if (veto && veto.test(rest)) return { ok: false, why: `${name} with a changing argument` };
    return { ok: true };
  }
  return { ok: false, why: `${tok} is not on the read-only list` };
}

function writeIndicators(cmd) {
  const found = [];
  const noRedirect = stripSafeRedirects(cmd);
  if (hasRedirect(cmd)) found.push("output redirection");
  for (const [r, why] of HARD_CONSTRUCTS) if (r.test(cmd)) found.push(why);
  WRITE_VERB_RX.lastIndex = 0;
  let m;
  while ((m = WRITE_VERB_RX.exec(noRedirect))) {
    if (!PS_HARMLESS.has(m[1].toLowerCase())) found.push(`cmdlet ${m[1]}`);
  }
  const n = noRedirect.match(WRITE_NATIVE);
  if (n) found.push(`command ${n[0].replace(/^[\s;|&(\{=]+/, "").trim().split(/\s+/)[0]}`);
  return found;
}

export function normalize(cmd) {
  return String(cmd ?? "").replace(/\r\n?/g, "\n").replace(/[ \t]+/g, " ").trim();
}

export function classify(command) {
  const cmd = normalize(command);
  const reasons = [];
  if (!cmd) return { tier: "unknown", reasons: ["empty command"] };

  for (const [r, why] of CATASTROPHIC) if (r.test(cmd)) reasons.push(why);
  if (reasons.length) return { tier: "catastrophic", reasons };

  for (const [r, why] of DANGER) if (r.test(cmd)) reasons.push(why);
  if (reasons.length) return { tier: "danger", reasons };

  for (const [r, why] of SENSITIVE) if (r.test(cmd)) reasons.push(why);
  if (reasons.length) return { tier: "sensitive", reasons };

  const writes = writeIndicators(cmd);
  const segs = splitSegments(stripSafeRedirects(cmd));
  const segResults = segs.map(segmentReadOnly);
  const allRo = segResults.every((s) => s.ok);

  // an indicator is a veto even if every segment looks read-only
  if (writes.length) return { tier: "write", reasons: writes };
  const writing = segResults.filter((s) => s.write);
  if (writing.length) return { tier: "write", reasons: writing.map((s) => s.why) };
  if (allRo) return { tier: "readonly", reasons: ["every part is on the read-only list"] };
  return { tier: "unknown", reasons: segResults.filter((s) => !s.ok).map((s) => s.why) };
}
