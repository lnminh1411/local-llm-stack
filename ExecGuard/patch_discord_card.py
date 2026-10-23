"""Gives the Discord approval card a custom look for exec-guard requests (pluginId "exec-guard"), and updates one card in place.

What it changes in @openclaw/discord (approval-handler.runtime-*.js), only for exec-guard requests:
  - pending card: coloured by severity, one bold reason line, the command in a code block, readable button labels
    ("Allow once" / "Allow always (save this command)" / "Deny"), no "Tool / Agent / Plugin" bullets.
  - catastrophic commands: after confirmation 1/2 is allowed, that same card is edited into the FINAL 2/2 card
    (no second message). When the last decision is made the card is cleared (deleted) when
    channels.discord.execApprovals.cleanupAfterResolve is true.
  - other OpenClaw approvals are untouched.

The file lives inside an npm-installed package, so an OpenClaw / Discord plugin update replaces it and the patch is gone:
Start-LocalAI.ps1 re-runs this script before the gateway starts. Idempotent. A copy of the original is kept next to the file.

usage: python patch_discord_card.py [--revert | --check]
"""
import glob
import os
import shutil
import sys

MARK = "/* exec-guard card patch v1 */"
HOME = os.path.expanduser("~")
PATTERN = os.path.join(HOME, ".openclaw", "npm", "projects", "openclaw-discord-*", "node_modules", "@openclaw", "discord", "dist",
                       "approval-handler.runtime-*.js")

HELPERS = MARK + r'''
const guardSlots = new Map();
function guardParse(view) {
	const title = String(view.title ?? "");
	const parts = String(view.description ?? "").split("\n");
	const stage = /(\d)\/2/.exec(title);
	return { title, reason: parts[0] ?? "", command: parts.slice(1).join("\n"), stage: stage ? stage[1] : null };
}
function guardCommandBlock(command) {
	return "```powershell\n" + formatCommandPreview(command || "(not shown)", 900) + "\n```";
}
function guardRequestContainer(params) {
	const view = params.view;
	const g = guardParse(view);
	const expiresAtSeconds = Math.max(0, Math.floor(view.expiresAtMs / 1e3));
	const components = [new TextDisplay(`## ${g.title}`)];
	if (g.reason) components.push(new TextDisplay(`**${g.reason}**`));
	components.push(new Separator({ divider: true, spacing: "small" }));
	components.push(new TextDisplay(guardCommandBlock(g.command)));
	if (params.actionRow) components.push(params.actionRow);
	components.push(new TextDisplay(`-# Expires <t:${expiresAtSeconds}:R> · ${view.approvalId}`));
	return new DiscordUiContainer({ cfg: params.cfg, accountId: params.accountId, components, accentColor: view.severity === "critical" ? "#ED4245" : "#FAA61A" });
}
function guardResolvedContainer(params) {
	const view = params.view;
	const g = guardParse(view);
	const d = view.decision;
	const stage1Allowed = g.stage === "1" && d !== "deny";
	const label = d === "deny" ? "Denied" : d === "allow-always" ? "Allowed and saved" : "Allowed once";
	const components = stage1Allowed
		? [new TextDisplay(`## ${g.title.replace(/confirmation 1\/2/i, "confirmation 1/2 granted")}`), new TextDisplay("Waiting for the final confirmation…"), new TextDisplay(guardCommandBlock(g.command))]
		: [new TextDisplay(`## ${label}`), new TextDisplay(`-# ${g.reason}`), new TextDisplay(guardCommandBlock(g.command))];
	const c = new DiscordUiContainer({ cfg: params.cfg, accountId: params.accountId, components, accentColor: stage1Allowed ? "#FAA61A" : d === "deny" ? "#ED4245" : "#57F287" });
	if (stage1Allowed) c.__guardKeep = true;
	return c;
}
function guardExpiredContainer(params) {
	const g = guardParse(params.view);
	return new DiscordUiContainer({ cfg: params.cfg, accountId: params.accountId, accentColor: "#99AAB5",
		components: [new TextDisplay("## Expired"), new TextDisplay("-# Nobody answered in time, the command did not run."), new TextDisplay(guardCommandBlock(g.command))] });
}
function guardActions(view) {
	if (view.pluginId !== "exec-guard") return view.actions;
	const labels = { "allow-once": "Allow once", "allow-always": "Allow always (save this command)", deny: "Deny" };
	return view.actions.map((a) => ({ ...a, label: labels[a.decision] ?? a.label }));
}
'''

EDITS = [
    # helpers go right before the first plugin container builder
    ("function createPluginApprovalRequestContainer(params) {\n",
     HELPERS + "function createPluginApprovalRequestContainer(params) {\n\tif (params.view.pluginId === \"exec-guard\") return guardRequestContainer(params);\n"),
    ("function createPluginResolvedContainer(params) {\n",
     "function createPluginResolvedContainer(params) {\n\tif (params.view.pluginId === \"exec-guard\") return guardResolvedContainer(params);\n"),
    ("function createPluginExpiredContainer(params) {\n",
     "function createPluginExpiredContainer(params) {\n\tif (params.view.pluginId === \"exec-guard\") return guardExpiredContainer(params);\n"),
    # readable button labels
    ("\t\tactions: view.actions\n\t});\n}", "\t\tactions: guardActions(view)\n\t});\n}"),
    # keep the card after confirmation 1/2 and remember it for the final confirmation
    ("\t\t\tconst container = payload;\n\t\t\tawait finalizeMessage({",
     "\t\t\tconst container = payload;\n\t\t\tif (container && container.__guardKeep) guardSlots.set(entry.discordChannelId, { id: entry.discordMessageId, ts: Date.now() });\n\t\t\tawait finalizeMessage({"),
    ("cleanupAfterResolve: phase === \"resolved\" ? resolved.context.config.cleanupAfterResolve : false,",
     "cleanupAfterResolve: phase === \"resolved\" && !(container && container.__guardKeep) ? resolved.context.config.cleanupAfterResolve : false,"),
    # the final confirmation edits that card instead of sending a new message
    ("\t\t\tconst message = await discordRequest(() => createChannelMessage(rest, preparedTarget.discordChannelId, { body: pendingPayload.body }),",
     "\t\t\tconst guardSlot = /FINAL CONFIRMATION/.test(JSON.stringify(pendingPayload.body)) ? guardSlots.get(preparedTarget.discordChannelId) : null;\n"
     "\t\t\tif (guardSlot && Date.now() - guardSlot.ts < 6e5) {\n"
     "\t\t\t\tguardSlots.delete(preparedTarget.discordChannelId);\n"
     "\t\t\t\ttry {\n"
     "\t\t\t\t\tawait discordRequest(() => editChannelMessage(rest, preparedTarget.discordChannelId, guardSlot.id, { body: pendingPayload.body }), \"update-approval\");\n"
     "\t\t\t\t\treturn { discordMessageId: guardSlot.id, discordChannelId: preparedTarget.discordChannelId };\n"
     "\t\t\t\t} catch (err) {\n"
     "\t\t\t\t\tlogError(`discord approvals: failed to reuse card for final confirmation: ${String(err)}`);\n"
     "\t\t\t\t}\n"
     "\t\t\t}\n"
     "\t\t\tconst message = await discordRequest(() => createChannelMessage(rest, preparedTarget.discordChannelId, { body: pendingPayload.body }),"),
]


def target():
    files = glob.glob(PATTERN)
    if len(files) != 1:
        sys.exit(f"expected exactly one approval-handler.runtime file, found {files}")
    return files[0]


def main():
    f = target()
    orig = f + ".execguard-orig"
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    text = open(f, encoding="utf-8").read()
    patched = MARK in text
    if mode == "--check":
        print("patched" if patched else "NOT patched")
        return 0 if patched else 1
    if mode == "--revert":
        if os.path.exists(orig):
            shutil.copyfile(orig, f)
            print("reverted")
        else:
            print("no saved original")
        return 0
    if patched:
        print("already patched")
        return 0
    for old, _ in EDITS:
        if text.count(old) != 1:
            sys.exit(f"anchor not found exactly once (the Discord plugin changed?): {old[:70]!r}")
    shutil.copyfile(f, orig)
    for old, new in EDITS:
        text = text.replace(old, new, 1)
    open(f, "w", encoding="utf-8", newline="").write(text)
    print("patched", os.path.basename(f))
    return 0


if __name__ == "__main__":
    sys.exit(main())
