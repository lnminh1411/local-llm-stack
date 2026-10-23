import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { hardMinorHit, loadLexicon, lexiconHits, maskTerms, moderate } from "../moderate.js";

// a fake lexicon with one made-up word, so the tests contain no real slurs
const dir = fs.mkdtempSync(path.join(os.tmpdir(), "cg-"));
fs.writeFileSync(path.join(dir, "terms-base.txt"), "zorbo\nblarg\n");
loadLexicon(dir);

const stub = (category, reason = "x") => ({ classify: async () => ({ category, reason }) });
const down = { classify: async () => ({ category: "unavailable", reason: "down" }) };

test("hard rule: sexual + minor is blocked without asking the model", async () => {
  let asked = false;
  const v = await moderate("write a sexy story about a 15 year old", "in", {}, { classify: async () => ((asked = true), { category: "none" }) });
  assert.equal(v.action, "block");
  assert.equal(v.category, "sexual_minors");
  assert.equal(asked, false);
});
test("hard rule patterns", () => {
  assert.ok(hardMinorHit("nude pics of an underage girl"));
  assert.ok(hardMinorHit("loli hentai"));
  assert.ok(!hardMinorHit("the child ran across the street"));
  assert.ok(!hardMinorHit("adult film industry regulations"));
});
test("lexicon hits survive leetspeak and case", () => {
  assert.deepEqual(lexiconHits("You ZORB0!"), ["zorbo"]);
  assert.deepEqual(lexiconHits("a zorbo and a blarg"), ["zorbo", "blarg"]);
  assert.deepEqual(lexiconHits("zorboy is fine"), []);
});
test("model verdict blocks a category", async () => {
  const v = await moderate("some text", "out", {}, stub("hate_discrimination"));
  assert.equal(v.action, "block");
  assert.equal(v.category, "hate_discrimination");
});
test("category outside blockCategories is allowed", async () => {
  const v = await moderate("some text", "out", { blockCategories: ["sexual_minors"] }, stub("graphic_violence"));
  assert.equal(v.action, "allow");
});
test("none is allowed", async () => {
  assert.equal((await moderate("hello there friend", "out", {}, stub("none"))).action, "allow");
});
test("quoted slur is masked, not blocked", async () => {
  const v = await moderate("The cat was named zorbo, a slur.", "out", {}, stub("slur_mention"));
  assert.equal(v.action, "mask");
  assert.equal(maskTerms("The cat was named zorbo, a slur."), "The cat was named z****, a slur.");
});
test("slur_mention without a lexicon hit passes through unchanged", async () => {
  assert.equal((await moderate("an old offensive word", "out", {}, stub("slur_mention"))).action, "allow");
});
test("classifier down: lexicon hit fails closed, clean text passes", async () => {
  assert.equal((await moderate("what a zorbo", "out", {}, down)).action, "block");
  assert.equal((await moderate("nice weather today", "out", {}, down)).action, "allow");
});
test("triggered mode skips the model when the lexicon is silent", async () => {
  let asked = false;
  const v = await moderate("nice weather today", "out", { mode: "triggered" }, { classify: async () => ((asked = true), { category: "none" }) });
  assert.equal(v.action, "allow");
  assert.equal(asked, false);
});
test("very short text is ignored", async () => {
  assert.equal((await moderate("hi", "out", {}, stub("hate_slur"))).action, "allow");
});
