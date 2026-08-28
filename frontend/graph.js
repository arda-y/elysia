// graph.js — pure graph-walk logic for the branch explorer. No DOM, no
// fetch, no globals: everything here is a function of a BranchIndex (see
// indexBranch()) plus starting coordinates.
//
// Terminology used in comments/identifiers here:
//   "line"  = a dentry (a real, non-system node)
//   "scene" = a branch (a dialogues row)
// The two user-facing concepts this module ultimately feeds:
//   "branch viewer" = the read-only auto-played chain of lines
//   "options list"  = the clickable fork buttons shown when the walk can't
//                      auto-advance on its own
// This module only produces the data for both — no rendering here.

/**
 * @typedef {Object} Entry  // one dentry, as returned by GET /branches/{id}
 * @property {number} dentry_id
 * @property {boolean} is_system
 * @property {boolean} is_player_choice
 * @property {string|null} condition
 * @property {string|null} effect       // raw userscript text, e.g. "SetVariableValue(...)"
 * @property {Object|null} check         // player-initiated check
 * @property {Object|null} passive_check // difficultypass-gated line
 */

/**
 * @typedef {Object} BranchIndex
 * @property {number} branchId
 * @property {Object<number, Entry>} entriesById
 * @property {Object<number, {to_dentry_id:number, leaves_branch:boolean, to_branch_id?:number}[]>} linksByOrigin
 * @property {{branch_id:number, dentry_id:number, branch_title:string|null, text:string|null}|null} trueEntry
 * @property {string|null} description  // ORB branches with no real dentries live entirely in this field
 * @property {string|null} title
 */

/** Builds a BranchIndex from GET /branches/{id}'s raw JSON. */
export function indexBranch(branchId, json) {
  const entriesById = {};
  const linksByOrigin = {};
  for (const e of json.entries) entriesById[e.dentry_id] = e;
  for (const l of json.links) {
    (linksByOrigin[l.from_dentry_id] ||= []).push(l);
  }
  return {
    branchId,
    entriesById,
    linksByOrigin,
    trueEntry: json.true_entry || null,
    description: json.description || null,
    title: json.title || null,
  };
}

/**
 * @typedef {Object} Step  // one real (non-system) node reached from some
 * origin, with the generic (non-outcome) conditions accumulated on the way.
 * @property {"line"|"cross"} type
 * @property {number} [dentryId]     // type === "line"
 * @property {number} [toBranch]     // type === "cross"
 * @property {number} [toDentry]     // type === "cross"
 * @property {string[]} conditions   // generic conditionstring gates seen on system nodes en route
 */

/**
 * Walks forward from dentryId through any number of is_system (junction)
 * nodes, returning every real (non-system) node reached — a "line" in the
 * current branch, or a "cross" marker for a link that leaves the branch
 * entirely. Does NOT do anything special with passive checks — a passive
 * check is a real, non-system dentry, so it's returned here like any
 * other line; classifyFork() (below) decides what to do with it.
 *
 * visited guards against cycles (the graph genuinely has some — confirmed,
 * e.g. branch 1199/236 loops through itself, not a data error). A cycle
 * simply stops contributing further steps down that path rather than
 * hanging.
 *
 * @param {BranchIndex} idx
 * @param {number} dentryId
 * @param {Set<number>} [visited]
 * @param {string[]} [conditions]
 * @returns {Step[]}
 */
export function nextRealSteps(idx, dentryId, visited, conditions) {
  visited = visited || new Set();
  conditions = conditions || [];
  const steps = [];
  const links = idx.linksByOrigin[dentryId] || [];
  for (const l of links) {
    if (l.leaves_branch) {
      steps.push({ type: "cross", toBranch: l.to_branch_id, toDentry: l.to_dentry_id, conditions });
      continue;
    }
    if (visited.has(l.to_dentry_id)) continue; // cycle guard
    const dest = idx.entriesById[l.to_dentry_id];
    if (!dest) continue;
    if (dest.is_system) {
      const nextConditions = dest.condition ? [...conditions, dest.condition] : conditions;
      const nextVisited = new Set(visited);
      nextVisited.add(l.to_dentry_id);
      steps.push(...nextRealSteps(idx, l.to_dentry_id, nextVisited, nextConditions));
    } else {
      const nextConditions = dest.condition ? [...conditions, dest.condition] : conditions;
      steps.push({ type: "line", dentryId: l.to_dentry_id, conditions: nextConditions });
    }
  }
  return steps;
}

// A node's identity for reconvergence/dedup purposes - two "line" steps in
// the same branch or two "cross" steps to the same (branch, dentry) count
// as the same node; a "cross" and a "line" never do, even by coincidence
// of numbers.
function stepKey(step) {
  return step.type === "cross" ? `cross:${step.toBranch}:${step.toDentry}` : `line:${step.dentryId}`;
}

/**
 * Full forward-reachable set of step-keys from a starting step, used only
 * to test reconvergence (see classifyFork) - not for rendering. Crosses
 * out of the branch are treated as opaque leaves (their own key, not
 * walked further - no fetch happens here, this module is pure/sync) so
 * two paths that both leave to the exact same (branch, dentry) still
 * count as reconverging, but a branch-crossing path is never compared
 * node-for-node against another branch's internals.
 * Bounded by the same cycle guard as nextRealSteps; also caps total
 * expansion as a defensive limit against a pathological fan-out, since
 * this walks the *whole* subtree rather than a single line.
 *
 * @param {BranchIndex} idx
 * @param {Step} startStep
 * @returns {Set<string>}
 */
function reachableKeys(idx, startStep) {
  const seen = new Set();
  const queue = [startStep];
  const visitedDentries = new Set();
  const MAX_NODES = 2000; // defensive; real branches are a few hundred nodes at most
  while (queue.length && seen.size < MAX_NODES) {
    const step = queue.shift();
    const key = stepKey(step);
    if (seen.has(key)) continue;
    seen.add(key);
    if (step.type === "cross") continue; // opaque leaf, see above
    if (visitedDentries.has(step.dentryId)) continue;
    visitedDentries.add(step.dentryId);
    for (const next of nextRealSteps(idx, step.dentryId, new Set(visitedDentries))) {
      queue.push(next);
    }
  }
  return seen;
}

/**
 * The passive-check exclusivity/reconvergence rule (see
 * misc/frontend-rewrite-decisions.md — this is THE rule, not an
 * approximation of it): a passive-check step is only eligible to become
 * its own option if it gates real content a sibling step at the same
 * fork can't otherwise reach.
 *
 * Concretely: walk the check's own downstream forward. If it reaches a
 * node that's also reachable from any sibling step (a "bypass" - the
 * check was skipped), truncate right there — that shared node isn't
 * exclusive. Anything strictly before that reconvergence point is
 * exclusive content, which makes the check eligible. A check with no
 * TRUE bypass sibling at all is always eligible - there is no way to
 * reach anything here without going through some check, so its own
 * content is exclusive to it by definition. "True bypass" specifically
 * excludes another passive-check sibling from counting: two (or more)
 * parallel passive checks that happen to reconverge on identical
 * downstream content are NOT a check-vs-skip-the-check situation, they're
 * duplicate/parallel content - confirmed on real data, branch 569: 286
 * forks into two separate system-node chains (287->289, 288->6), both
 * duplicate-text Encyclopedia passive checks, both eventually reaching
 * the exact same downstream (14's fork) - there is no way to reach that
 * fork at all without going through ONE of them, so treating either as
 * "the other one is a bypass" silently flattened BOTH away, leaking
 * their shared downstream up as if it belonged to neither - this is the
 * known, already-documented genuine-duplicate-dentries case (see
 * renderOptions' opt-id-dup handling), not something to collapse.
 *
 * @param {BranchIndex} idx
 * @param {Step} checkStep    // a "line" step whose entry has passive_check
 * @param {Step[]} siblings   // the other steps at the same fork
 * @returns {boolean}
 */
export function passiveCheckIsExclusive(idx, checkStep, siblings) {
  const bypassSiblings = siblings.filter(s => {
    if (s.type !== "line") return true; // a cross-branch link is always a real bypass
    const e = idx.entriesById[s.dentryId];
    return !(e && e.passive_check); // another passive check is never a "skip this one" bypass
  });
  if (!bypassSiblings.length) return true; // no real bypass exists at all - exclusive by definition
  const bypassReachable = new Set();
  for (const sib of bypassSiblings) for (const k of reachableKeys(idx, sib)) bypassReachable.add(k);

  // BFS the check's own downstream, one real step at a time, tracking the
  // dentries already walked past (for the cycle guard passed to
  // nextRealSteps) separately from the reconverged nodes still queued for
  // expansion (so a reconvergence-then-diverge-again case downstream is
  // still found, not just the very first layer). The moment any node is
  // found that the bypass side can't also reach, that's real exclusive
  // content - one way to frame it: "x -> A -> y -> q -> f -> z" vs
  // "x -> A -> z" - finding just y already proves it, so this can return
  // as soon as one such node turns up, no need to enumerate the rest.
  const visitedDentries = new Set([checkStep.dentryId]);
  const seenKeys = new Set([stepKey(checkStep)]);
  let queue = nextRealSteps(idx, checkStep.dentryId, visitedDentries);
  while (queue.length) {
    const step = queue.shift();
    const key = stepKey(step);
    if (seenKeys.has(key)) continue;
    seenKeys.add(key);
    if (bypassReachable.has(key)) {
      // reconverged here - not exclusive itself, but keep walking past it
      // in case the paths diverge again further downstream.
      if (step.type === "line") {
        visitedDentries.add(step.dentryId);
        queue.push(...nextRealSteps(idx, step.dentryId, visitedDentries));
      }
      continue;
    }
    return true; // real content the bypass side never reaches
  }
  return false; // every downstream node reconverges (or dead-ends) with no exclusive content
}

/**
 * The real options-list resolver. Two genuinely different situations,
 * both funneled through here so callers never have to tell them apart:
 *
 * 1. Exactly one way forward (nextRealSteps returns a single step) - this
 *    is NOT a decision point, whatever that one step is (plain line,
 *    lone passive check with no sibling to be exclusive against, cross-
 *    branch link). Returned as-is, `via` empty. The caller (the walking
 *    loop) just renders it and keeps walking - critically, this function
 *    does NOT recurse past it on its own. Doing that used to be the bug:
 *    a lone passive check has no siblings, so it's trivially never
 *    "exclusive", and recursing straight past it here meant it was
 *    silently skipped as content instead of just correctly not becoming
 *    an option - confirmed on branch 1370, walking from 5 through 291 (a
 *    lone passive check, single link forward, no sibling at that point
 *    at all) must still render 291 itself before continuing to discover
 *    293's real fork one loop iteration later.
 *
 * 2. A genuine fork (0 or 2+ steps) - resolved via resolveFork(), which
 *    recursively replaces any non-exclusive passive-check *candidate at
 *    that fork* with its own further-resolved downstream, carrying every
 *    intervening dentry it flattened through in that option's `via` -
 *    real content that still needs rendering once the option is taken,
 *    just never previewed as a badge on the button beforehand.
 *
 * A real (player-initiated) check is never collapsed either way - always
 * a genuine stop, same as always.
 *
 * @param {BranchIndex} idx
 * @param {number} dentryId
 * @param {Set<number>} [visited]
 * @returns {Step[]} the final, merged option steps (each `via`-annotated)
 */
export function resolveOptions(idx, dentryId, visited) {
  visited = visited || new Set([dentryId]);
  const steps = nextRealSteps(idx, dentryId, visited);
  if (steps.length === 1) return steps.map(s => ({ ...s, via: s.via || [] }));
  return mergeOptions(resolveFork(idx, steps, visited));
}

/**
 * Resolves one specific set of fork candidates (steps that already share
 * a common origin) into their final form - see resolveOptions() above for
 * when this does vs. doesn't apply. Not exported: always reached through
 * resolveOptions, which is the only thing that knows whether a given
 * `steps` array is actually a fork worth resolving this way.
 */
function resolveFork(idx, steps, visited) {
  const result = [];
  for (const step of steps) {
    if (step.type === "line") {
      const entry = idx.entriesById[step.dentryId];
      if (entry && entry.passive_check) {
        const siblings = steps.filter(s => s !== step);
        if (!passiveCheckIsExclusive(idx, step, siblings)) {
          result.push(...flattenThrough(idx, step, visited));
          continue;
        }
      }
    }
    result.push({ ...step, via: step.via || [] });
  }
  return result;
}

/**
 * Walks forward from a non-exclusive passive-check step until it reaches
 * either a further real fork (resolved recursively via resolveFork - the
 * "check leading straight into a further real fork" case, e.g. old 292)
 * or a single terminal step (just returned as-is, same reasoning as
 * resolveOptions' single-step case - no need to keep recursing through
 * further lone single-hop content here, the next loop iteration on the
 * caller's side will naturally continue discovering it). Every returned
 * option is prefixed with this check's own dentryId in `via`.
 */
function flattenThrough(idx, checkStep, visited) {
  const nextVisited = new Set(visited);
  nextVisited.add(checkStep.dentryId);
  const beyond = nextRealSteps(idx, checkStep.dentryId, nextVisited);
  const resolved = beyond.length === 1 ? beyond : resolveFork(idx, beyond, nextVisited);
  // checkStep's own `conditions` (accumulated by nextRealSteps on the way
  // TO the check itself, e.g. a gate on an intermediate system node) has
  // to travel with it into `via`, not get dropped - it's the same
  // "shown only if X" gate old renderNodeHtml threaded through as
  // extraConditions, just for a flattened-through line instead of a
  // collected one.
  const ownVia = { dentryId: checkStep.dentryId, conditions: checkStep.conditions || [] };
  return resolved.map(s => ({ ...s, via: [ownVia, ...(s.via || [])] }));
}

/**
 * Merges steps landing on the same destination into one. Same rule
 * applied independently to both per-merge fields, same reasoning in each
 * case: if even one of the merged paths reached the destination without
 * needing X (no gating condition / no passive check flattened through on
 * the way), X isn't a real requirement of the destination as a whole - a
 * leftover condition or flattened-check from a *different* path in is
 * just one specific way in, not a restriction on every way in. (This is
 * the fix for the old mergeIdenticalOptions bug where passThrough got a
 * blanket union with no such exclusion - see decisions log.)
 *
 * @param {Step[]} steps
 * @returns {Step[]}
 */
export function mergeOptions(steps) {
  const groups = new Map();
  const order = [];
  for (const s of steps) {
    const key = stepKey(s);
    if (!groups.has(key)) { groups.set(key, []); order.push(key); }
    groups.get(key).push(s);
  }
  const merged = [];
  for (const key of order) {
    const group = groups.get(key);
    if (group.length === 1) { merged.push(group[0]); continue; }
    const anyUnconditional = group.some(s => !s.conditions || s.conditions.length === 0);
    const anyDirectVia = group.some(s => !s.via || s.via.length === 0);
    merged.push({
      ...group[0],
      conditions: anyUnconditional ? [] : group[0].conditions,
      via: anyDirectVia ? [] : group[0].via,
    });
  }
  return merged;
}

/**
 * @typedef {Object} CollectedLine
 * @property {number} dentryId
 * @property {string[]} conditions  // gates on an intermediate system node
 *   passed through (skipped) on the way here - not on this line's own
 *   conditionstring necessarily. Old app called this extraConditions.
 */
/**
 * @typedef {Object} Chain
 * @property {CollectedLine[]} collected  // real lines to render, in
 *                                  order, since the chain's own start
 *                                  point only
 * @property {Step[]} options      // 0 or 2+ final options to stop on;
 *                                  empty means a genuine dead end
 * @property {number} branchId     // may differ from the call's own
 *                                  branchId if a cross-branch link was
 *                                  auto-walked through along the way
 */

/**
 * Auto-plays forward from (branchId, dentryId) exactly like the branch
 * viewer should: one real hop at a time via resolveOptions, rendering
 * every line collected (including anything a flattened-through option's
 * `via` carries) until it hits a genuine fork/dead end.
 *
 * Deliberately stateless and start-point-only: each call is a pure
 * function of where it starts, with zero memory of any *previous* chain -
 * this is what makes "choosing an option re-renders fresh from the new
 * anchor, not an ever-growing transcript of the whole playthrough" the
 * only possible behavior, rather than something a caller has to
 * separately remember to enforce - the demo page was once caught failing
 * to clear between choices; the fix belongs here, at the one place that
 * decides what a chain contains, not scattered across every caller that
 * walks one.
 *
 * A cross-branch link mid-chain can't be followed further without that
 * other branch's own BranchIndex, which this module has no way to fetch
 * (pure, no I/O) - it stops there and returns a single "cross" option,
 * leaving the actual branch-switch + continuation to the caller (fetch
 * the new branch, call resolveChain again on it).
 *
 * @param {BranchIndex} idx
 * @param {number} dentryId
 * @returns {Chain}
 */
export function resolveChain(idx, dentryId) {
  const collected = [];
  let current = dentryId;
  // Conditions gating the *next* thing collected - carried forward from
  // whatever Step landed on it (a system-node gate on the way there),
  // same convention the old app's `pendingConditions` used. Starts empty:
  // resolveChain has no memory of anything before its own start point,
  // same as the old code starting fresh at the top of renderChainFrom.
  let pendingConditions = [];
  const visited = new Set();
  while (true) {
    if (visited.has(current)) return { collected, options: [], branchId: idx.branchId }; // cycle guard
    visited.add(current);

    const entry = idx.entriesById[current];
    if (entry && !entry.is_system) {
      collected.push({ dentryId: current, conditions: pendingConditions });
      pendingConditions = [];
    }

    const options = resolveOptions(idx, current, new Set([current]));
    if (options.length !== 1) return { collected, options, branchId: idx.branchId };

    const only = options[0];
    for (const v of only.via || []) collected.push({ dentryId: v.dentryId, conditions: v.conditions || [] });
    if (only.type === "cross") return { collected, options: [only], branchId: idx.branchId };
    pendingConditions = only.conditions || [];
    current = only.dentryId;
  }
}
