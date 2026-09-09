"""match_voice_files_v2.py - v2 of the raw-dump -> dentry matcher.

Why v1 wasn't enough: v1 (match_voice_files.py) resolves a file's branch
title -> conversationid, then trusts the filename's trailing N as a
literal dentries.id within that conversation. That produced 46,270/47,155
"matches" against the current (JV) dialogue db - but 10,508 of those
(22.7%) resolved to a dentry whose real actor is "You" (the player
narrator line), which never has a voice line. Concrete example: a file
named "Alice-PLAZA COUPRIS KINEEMA-124.fsb" resolved to (776, 124), a
dentry whose actual line is a plain "You" action line, not Alice's.
So N-as-dentry-id alone is not trustworthy against this dialogue db.

What's new here: the raw dump's own folder structure carries a second,
independent signal v1 never used -
  {location}/{actor_lowercased}/{Actor Name}-{LOCATION}  {SUBTITLE}-{N}.fsb
`folder` (the location slug, e.g. "whirling-f3") is more consistently
formatted than the embedded branch-title text (e.g. "WHIRLING F3"). And
critically, `rel_path`'s middle path segment is the actor's own name,
lowercased - independent of the filename's "label" prefix, which v1's
own docstring already showed is unreliable (can be a skill/check name
instead of the real actor).

Matching strategy:
  1. Parse filename exactly like v1 (same two regexes) to get label,
     branch title, N.
  2. Resolve branch title -> conversationid the same way (normalized
     title lookup), same ambiguity handling as v1.
  3. Look up (conversationid, N) in dentries, same as v1.
  4. NEW: extract the actor-folder segment from rel_path (the directory
     directly containing the file) and normalize it the same way as
     actor names. Cross-check it against the resolved dentry's real
     actor name (dentries.actor -> actors.name).
  5. Accept the match ONLY if the actor-folder agrees with the resolved
     dentry's real actor. Disagreement is treated as a rejection, not a
     lower-confidence guess - v1's blind trust in N is exactly what
     produced the 10,508 "You" collisions, so any disagreement here is
     good evidence N doesn't point at the right dentry for this file at
     all, not evidence the actor field is wrong.

This trades recall for precision on purpose: rejecting a real match
because of a normalization edge case is a smaller problem (falls back
to "no voice line", same graceful-degradation as no match at all) than
accepting a false one (serves the wrong line's audio, or worse, silently
overwrites a correct match with a wrong one, as v1 did).

Usage: python3 match_voice_files_v2.py <dialogue.db> <sound.db>
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


def normalize_actor(s: str) -> str:
    """Same idea as normalize_title but for actor names/folder segments -
    case-insensitive, punctuation-insensitive (folder segments drop
    commas/parens that actors.name keeps, e.g. "Klaasje (Miss Oranje...)"
    vs a folder segment that may or may not keep the parenthetical)."""
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_dialogue_db(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    title_to_cids = defaultdict(list)
    for cid, title in conn.execute("SELECT id, title FROM dialogues"):
        title_to_cids[normalize_title(title)].append(cid)

    # (conversationid, dentry id) -> real actor name, normalized. NULL
    # actor (rare, e.g. group lines) maps to None - treated as "no
    # cross-check possible", handled explicitly below rather than
    # silently matching everything.
    actor_names = dict(conn.execute("SELECT id, name FROM actors"))
    dentry_actor = {}
    for cid, did, actor_id in conn.execute("SELECT conversationid, id, actor FROM dentries"):
        name = actor_names.get(actor_id)
        dentry_actor[(cid, did)] = normalize_actor(name) if name else None

    return title_to_cids, dentry_actor


def load_sound_db(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return conn.execute("SELECT id, rel_path, filename FROM voice_files").fetchall()


def actor_folder_from_rel_path(rel_path: str) -> str | None:
    """rel_path looks like "location/actor name/Actor Name-BRANCH-N.fsb" -
    the actor segment is the second-to-last path component. A handful of
    files may not follow the two-level convention; return None rather
    than guess."""
    parts = rel_path.split("/")
    if len(parts) < 2:
        return None
    return normalize_actor(parts[-2])


def match(dialogue_db_path, sound_db_path):
    title_to_cids, dentry_actor = load_dialogue_db(dialogue_db_path)
    sound_rows = load_sound_db(sound_db_path)

    result = {}
    stats = defaultdict(int)

    for sound_id, rel_path, filename in sound_rows:
        m = ALT_RE.match(filename) or PRIMARY_RE.match(filename)
        if not m:
            stats["unparseable"] += 1
            continue

        branch_key = normalize_title(m.group("branch"))
        n = int(m.group("n"))
        cids = title_to_cids.get(branch_key)

        if not cids:
            stats["branch_not_found"] += 1
            continue
        if len(cids) > 1:
            stats["branch_ambiguous"] += 1
            continue

        cid = cids[0]
        real_actor = dentry_actor.get((cid, n))
        if real_actor is None and (cid, n) not in dentry_actor:
            stats["dentry_not_found"] += 1
            continue

        folder_actor = actor_folder_from_rel_path(rel_path)
        if folder_actor is None:
            stats["no_actor_folder"] += 1
            continue

        if real_actor is None:
            # dentry exists but has no actor at all (group line, rare) -
            # no cross-check possible, reject rather than guess.
            stats["dentry_actor_null"] += 1
            continue

        if folder_actor != real_actor:
            stats["actor_mismatch"] += 1
            continue

        result[sound_id] = (cid, n)
        stats["matched"] += 1

    return result, stats


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <dialogue.db> <sound.db>")
        sys.exit(1)

    result, stats = match(sys.argv[1], sys.argv[2])

    print("--- matching steps ---")
    for k in ["unparseable", "branch_not_found", "branch_ambiguous", "dentry_not_found",
              "no_actor_folder", "dentry_actor_null", "actor_mismatch", "matched"]:
        print(f"{k:20s}: {stats.get(k, 0)}")
