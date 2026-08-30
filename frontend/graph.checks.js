// graph.checks.js — regression fixtures for graph.js, built from real
// confirmed-against-live-data cases (see the old index.html's own
// CHANGELOG/comments for the original bug reports these trace back to).
// Named *.checks.js, not test*, because .gitignore has a bare "test*"
// pattern that would silently swallow anything starting with "test".
//
// Branch/dentry numbers below were re-derived after the Dec 2021 database
// migration renumbered every id from scratch - matched by content
// (title/dialoguetext/actor name/condition), not carried over from the old
// ids. See the old numbers in each comment for traceability.
//
// Run in a browser at /frontend/graph.checks.html (fetches real branch
// data from the same-origin API - no mock fixtures, these assert against
// the actual live database) or with node (see runNode() below) as long as
// a fetch-capable runtime hits the same API base.

import { indexBranch, nextRealSteps, passiveCheckIsExclusive, resolveOptions, resolveChain } from "./graph.js";
import { resolvePrevReal } from "./back.js";

// v2.3.1: the "effect: <code>...</code>" line inside a search/effects
// result rendered completely unstyled - #explorer .effect / #line-detail
// .effect was the only CSS scope for that class, and runSearchEffects
// renders into #search-results, matching neither. Confirmed pre-existing
// back to v1.2.0/v2.1.0, not caused by any later change - fixed by adding
// #search-results to the selector. Checked here as a plain text search
// over the served index.html (not a DOM assertion - this file has no DOM
// access in either its browser or node run mode) so a future edit that
// silently narrows the selector back down gets caught.
async function checkEffectCssScoping(apiBase) {
  const base = apiBase.replace(/\/+$/, "");
  const res = await fetch(`${base}/`);
  if (!res.ok) throw new Error(`GET / : ${res.status}`);
  const html = await res.text();
  const m = html.match(/#explorer \.effect,[^{]*\{/);
  assert(m, "expected to find the #explorer .effect CSS rule at all");
  assert(m[0].includes("#search-results"), `#search-results .effect is missing from the rule - the effects search page will render unstyled again. Rule found: ${m[0]}`);
}

// v0 of the rewrite briefly had a real bug here: resolveOptions from 181
// jumped straight to [217, 399], silently never surfacing 12 as its own
// step at all - because a lone passive check with no sibling is trivially
// "not exclusive", and the old code treated "not exclusive" as "flatten
// away" even with nothing to flatten past. Caught by hand in the demo
// page, not by any check - this fixture exists so it can't recur silently.

const results = [];
async function check(name, fn) {
  try {
    await fn();
    results.push({ name, ok: true });
  } catch (e) {
    results.push({ name, ok: false, error: String(e && e.stack || e) });
  }
}
function assert(cond, msg) {
  if (!cond) throw new Error(msg || "assertion failed");
}

async function fetchIndex(apiBase, branchId) {
  const base = apiBase.replace(/\/+$/, "");
  const res = await fetch(`${base}/branches/${branchId}`);
  if (!res.ok) throw new Error(`branches/${branchId}: ${res.status}`);
  return indexBranch(branchId, await res.json());
}

export async function runChecks(apiBase) {
  results.length = 0;

  // --- 537/12 vs 217: the motivating exclusivity-rule case (was 1370/291 vs 292)
  // 12 (Volition 10) -> 218 (junction) -> {219 -> 217 (Empathy 2) -> 263
  // -> {264,265}} vs {220 -> 399}. 217's downstream is only reachable
  // through 217 - not accessible via the bypass sibling (220->399) - so
  // 217 must be exclusive (eligible option). 12 itself has no sibling at
  // its own fork point (single link forward from its predecessor) so it
  // must never be exclusive, regardless of what's downstream of it.
  const idx537 = await fetchIndex(apiBase, 537);

  await check("217 is exclusive (gates 263/264/265, bypass sibling 220->399 never reaches them)", () => {
    const forkSteps = nextRealSteps(idx537, 218); // the junction both 219 and 220 hang off
    const checkStep = forkSteps.find(s => s.dentryId === 217 || (idx537.entriesById[s.dentryId] && false));
    // 219 is itself a system node collapsed away by nextRealSteps, so the
    // step landing on 217 is what we expect here directly.
    const step217 = forkSteps.find(s => s.type === "line" && s.dentryId === 217);
    assert(step217, `expected a step landing on 217, got: ${JSON.stringify(forkSteps)}`);
    const siblings = forkSteps.filter(s => s !== step217);
    assert(passiveCheckIsExclusive(idx537, step217, siblings), "217 should be exclusive");
  });

  await check("12 has no siblings at its own fork point (resolveOptions never even calls passiveCheckIsExclusive here - single-hop short-circuit)", () => {
    // 12's own predecessor links straight to it with nothing else -
    // confirm via the real link table rather than assuming. Direct calls
    // to passiveCheckIsExclusive([]) now correctly return true (no true
    // bypass sibling = exclusive by definition, per the 1012 fix below) -
    // but that path is never actually exercised for a real single-hop
    // step in practice, since resolveOptions short-circuits on
    // steps.length === 1 before ever calling passiveCheckIsExclusive at
    // all (see resolveOptions' own docs) - this check only confirms the
    // no-sibling precondition holds for 12, not a behavior of the
    // resolver itself (see the "does NOT skip past 12" check for that).
    const preSteps = nextRealSteps(idx537, 181); // 181 -> 12 directly, per dlinks
    const step12 = preSteps.find(s => s.type === "line" && s.dentryId === 12);
    assert(step12, `expected a step landing on 12 from 181, got: ${JSON.stringify(preSteps)}`);
    const siblings = preSteps.filter(s => s !== step12);
    assert(siblings.length === 0, `expected no siblings for 12, got: ${JSON.stringify(siblings)}`);
  });

  await check("resolveOptions(181) does NOT skip past 12 - it's the sole single-hop step, not a fork to flatten", () => {
    const options = resolveOptions(idx537, 181, new Set([181]));
    assert(options.length === 1, `expected exactly one step from 181, got: ${JSON.stringify(options)}`);
    assert(options[0].type === "line" && options[0].dentryId === 12, `expected step to land on 12 itself, got: ${JSON.stringify(options[0])}`);
    assert((options[0].via || []).length === 0, `12 should have no via - it's the real next step, not flattened-through content`);
  });

  await check("resolveOptions(218) surfaces 217 as a real option, flattens 220's junction straight to 399", () => {
    const options = resolveOptions(idx537, 218, new Set([218]));
    const dentryIds = options.filter(o => o.type === "line").map(o => o.dentryId).sort((a, b) => a - b);
    assert(
      dentryIds.length === 2 && dentryIds[0] === 217 && dentryIds[1] === 399,
      `expected exactly [217, 399], got: ${JSON.stringify(dentryIds)} (full: ${JSON.stringify(options)})`
    );
  });

  // --- 217's own outcome-contradiction promotion: its effect sets
  // plaza.tribunal_halflight_escalate=true, and 265 (downstream of 217)
  // gates on that exact variable being false, landing on 425
  // ("...*PIGFUCK!*..."). Same real pattern as 1012 below, found in the
  // SAME branch this whole exclusivity rule was originally built around -
  // confirmed via the real link/condition table, not assumed. Promotion
  // is scoped to exactly 217's own downstream (once you've committed to
  // viewing 217 itself) - NOT pulled up to 218's/12's own level, where
  // 217 is merely a sibling of the unrelated 399 and nothing about 217's
  // own effect is settled yet.
  await check("resolveOptions(217) promotes 425 alongside its own normal continuation", () => {
    const options = resolveOptions(idx537, 217, new Set([217]));
    const dentryIds = options.filter(o => o.type === "line").map(o => o.dentryId).sort((a, b) => a - b);
    assert(dentryIds.includes(425), `expected 425 present, got: ${JSON.stringify(dentryIds)}`);
  });
  await check("resolveOptions(12)/resolveOptions(218) do NOT show 425 - 217's own effect isn't settled until you're on 217 itself", () => {
    const from12 = resolveOptions(idx537, 12, new Set([12])).filter(o => o.type === "line").map(o => o.dentryId);
    const from218 = resolveOptions(idx537, 218, new Set([218])).filter(o => o.type === "line").map(o => o.dentryId);
    assert(!from12.includes(425), `12 should not show 425 yet, got: ${JSON.stringify(from12)}`);
    assert(!from218.includes(425), `218 should not show 425 yet, got: ${JSON.stringify(from218)}`);
  });

  // --- 1012/229: two parallel duplicate passive checks, no true bypass -
  // (was 569/286) real bug caught live (v2.1.0): both 141 and 77
  // were silently flattened away (each treated the OTHER as a "bypass"),
  // leaking their shared downstream (233/177/303) up as if it belonged to
  // neither check.
  const idx1012 = await fetchIndex(apiBase, 1012);
  await check("1012: both duplicate passive checks (77, 141) are exclusive - no true bypass exists", () => {
    const forkSteps = nextRealSteps(idx1012, 229);
    const ids = forkSteps.filter(s => s.type === "line").map(s => s.dentryId).sort((a, b) => a - b);
    assert(JSON.stringify(ids) === JSON.stringify([77, 141]), `expected fork [77, 141], got: ${JSON.stringify(ids)}`);
    for (const step of forkSteps) {
      const siblings = forkSteps.filter(s => s !== step);
      assert(passiveCheckIsExclusive(idx1012, step, siblings), `expected dentry ${step.dentryId} to be exclusive (no true bypass sibling)`);
    }
  });
  await check("resolveOptions(229) surfaces BOTH 77 and 141 as their own options, not their shared downstream", () => {
    const options = resolveOptions(idx1012, 229, new Set([229]));
    const ids = options.filter(o => o.type === "line").map(o => o.dentryId).sort((a, b) => a - b);
    assert(JSON.stringify(ids) === JSON.stringify([77, 141]), `expected [77, 141], got: ${JSON.stringify(ids)} (full: ${JSON.stringify(options)})`);
  });

  // --- 1012's third check (303, Interfacing): the full diagnosis - (was 569's 290)
  // 141/77's own SetVariableValue sets the exact variable 142's downstream
  // fork (143/144) branches on, so 303 (144's branch) is the real "neither
  // Encyclopedia check fired" outcome, not unrelated duplicate content.
  // Scoped correctly: only appears once you've committed to 141 or 77
  // (their own effect is what settles the variable), never at 229's own
  // level (where neither has fired yet). Also confirms the specific
  // wrongly-leaked options (only reachable via 303's own downstream) are
  // gone from 141/77's own level now.
  await check("resolveOptions(141) and resolveOptions(77) both promote 303, with no leaked downstream", () => {
    for (const start of [141, 77]) {
      const options = resolveOptions(idx1012, start, new Set([start]));
      const ids = options.filter(o => o.type === "line").map(o => o.dentryId).sort((a, b) => a - b);
      assert(JSON.stringify(ids) === JSON.stringify([177, 233, 303]),
        `expected [177, 233, 303] from ${start}, got: ${JSON.stringify(ids)}`);
    }
  });

  await check("resolveChain(181) collects [181, 12] then stops on the real 217/399 fork", () => {
    const chain = resolveChain(idx537, 181);
    const ids = chain.collected.map(c => c.dentryId);
    assert(JSON.stringify(ids) === JSON.stringify([181, 12]), `expected [181, 12], got: ${JSON.stringify(ids)}`);
    const dentryIds = chain.options.filter(o => o.type === "line").map(o => o.dentryId).sort((a, b) => a - b);
    assert(JSON.stringify(dentryIds) === JSON.stringify([217, 399]), `expected fork [217, 399], got: ${JSON.stringify(dentryIds)}`);
  });

  await check("resolveChain is stateless — choosing 217 next doesn't carry over 181/12 from the previous chain", () => {
    // Simulates exactly what the demo page's per-click bug got wrong:
    // each choice must start a genuinely fresh chain from its own anchor,
    // never an accumulating transcript of everything walked before it.
    const first = resolveChain(idx537, 181);
    const chosen = first.options.find(o => o.type === "line" && o.dentryId === 217);
    assert(chosen, "expected 217 among the first chain's options");
    const second = resolveChain(idx537, chosen.dentryId);
    const secondIds = second.collected.map(c => c.dentryId);
    assert(!secondIds.includes(181) && !secondIds.includes(12),
      `second chain must not carry over the first chain's lines, got: ${JSON.stringify(secondIds)}`);
    assert(secondIds[0] === 217, `second chain should start at 217 itself, got: ${JSON.stringify(secondIds)}`);
  });

  // --- graph-native "back" (back.js) ---------------------------------
  function makeFetchers(base) {
    const b = base.replace(/\/+$/, "");
    return {
      fetchPredecessors: async (branchId, dentryId) => {
        const res = await fetch(`${b}/branches/${branchId}/lines/${dentryId}/predecessors`);
        return res.ok ? res.json() : [];
      },
      fetchLineInfo: async (branchId, dentryId) => {
        const res = await fetch(`${b}/branches/${branchId}/lines/${dentryId}`);
        return res.ok ? res.json() : null;
      },
    };
  }

  await check("resolvePrevReal(12) is a direct real predecessor, no system hop", async () => {
    const { fetchPredecessors, fetchLineInfo } = makeFetchers(apiBase);
    const prev = await resolvePrevReal(fetchPredecessors, fetchLineInfo, 537, 12);
    assert(prev && prev.branchId === 537 && prev.dentryId === 181, `expected {537,181}, got: ${JSON.stringify(prev)}`);
  });

  await check("resolvePrevReal(217) walks back through two system hops (219, 218) to real line 12", async () => {
    const { fetchPredecessors, fetchLineInfo } = makeFetchers(apiBase);
    const prev = await resolvePrevReal(fetchPredecessors, fetchLineInfo, 537, 217);
    assert(prev && prev.branchId === 537 && prev.dentryId === 12, `expected {537,12}, got: ${JSON.stringify(prev)}`);
  });

  await check("resolvePrevReal cycle guard: 556/407 doesn't hang walking backward either", async () => {
    const { fetchPredecessors, fetchLineInfo } = makeFetchers(apiBase);
    const prev = await resolvePrevReal(fetchPredecessors, fetchLineInfo, 556, 407);
    assert(prev === null || (typeof prev.branchId === "number" && typeof prev.dentryId === "number"),
      `expected null or a real {branchId,dentryId}, got: ${JSON.stringify(prev)}`);
  });

  // --- 556/407: genuine graph cycle (was 1199/236) --------------------
  await check("556/407 cycle guard: nextRealSteps terminates and doesn't loop forever", async () => {
    const idx556 = await fetchIndex(apiBase, 556);
    const steps = nextRealSteps(idx556, 407);
    assert(Array.isArray(steps), "nextRealSteps must return an array even when the graph cycles back through 407");
  });

  // --- 9/50: check success/failure outcome tagging --------------------
  // v2.3.1: graph.js's rewrite of the walk logic (v2.1.0) never ported
  // the pre-rewrite app's outcome tagging - every option's `.outcome` was
  // always undefined, so the success/failure marker (border color + the
  // "✓ success"/"✗ failure" tag) never rendered anywhere, on any check,
  // even though the rendering code for it was still fully in place. 9/50
  // is a real white Savoir Faire check ("Grab the tie.") confirmed
  // against live data: its one success branch is 82, its (several)
  // failure branches include 38/42/33/19.
  await check("resolveChain(9/50) tags the check's own branches success/failure", async () => {
    const idx9 = await fetchIndex(apiBase, 9);
    const chain = resolveChain(idx9, 50);
    const byId = Object.fromEntries(chain.options.filter(o => o.type === "line").map(o => [o.dentryId, o.outcome]));
    assert(byId[82] === "success", `expected 82 tagged success, got: ${JSON.stringify(byId)}`);
    assert(byId[38] === "failure", `expected 38 tagged failure, got: ${JSON.stringify(byId)}`);
    assert(Object.values(byId).some(o => o === "success") && Object.values(byId).some(o => o === "failure"),
      `expected at least one success and one failure branch, got: ${JSON.stringify(byId)}`);
  });

  // --- CSS: #search-results .effect scoping ----------------------------
  await check("index.html's .effect CSS rule covers #search-results, not just #explorer/#line-detail", async () => {
    await checkEffectCssScoping(apiBase);
  });

  return results;
}

// Minimal node runner, for CI/manual use outside a browser - Node 18+ has
// global fetch built in, so this needs nothing beyond that.
export async function runNode(apiBase) {
  const results = await runChecks(apiBase);
  let failed = 0;
  for (const r of results) {
    console.log(`${r.ok ? "PASS" : "FAIL"} - ${r.name}`);
    if (!r.ok) { failed++; console.log(`  ${r.error}`); }
  }
  console.log(`\n${results.length - failed}/${results.length} passed`);
  if (failed) process.exitCode = 1;
}

if (typeof process !== "undefined" && process.argv && process.argv[1] && process.argv[1].endsWith("graph.checks.js")) {
  runNode(process.argv[2] || "http://127.0.0.1:2015").catch(e => { console.error(e); process.exitCode = 1; });
}
