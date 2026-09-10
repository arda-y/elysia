"""match_voice_files_v7.py - v6 + trusting the OLD db's actor tag when
the raw dump's folder name is itself wrong.

New in v7: for unique-text-but-wrong-actor cases, check whether the OLD
(TFC) db's own actor tag for that dentry - not the raw dump's folder
name - uniquely matches one of the JV candidates. Found by hand: 3
files in GATES SCAB LEADER were folder-labeled "composure", but the OLD
db's actor field for that exact text said "Savoir Faire" - which is
also the correct JV owner. The raw dump's folder was simply mislabeled
for these, not a matching-logic problem. Checked at scale before
trusting broadly: 385/638 (60%) of the remaining unique-text-wrong-
actor bucket recovers this way. The other 229 have a JUNK_OLD_ACTORS
placeholder (HUB/You) as their old-db actor - no signal there, same
placeholder-collision problem as everywhere else, still needs
individual review. Tried as a fallback only when the real-folder-actor
filter finds nothing, same rigor bar as every other tier (exact text +
exact actor agreement, just against a different actor source).

New in v6: for files v5 still couldn't resolve (old-side text didn't
exact-match anything in the JV branch, usually because the wording
drifted slightly between TFC and JV), try one more cheap check before
giving up - does a JV dentry exist at the *same numeric id* as the
file's old N, in the same branch, spoken by the same real (folder)
actor? Found by hand: 4/4 "old_dentry_not_found" files in WHIRLING KIM
MAIN resolved exactly this way (branch content added after the old
snapshot was taken, but JV's numbering for it already matches what the
files expect). Checked at scale before trusting it broadly: 42.1%
(8/19) hit rate on the old_dentry_not_found bucket, 17.9% (219/1224) on
the wider actor-mismatch bucket - real, non-random signal (it's an
exact actor match, same confidence bar as every other tier), but
partial coverage, not a general fix. Tried last, after every v5 tier,
and only ever accepted on exact actor agreement - same rigor as
everything else, just one more cheap thing to try before falling back
to manual review.

v5's own fix (unchanged, kept for context): fixes the hyphenated-actor-
name parsing bug.

What was wrong: v1-v4 all parsed filenames with a single generic regex
(`^(?P<label>.+?)-(?P<branch>.+?)-(?P<n>\\d+)\\.fsb$`) that assumes the
*first* hyphen in the filename separates the actor label from the
branch title. Wrong whenever the actor's own name contains a hyphen
(e.g. "Mega Rich Light-Bending Guy", "Horse-Faced Woman") or is a
negative-numbered object ("Door, Room -3") - the label capture stops
short, and the leftover fragment gets swallowed into the branch title,
corrupting it (e.g. branch parses as "BENDING GUY CONTAINERYARD LIGHT
BENDING GUY" instead of "CONTAINERYARD LIGHT BENDING GUY"). Checked at
scale: 628 of the 899 "old_branch_not_found" files under v4 (70%) trace
to just 20 such actor/object names.

Fix: use the raw dump's own folder structure
(`{location}/{actor_lowercased}/{file}.fsb`) as the anchor - the
folder segment is never mangled by this bug, since it's a separate
path component, not something the label/branch regex has to guess at.
Strip that known-correct actor name off the front of the filename
first, then parse the remainder (which is now just "{branch}-{n}.fsb"
or "{branch}-{n}-{s}.fsb" for alt-takes) with no ambiguity left to
guess at. Falls back to the old generic regex only if the folder-anchor
strip doesn't cleanly apply (e.g. malformed path), so nothing that
worked before regresses.

Everything else - stage 1 (TFC resolve), the actor-verified text-bridge
tiers from v4 - is unchanged.

Usage: python3 match_voice_files_v7.py <old.db> <jv.db> <sound.db>
"""
import re
import sqlite3
import sys
import unicodedata
from collections import defaultdict

PRIMARY_RE = re.compile(r"^(?P<label>.+?)-(?P<branch>.+?)-(?P<n>\d+)\.fsb$", re.IGNORECASE)

# Structural/placeholder actor names that carry no real identity signal -
# an old-db dentry tagged with one of these is a bookkeeping node, not
# a genuine speaker, so trusting it as a disambiguator (v7's new tier)
# would just produce false confidence.
JUNK_OLD_ACTORS = {"HUB", "You"}
ALT_RE = re.compile(r"^alternative-(?P<alt>\d+)-(?P<label>.+?)-(?P<branch>.+?)-(?P<n>\d+)-(?P<suffix>\d+)\.fsb$", re.IGNORECASE)
ALT_PREFIX_RE = re.compile(r"^alternative-(?P<alt>\d+)-", re.IGNORECASE)
BRANCH_N_RE = re.compile(r"^(?P<branch>.+)-(?P<n>\d+)\.fsb$")
BRANCH_N_S_RE = re.compile(r"^(?P<branch>.+)-(?P<n>\d+)-(?P<suffix>\d+)\.fsb$")


def normalize_title(s: str) -> str:
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "")).strip()


def tight_normalize_actor(s: str) -> str:
    if not s:
        return ""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.upper()
    return re.sub(r"[^A-Z0-9]", "", s)


def real_folder_actor(rel_path: str) -> str | None:
    parts = rel_path.split("/")
    if len(parts) >= 3 and parts[-2].lower() == "alternative":
        return parts[-3]
    if len(parts) >= 2:
        return parts[-2]
    return None


FIXED_PREFIX_RE = re.compile(r"^fixed-", re.IGNORECASE)


def parse_filename(filename: str, folder_actor: str | None):
    """Returns (branch_text, n, alt_index) or None. Tries the
    folder-anchored strip first (handles hyphenated actor names
    correctly), falls back to the old generic regex if that doesn't
    cleanly apply."""
    alt_index = None
    rest = filename
    m_fixed = FIXED_PREFIX_RE.match(rest)
    if m_fixed:
        # a handful of re-recorded takes (16, checked 2026-09-10) carry
        # a literal "fixed-" marker before the actor name - strip it
        # before the folder-anchor match, same idea as the alt-index
        # prefix below.
        rest = rest[m_fixed.end():]
    m_alt = ALT_PREFIX_RE.match(rest)
    if m_alt:
        alt_index = int(m_alt.group("alt"))
        rest = rest[m_alt.end():]

    if folder_actor:
        prefix = folder_actor + "-"
        if rest.lower().startswith(prefix.lower()):
            rest = rest[len(prefix):]
            m = BRANCH_N_S_RE.match(rest) if alt_index is not None else BRANCH_N_RE.match(rest)
            if m:
                return m.group("branch"), int(m.group("n")), alt_index

    # fallback: old generic lazy-regex behavior
    m = ALT_RE.match(filename)
    if m:
        return m.group("branch"), int(m.group("n")), int(m.group("alt"))
    m = PRIMARY_RE.match(filename)
    if m:
        return m.group("branch"), int(m.group("n")), None
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
        folder_actor = real_folder_actor(rel_path)
        parsed = parse_filename(filename, folder_actor)
        if not parsed:
            stats["unparseable"] += 1
            continue
        branch_text, n, alt_index = parsed
        branch_key = normalize_title(branch_text)
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

        if real_actor_n and real_actor_n != "ALTERNATIVE":
            actor_ok_candidates = [c for c in candidates if tight_normalize_actor(c[1]) == real_actor_n]
        else:
            actor_ok_candidates = candidates

        if not actor_ok_candidates:
            # NEW in v7: the raw dump's folder name can itself be wrong
            # (not just a matching-logic problem) - before giving up,
            # check whether the OLD db's own actor tag for this exact
            # dentry uniquely matches one of the candidates. Skip
            # placeholder old-actors entirely - they carry no signal
            # and would just produce false confidence.
            if old_actor not in JUNK_OLD_ACTORS:
                old_actor_matches = [c for c in candidates if tight_normalize_actor(c[1]) == tight_normalize_actor(old_actor)]
                if len(old_actor_matches) == 1:
                    final[sound_id] = (jv_cid, old_actor_matches[0][0])
                    stats["resolved_old_actor_trust"] += 1
                    continue
            stats["unique_text_actor_mismatch" if len(candidates) == 1 else "all_candidates_actor_mismatch"] += 1
            continue

        if len(actor_ok_candidates) == 1:
            final[sound_id] = (jv_cid, actor_ok_candidates[0][0])
            stats["resolved_unique_actor_verified"] += 1
            continue

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


def stage3_same_number_fallback(stage1_result, already_resolved, sound_meta,
                                 old_title_to_cids, jv_title_to_cids, jv_dentries):
    """For files stage2 couldn't resolve: does a JV dentry exist at the
    file's own old N, same branch, same real actor? Exact-actor-match
    only - never applied on a guess."""
    old_cid_to_title = {}
    for title, cids in old_title_to_cids.items():
        for cid in cids:
            old_cid_to_title[cid] = title

    extra = {}
    stats = defaultdict(int)
    for sound_id, (old_cid, old_n) in stage1_result.items():
        if sound_id in already_resolved:
            continue
        title = old_cid_to_title.get(old_cid)
        jv_cids = jv_title_to_cids.get(title) if title else None
        if not jv_cids or len(jv_cids) != 1:
            continue
        jv_cid = jv_cids[0]

        real_actor = real_folder_actor(sound_meta.get(sound_id, ""))
        real_actor_n = tight_normalize_actor(real_actor) if real_actor else None
        if not real_actor_n or real_actor_n == "ALTERNATIVE":
            continue

        entry = jv_dentries.get((jv_cid, old_n))
        if not entry or not entry[0]:
            stats["same_number_no_dentry_or_actor"] += 1
            continue
        if tight_normalize_actor(entry[0]) != real_actor_n:
            stats["same_number_actor_mismatch"] += 1
            continue

        extra[sound_id] = (jv_cid, old_n)
        stats["resolved_same_number_fallback"] += 1

    return extra, stats


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
    print("\n--- stage 1 (resolve against TFC db, folder-anchored parse) ---")
    for k, v in s1_stats.items():
        print(f"  {k:35s}: {v}")

    final, s2_stats = stage2_bridge(stage1_result, sound_meta, old_title_to_cids, old_dentries, jv_title_to_cids, jv_dentries)
    print("\n--- stage 2 (bridge TFC -> JV via text match, actor-verified) ---")
    for k, v in s2_stats.items():
        print(f"  {k:35s}: {v}")

    extra, s3_stats = stage3_same_number_fallback(stage1_result, final, sound_meta, old_title_to_cids, jv_title_to_cids, jv_dentries)
    print("\n--- stage 3 (same-number/same-actor fallback) ---")
    for k, v in s3_stats.items():
        print(f"  {k:35s}: {v}")
    final.update(extra)

    print(f"\nFINAL: {len(final)} / {len(sound_rows)} files resolved to a current JV (branch, dentry)")

    import json
    with open("/tmp/v7_final_result.json", "w") as f:
        json.dump({str(k): v for k, v in final.items()}, f)
    print("wrote /tmp/v7_final_result.json")
