// back.js — graph-native "back": the nearest real (non-system)
// predecessor of a given (branchId, dentryId), found by walking dlinks
// backward exactly symmetrically to how the forward walk (graph.js)
// skips over system/junction nodes. Not click-replay - works identically
// regardless of how the user arrived at the current screen (a chain of
// clicks, a search result, a pasted line address), and can cross a branch
// boundary backward the same way the forward walk can cross one forward.
//
// Deliberately separate from graph.js: that module's whole contract is
// "pure function of an already-fetched BranchIndex, no I/O" - but a
// predecessor can live in a branch that was never fetched at all (that's
// the point: GET /branches/{id}'s own `links` only ever holds edges OUT
// of that branch, so walking backward needs a dedicated endpoint,
// potentially several sequential calls deep). fetchPredecessors/
// fetchLineInfo are injected rather than hardcoded to a real fetch() call
// so this stays testable the same way graph.js's checks are - real HTTP
// calls against the live API, just parameterized instead of hardcoded to
// one base URL.

/**
 * @callback FetchPredecessors
 * @param {number} branchId
 * @param {number} dentryId
 * @returns {Promise<{branch_id:number, dentry_id:number}[]>}
 */
/**
 * @callback FetchLineInfo
 * @param {number} branchId
 * @param {number} dentryId
 * @returns {Promise<{is_system:boolean}|null>}
 */

/**
 * @param {FetchPredecessors} fetchPredecessors
 * @param {FetchLineInfo} fetchLineInfo
 * @param {number} branchId
 * @param {number} dentryId
 * @param {Set<string>} [visited] - cycle guard, same reasoning as the
 *   forward walk (the graph genuinely loops in places, e.g. 1199/236)
 * @returns {Promise<{branchId:number, dentryId:number}|null>} null means
 *   a true root - nothing points here at all, or every predecessor
 *   candidate was exhausted without finding a real line.
 */
export async function resolvePrevReal(fetchPredecessors, fetchLineInfo, branchId, dentryId, visited) {
  visited = visited || new Set();
  const key = `${branchId}:${dentryId}`;
  if (visited.has(key)) return null;
  visited.add(key);

  const preds = await fetchPredecessors(branchId, dentryId);
  if (!preds.length) return null;

  // A node with more than one incoming edge is inherently ambiguous in a
  // graph that isn't strictly tree-shaped - resolved with one fixed,
  // deterministic rule (lowest branch id, then lowest dentry id) rather
  // than guessing "the" one true path, same tradeoff as the duplicate-
  // option preview elsewhere in this project.
  const sorted = [...preds].sort((a, b) => a.branch_id - b.branch_id || a.dentry_id - b.dentry_id);

  for (const p of sorted) {
    const info = await fetchLineInfo(p.branch_id, p.dentry_id);
    if (!info) continue;
    if (!info.is_system) return { branchId: p.branch_id, dentryId: p.dentry_id };
    const deeper = await resolvePrevReal(fetchPredecessors, fetchLineInfo, p.branch_id, p.dentry_id, visited);
    if (deeper) return deeper;
  }
  return null;
}
