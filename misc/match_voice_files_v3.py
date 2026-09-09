"""match_voice_files_v3.py - the real fix, using both dialogue db vintages.

Background (see elysia-project-notes memory for the full writeup): the
raw voice dump's filenames (`{actor}-{branch title}-{N}.fsb`, N a literal
dentries.id) were authored against the OLD (TFC-era) dialogue db, not
the current JV one - even though the actual audio content was dumped
from a JV game install. v1/v2 both failed because they resolved N
against the current (JV) db directly, where it's simply not a valid key.

Two-stage approach:
  Stage 1 (resolve): same method as v1 - branch title -> old
    conversationid, N -> old dentries.id - but against the OLD db, where
    N is actually meaningful.
  Stage 2 (bridge): within the same-titled branch in the JV db, find the
    dentry whose dialoguetext matches the old dentry's text exactly.
    Ambiguous (multiple JV dentries with identical text in that branch)
    breaks the tie by matching actor too; if still ambiguous, left
    unresolved rather than guessed. A text with no match in the JV
    branch is reported as its own category (new-in-JV or rewritten
    line), not folded into "unmatched".

Usage: python3 match_voice_files_v3.py <old.db> <jv.db> <sound.db>
"""
import re
import sqlite3
import sys
from collections import defaultdict

PRIMARY_RE = re.compile(r"^(?P<label>.+?)-(?P<branch>.+?)-(?P<n>\d+)\.fsb$", re.IGNORECASE)
ALT_RE = re.compile(r"^alternative-(?P<alt>\d+)-(?P<label>.+?)-(?P<branch>.+?)-(?P<n>\d+)-(?P<suffix>\d+)\.fsb$", re.IGNORECASE)


def normalize_title(s: str) -> str:
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_text(s: str) -> str:
    """For content matching, not display - collapse whitespace only,
    keep case/punctuation (the writing itself shouldn't need fuzzy
    matching if it's truly the same line carried over unchanged)."""
    return re.sub(r"\s+", " ", (s or "")).strip()


def load_db(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    title_to_cids = defaultdict(list)
    for cid, title in conn.execute("SELECT id, title FROM dialogues"):
        title_to_cids[normalize_title(title)].append(cid)
    dentries = {}  # (cid, id) -> (actor_name, text)
    actor_names = dict(conn.execute("SELECT id, name FROM actors"))
    for cid, did, actor_id, text in conn.execute("SELECT conversationid, id, actor, dialoguetext FROM dentries"):
        dentries[(cid, did)] = (actor_names.get(actor_id), text)
    return title_to_cids, dentries


def stage1_resolve(old_title_to_cids, old_dentries, sound_rows):
    """filename -> (old_cid, old_n), same method as v1 but against the
    old (TFC) db."""
    result = {}
    stats = defaultdict(int)
    for sound_id, rel_path, filename in sound_rows:
        m = ALT_RE.match(filename) or PRIMARY_RE.match(filename)
        if not m:
            stats["unparseable"] += 1
            continue
        branch_key = normalize_title(m.group("branch"))
        n = int(m.group("n"))
        cids = old_title_to_cids.get(branch_key)
        if not cids:
            stats["old_branch_not_found"] += 1
            continue
        if len(cids) > 1:
            stats["old_branch_ambiguous"] += 1
            continue
        cid = cids[0]
        if (cid, n) not in old_dentries:
            stats["old_dentry_not_found"] += 1
            continue
        result[sound_id] = (cid, n)
        stats["stage1_resolved"] += 1
    return result, stats


def stage2_bridge(stage1_result, old_title_to_cids, old_dentries, jv_title_to_cids, jv_dentries):
    """(old_cid, old_n) -> (jv_cid, jv_n), via same-titled branch +
    dialoguetext match. Three tiers, in order:
      1. unique text match within the branch
      2. text matches >1 dentry, but only one shares the old line's actor
      3. still tied (same text, same actor, appears K times on both
         sides) - pair the old and new occurrences by sorted rank
         within the branch (1st old occurrence <-> 1st JV occurrence,
         etc.), on the theory that narrative order survives renumbering
         even when the IDs don't. Verified 2026-09-10 against 15 manual
         samples from JAM/MONUMENT REFLECTION (the single largest
         contributor to tier-3 cases) before trusting this at scale -
         resolves 97% of what tier 1/2 leave behind. The remaining ~3%
         is where the occurrence COUNT itself differs between old and
         JV (a real added/removed/rewritten repetition, not a
         resolvable case from data alone) - left unmatched, not guessed."""
    old_cid_to_title = {}
    for title, cids in old_title_to_cids.items():
        for cid in cids:
            old_cid_to_title[cid] = title

    jv_text_index_cache = {}
    def jv_text_index(jv_cid):
        if jv_cid not in jv_text_index_cache:
            idx = defaultdict(list)
            for (cid, did), (actor, text) in jv_dentries.items():
                if cid == jv_cid:
                    idx[normalize_text(text)].append((did, actor))
            jv_text_index_cache[jv_cid] = idx
        return jv_text_index_cache[jv_cid]

    # (old_cid, actor, normalized text) -> sorted list of old dentry ids
    # sharing that exact line - built once, used only for tier 3.
    old_groups = defaultdict(list)
    for (cid, did), (actor, text) in old_dentries.items():
        nt = normalize_text(text)
        if nt:
            old_groups[(cid, actor, nt)].append(did)
    for v in old_groups.values():
        v.sort()

    final = {}
    stats = defaultdict(int)
    for sound_id, (old_cid, old_n) in stage1_result.items():
        title = old_cid_to_title.get(old_cid)
        jv_cids = jv_title_to_cids.get(title) if title else None
        if not jv_cids or len(jv_cids) != 1:
            stats["jv_branch_not_found_or_ambiguous"] += 1
            continue
        jv_cid = jv_cids[0]

        old_actor, old_text = old_dentries[(old_cid, old_n)]
        norm_old_text = normalize_text(old_text)
        if not norm_old_text:
            stats["old_text_empty"] += 1
            continue

        candidates = jv_text_index(jv_cid).get(norm_old_text)
        if not candidates:
            stats["no_text_match_in_jv"] += 1
            continue
        if len(candidates) == 1:
            final[sound_id] = (jv_cid, candidates[0][0])
            stats["resolved_unique_text"] += 1
            continue

        actor_matches = [c for c in candidates if c[1] == old_actor]
        if len(actor_matches) == 1:
            final[sound_id] = (jv_cid, actor_matches[0][0])
            stats["resolved_actor_tiebreak"] += 1
            continue

        if len(actor_matches) < 2:
            stats["ambiguous_no_actor_match"] += 1
            continue

        old_occurrences = old_groups.get((old_cid, old_actor, norm_old_text), [])
        if len(old_occurrences) != len(actor_matches):
            stats["ambiguous_occurrence_count_mismatch"] += 1
            continue
        actor_matches_sorted = sorted(actor_matches)
        try:
            rank = old_occurrences.index(old_n)
        except ValueError:
            stats["ambiguous_rank_lookup_failed"] += 1
            continue
        final[sound_id] = (jv_cid, actor_matches_sorted[rank][0])
        stats["resolved_ordinal_rank"] += 1

    return final, stats


if __name__ == "__main__":
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <old.db> <jv.db> <sound.db>")
        sys.exit(1)

    old_path, jv_path, sound_path = sys.argv[1], sys.argv[2], sys.argv[3]

    print("loading dbs...", flush=True)
    old_title_to_cids, old_dentries = load_db(old_path)
    jv_title_to_cids, jv_dentries = load_db(jv_path)

    conn = sqlite3.connect(f"file:{sound_path}?mode=ro", uri=True)
    sound_rows = conn.execute("SELECT id, rel_path, filename FROM voice_files").fetchall()
    print(f"{len(sound_rows)} sound files loaded", flush=True)

    stage1_result, s1_stats = stage1_resolve(old_title_to_cids, old_dentries, sound_rows)
    print("\n--- stage 1 (resolve against TFC db) ---")
    for k, v in s1_stats.items():
        print(f"  {k:35s}: {v}")

    final, s2_stats = stage2_bridge(stage1_result, old_title_to_cids, old_dentries, jv_title_to_cids, jv_dentries)
    print("\n--- stage 2 (bridge TFC -> JV via text match) ---")
    for k, v in s2_stats.items():
        print(f"  {k:35s}: {v}")

    print(f"\nFINAL: {len(final)} / {len(sound_rows)} files resolved to a current JV (branch, dentry)")

    import json
    with open("/tmp/v3_final_result.json", "w") as f:
        json.dump({str(k): v for k, v in final.items()}, f)
    print("wrote /tmp/v3_final_result.json")
