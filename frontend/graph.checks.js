// graph.checks.js — regression fixtures for graph.js, built from real
// confirmed-against-live-data cases (see the old index.html's own
// CHANGELOG/comments for the original bug reports these trace back to).
// Named *.checks.js, not test*, because .gitignore has a bare "test*"
// pattern that would silently swallow anything starting with "test".
//
// Run in a browser at /frontend/graph.checks.html (fetches real branch
// data from the same-origin API - no mock fixtures, these assert against
// the actual live database) or with node (see runNode() below) as long as
// a fetch-capable runtime hits the same API base.

import { indexBranch, nextRealSteps, passiveCheckIsExclusive, resolveOptions, resolveChain } from "./graph.js";
import { resolvePrevReal } from "./back.js";

// v0 of the rewrite briefly had a real bug here: resolveOptions from 5
// jumped straight to [292, 307], silently never surfacing 291 as its own
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

  // --- 1370/291 vs 292: the motivating exclusivity-rule case ---------
  // 291 (Volition 10) -> 293 (junction) -> {294 -> 292 (Empathy 2) -> 296
  // -> {297,298}} vs {295 -> 307}. 292's downstream is only reachable
  // through 292 - not accessible via the bypass sibling (295->307) - so
  // 292 must be exclusive (eligible option). 291 itself has no sibling at
  // its own fork point (single link forward from its predecessor) so it
  // must never be exclusive, regardless of what's downstream of it.
  const idx1370 = await fetchIndex(apiBase, 1370);

  await check("292 is exclusive (gates 296/297/298, bypass sibling 295->307 never reaches them)", () => {
    const forkSteps = nextRealSteps(idx1370, 293); // the junction both 294 and 295 hang off
    const checkStep = forkSteps.find(s => s.dentryId === 292 || (idx1370.entriesById[s.dentryId] && false));
    // 294 is itself a system node collapsed away by nextRealSteps, so the
    // step landing on 292 is what we expect here directly.
    const step292 = forkSteps.find(s => s.type === "line" && s.dentryId === 292);
    assert(step292, `expected a step landing on 292, got: ${JSON.stringify(forkSteps)}`);
    const siblings = forkSteps.filter(s => s !== step292);
    assert(passiveCheckIsExclusive(idx1370, step292, siblings), "292 should be exclusive");
  });

  await check("291 has no siblings at its own fork point (resolveOptions never even calls passiveCheckIsExclusive here - single-hop short-circuit)", () => {
    // 291's own predecessor links straight to it with nothing else -
    // confirm via the real link table rather than assuming. Direct calls
    // to passiveCheckIsExclusive([]) now correctly return true (no true
    // bypass sibling = exclusive by definition, per the 569 fix below) -
    // but that path is never actually exercised for a real single-hop
    // step in practice, since resolveOptions short-circuits on
    // steps.length === 1 before ever calling passiveCheckIsExclusive at
    // all (see resolveOptions' own docs) - this check only confirms the
    // no-sibling precondition holds for 291, not a behavior of the
    // resolver itself (see the "does NOT skip past 291" check for that).
    const preSteps = nextRealSteps(idx1370, 5); // 5 -> 291 directly, per dlinks
    const step291 = preSteps.find(s => s.type === "line" && s.dentryId === 291);
    assert(step291, `expected a step landing on 291 from 5, got: ${JSON.stringify(preSteps)}`);
    const siblings = preSteps.filter(s => s !== step291);
    assert(siblings.length === 0, `expected no siblings for 291, got: ${JSON.stringify(siblings)}`);
  });

  await check("resolveOptions(5) does NOT skip past 291 - it's the sole single-hop step, not a fork to flatten", () => {
    const options = resolveOptions(idx1370, 5, new Set([5]));
    assert(options.length === 1, `expected exactly one step from 5, got: ${JSON.stringify(options)}`);
    assert(options[0].type === "line" && options[0].dentryId === 291, `expected step to land on 291 itself, got: ${JSON.stringify(options[0])}`);
    assert((options[0].via || []).length === 0, `291 should have no via - it's the real next step, not flattened-through content`);
  });

  await check("resolveOptions(293) surfaces 292 as a real option, flattens 295's junction straight to 307", () => {
    const options = resolveOptions(idx1370, 293, new Set([293]));
    const dentryIds = options.filter(o => o.type === "line").map(o => o.dentryId).sort((a, b) => a - b);
    assert(
      dentryIds.length === 2 && dentryIds[0] === 292 && dentryIds[1] === 307,
      `expected exactly [292, 307], got: ${JSON.stringify(dentryIds)} (full: ${JSON.stringify(options)})`
    );
  });

  // --- 569/286: two parallel duplicate passive checks, no true bypass -
  // real bug arda caught live (v2.1.0): both 289 and 6 were silently
  // flattened away (each treated the OTHER as a "bypass"), leaking their
  // shared downstream (23/26/290) up as if it belonged to neither check.
  const idx569 = await fetchIndex(apiBase, 569);
  await check("569: both duplicate passive checks (289, 6) are exclusive - no true bypass exists", () => {
    const forkSteps = nextRealSteps(idx569, 286);
    const ids = forkSteps.filter(s => s.type === "line").map(s => s.dentryId).sort((a, b) => a - b);
    assert(JSON.stringify(ids) === JSON.stringify([6, 289]), `expected fork [6, 289], got: ${JSON.stringify(ids)}`);
    for (const step of forkSteps) {
      const siblings = forkSteps.filter(s => s !== step);
      assert(passiveCheckIsExclusive(idx569, step, siblings), `expected dentry ${step.dentryId} to be exclusive (no true bypass sibling)`);
    }
  });
  await check("resolveOptions(286) surfaces BOTH 289 and 6 as their own options, not their shared downstream", () => {
    const options = resolveOptions(idx569, 286, new Set([286]));
    const ids = options.filter(o => o.type === "line").map(o => o.dentryId).sort((a, b) => a - b);
    assert(JSON.stringify(ids) === JSON.stringify([6, 289]), `expected [6, 289], got: ${JSON.stringify(ids)} (full: ${JSON.stringify(options)})`);
  });

  await check("resolveChain(5) collects [5, 291] then stops on the real 292/307 fork", () => {
    const chain = resolveChain(idx1370, 5);
    const ids = chain.collected.map(c => c.dentryId);
    assert(JSON.stringify(ids) === JSON.stringify([5, 291]), `expected [5, 291], got: ${JSON.stringify(ids)}`);
    const dentryIds = chain.options.filter(o => o.type === "line").map(o => o.dentryId).sort((a, b) => a - b);
    assert(JSON.stringify(dentryIds) === JSON.stringify([292, 307]), `expected fork [292, 307], got: ${JSON.stringify(dentryIds)}`);
  });

  await check("resolveChain is stateless — choosing 292 next doesn't carry over 5/291 from the previous chain", () => {
    // Simulates exactly what the demo page's per-click bug got wrong:
    // each choice must start a genuinely fresh chain from its own anchor,
    // never an accumulating transcript of everything walked before it.
    const first = resolveChain(idx1370, 5);
    const chosen = first.options.find(o => o.type === "line" && o.dentryId === 292);
    assert(chosen, "expected 292 among the first chain's options");
    const second = resolveChain(idx1370, chosen.dentryId);
    const secondIds = second.collected.map(c => c.dentryId);
    assert(!secondIds.includes(5) && !secondIds.includes(291),
      `second chain must not carry over the first chain's lines, got: ${JSON.stringify(secondIds)}`);
    assert(secondIds[0] === 292, `second chain should start at 292 itself, got: ${JSON.stringify(secondIds)}`);
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

  await check("resolvePrevReal(291) is a direct real predecessor, no system hop", async () => {
    const { fetchPredecessors, fetchLineInfo } = makeFetchers(apiBase);
    const prev = await resolvePrevReal(fetchPredecessors, fetchLineInfo, 1370, 291);
    assert(prev && prev.branchId === 1370 && prev.dentryId === 5, `expected {1370,5}, got: ${JSON.stringify(prev)}`);
  });

  await check("resolvePrevReal(292) walks back through two system hops (294, 293) to real line 291", async () => {
    const { fetchPredecessors, fetchLineInfo } = makeFetchers(apiBase);
    const prev = await resolvePrevReal(fetchPredecessors, fetchLineInfo, 1370, 292);
    assert(prev && prev.branchId === 1370 && prev.dentryId === 291, `expected {1370,291}, got: ${JSON.stringify(prev)}`);
  });

  await check("resolvePrevReal cycle guard: 1199/236 doesn't hang walking backward either", async () => {
    const { fetchPredecessors, fetchLineInfo } = makeFetchers(apiBase);
    const prev = await resolvePrevReal(fetchPredecessors, fetchLineInfo, 1199, 236);
    assert(prev === null || (typeof prev.branchId === "number" && typeof prev.dentryId === "number"),
      `expected null or a real {branchId,dentryId}, got: ${JSON.stringify(prev)}`);
  });

  // --- 1199/236: genuine graph cycle ---------------------------------
  await check("1199/236 cycle guard: nextRealSteps terminates and doesn't loop forever", async () => {
    const idx1199 = await fetchIndex(apiBase, 1199);
    const steps = nextRealSteps(idx1199, 236);
    assert(Array.isArray(steps), "nextRealSteps must return an array even when the graph cycles back through 236");
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
