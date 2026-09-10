"""match_voice_files_v4.py - v3 + a real actor cross-check at every tier.

What was wrong with v3: its tier 1 ("unique text match in the branch")
never checked actor agreement at all - only tiers 2/3 (which only run
when a text is ambiguous) did. Found by hand-reviewing two of the
"stuck" cases (see misc/stuck_review data / elysia-project-notes memory,
2026-09-10 round 2): both had a file whose real recorded actor (from its
own folder path) exactly matched a *different*, currently-unclaimed
dentry's text verbatim - but that dentry was already claimed by a
completely unrelated actor's file via tier 1, which never checked.
Checked at scale: 1,234/46,121 (2.68%) of v3's "resolved" matches have a
real folder-actor that disagrees with the resolved dentry's actual
actor - concentrated heavily in CONTAINERYARD EVRART, a branch dense
with skill-check reaction lines. Two hand-verified examples confirmed
these are genuine wrong matches (two different, unrelated lines that
happened to share identical text), not legitimate cross-version actor
reassignments.

Fix: require the file's own real actor (parsed from its rel_path, not
the old db's actor field, which is a separate and less trustworthy
signal) to match the candidate JV dentry's actor at EVERY tier, not
just as a disambiguator when a text is already ambiguous. A unique
text match with a disagreeing actor is now its own explicit rejection
category (`unique_text_actor_mismatch`), not a silent wrong accept.

Everything else - stage 1 (resolve against TFC db), the text-bridge
tiers, ordinal-rank pairing for repeated lines - is identical to v3.

Usage: python3 match_voice_files_v4.py <old.db> <jv.db> <sound.db>
"""
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict

PRIMARY_RE = re.compile(r"^(?P<label>.+?)-(?P<branch>.+?)-(?P<n>\d+)\.fsb$", re.IGNORECASE)
ALT_RE = re.compile(r"^alternative-(?P<alt>\d+)-(?P<label>.+?)-(?P<branch>.+?)-(?P<n>\d+)-(?P<suffix>\d+)\.fsb$", re.IGNORECASE)


def normalize_title(s: str) -> str:
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def tight_normalize_actor(s: str) -> str:
    """Actor-name equality check, not display - strips accents (the raw
    dump's folder names are plain ASCII, e.g. "manana" for "Mañana")
    and ALL non-alphanumeric with no space substitution (so "Hand/Eye
    Coordination" and its folder form "handeye coordination" compare
    equal) - deliberately tighter than normalize_title, which is for
    branch titles where word-boundary preservation matters."""
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    return re.sub(r"[^A-Z0-9]", "", s)


def real_folder_actor(rel_path: str) -> str | None:
    """The actor segment of rel_path - one level deeper for alt-takes
    nested under an "Alternative" subfolder."""
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[-2].lower() == "alternative":
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return None


def load_db(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    title_to_cids = defaultdict(list)
    for cid, title in conn.execute("SELECT id, title FROM dialogues"):
        title_to_cids[normalize_title(title)].append(cid)
    dentries = {}
    actor_names = dict(conn.execute("SELECT id, name FROM actors"))
    for cid, did, actor_id, text in conn.execute("SELECT conversationid, id, actor, dialoguetext FROM dentries"):
        dentries[(cid, did)] = (actor_names.get(actor_id), text)
    return title_to_cids, dentries


def stage1_resolve(old_title_to_cids, old_dentries, sound_rows):
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


def stage2_bridge(stage1_result, sound_meta, old_title_to_cids, old_dentries, jv_title_to_cids, jv_dentries):
    """sound_meta: sound_id -> rel_path, for the real-actor check."""
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

        real_actor = real_folder_actor(sound_meta.get(sound_id, ""))
        real_actor_n = tight_normalize_actor(real_actor) if real_actor else None

        candidates = jv_text_index(jv_cid).get(norm_old_text)
        if not candidates:
            stats["no_text_match_in_jv"] += 1
            continue

        # NEW: filter every candidate by real-actor agreement up front,
        # regardless of tier - this is the actual fix over v3.
        if real_actor_n and real_actor_n not in ("ALTERNATIVE",):
            actor_ok_candidates = [c for c in candidates if tight_normalize_actor(c[1]) == real_actor_n]
        else:
            # no usable real-actor signal (e.g. malformed path) - fall
            # back to v3's behavior rather than reject everything.
            actor_ok_candidates = candidates

        if not actor_ok_candidates:
            stats["unique_text_actor_mismatch" if len(candidates) == 1 else "all_candidates_actor_mismatch"] += 1
            continue

        if len(actor_ok_candidates) == 1:
            final[sound_id] = (jv_cid, actor_ok_candidates[0][0])
            stats["resolved_unique_actor_verified"] += 1
            continue

        # still >1 after the real-actor filter - fall back to v3's
        # old-actor tiebreak, then ordinal rank, scoped to this
        # already-actor-filtered candidate set.
        actor_matches = [c for c in actor_ok_candidates if c[1] == old_actor]
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
    sound_meta = {sid: rel_path for sid, rel_path, _ in sound_rows}
    print(f"{len(sound_rows)} sound files loaded", flush=True)

    stage1_result, s1_stats = stage1_resolve(old_title_to_cids, old_dentries, sound_rows)
    print("\n--- stage 1 (resolve against TFC db) ---")
    for k, v in s1_stats.items():
        print(f"  {k:35s}: {v}")

    final, s2_stats = stage2_bridge(stage1_result, sound_meta, old_title_to_cids, old_dentries, jv_title_to_cids, jv_dentries)
    print("\n--- stage 2 (bridge TFC -> JV via text match, actor-verified) ---")
    for k, v in s2_stats.items():
        print(f"  {k:35s}: {v}")

    print(f"\nFINAL: {len(final)} / {len(sound_rows)} files resolved to a current JV (branch, dentry)")

    import json
    with open("/tmp/v4_final_result.json", "w") as f:
        json.dump({str(k): v for k, v in final.items()}, f)
    print("wrote /tmp/v4_final_result.json")
