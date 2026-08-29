"""match_voice_files.py - matches raw dumped voice files to dialogue database
dentries, using ONLY the filesystem-derived columns of the sound database
(rel_path/folder/filename) - no dialoguetext/actor/branch_id/dentry_id from
that side, since in the real bootstrapping problem those don't exist yet;
they're exactly what this script has to produce.

Usage: python3 match_voice_files.py <dialogue.db> <sound.db>

<dialogue.db> needs: dialogues(id, title), dentries(conversationid, id)
<sound.db> needs: voice_files(id, filename) - any other columns present
    are ignored on purpose (ground-truth columns, if present, are used
    only at the very end to report an overlap percentage - never fed into
    the matching logic itself).

Filename convention (Chat Mapper's own VO-export naming):
    {label}-{BRANCH TITLE}-{N}.fsb                      (primary line)
    alternative-{alt}-{label}-{BRANCH TITLE}-{N}-{s}.fsb (alternates variant)

`label` looks like an actor/skill name but is NOT reliable as one - it can
name the check/skill the line is voiced under rather than dentries.actor
(verified: e.g. several "Composure-GATES SCAB LEADER-*.fsb" files resolve
to dentries whose real actor is "Savoir Faire", not "Composure"). It's
ignored here entirely.

`N`, on the other hand, turned out NOT to be a fuzzy ordering hint at all -
it is a direct, literal `dentries.id` value (verified: 46,751/46,751 = 100%
of known-correct rows have filename-N == true dentry_id exactly). That
makes this a straightforward two-step lookup, not a sequence-alignment
problem: resolve the branch title to a conversationid, then look up
(conversationid, N) in dentries directly. No positional guessing needed.
"""
import re
import sqlite3
import sys
from collections import defaultdict

PRIMARY_RE = re.compile(r"^(?P<label>.+?)-(?P<branch>.+?)-(?P<n>\d+)\.fsb$", re.IGNORECASE)
ALT_RE = re.compile(r"^alternative-(?P<alt>\d+)-(?P<label>.+?)-(?P<branch>.+?)-(?P<n>\d+)-(?P<suffix>\d+)\.fsb$", re.IGNORECASE)


def normalize(s: str) -> str:
    """Collapse punctuation/whitespace differences between the DB's title
    strings ("WHIRLING F1 / GARTE MAIN") and the filename-embedded ones
    ("WHIRLING F1  GARTE MAIN") down to one comparable form."""
    s = s.upper()
    s = re.sub(r"[^A-Z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def load_dialogue_db(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    # branch title -> conversationid. A handful of titles collide once
    # normalized (rare, but real) - keep every candidate id per title
    # rather than silently picking one.
    title_to_cids = defaultdict(list)
    for cid, title in conn.execute("SELECT id, title FROM dialogues"):
        title_to_cids[normalize(title)].append(cid)

    # (conversationid, dentry id) -> True, just an existence set for the
    # direct lookup - no sequence/actor bookkeeping needed at all.
    real_dentries = set(conn.execute("SELECT conversationid, id FROM dentries"))
    return title_to_cids, real_dentries


def load_sound_db(path):
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    return conn.execute("SELECT id, filename FROM voice_files").fetchall()


def match(dialogue_db_path, sound_db_path):
    title_to_cids, real_dentries = load_dialogue_db(dialogue_db_path)
    sound_rows = load_sound_db(sound_db_path)

    result = {}            # sound_id -> (branch_id, dentry_id)
    unparseable = []        # filename didn't fit either naming convention
    branch_not_found = []   # normalized branch title has no dialogues match
    branch_ambiguous = []    # normalized branch title matches >1 conversationid
    dentry_not_found = []    # branch resolved, but (conversationid, N) doesn't exist

    for sound_id, filename in sound_rows:
        m = ALT_RE.match(filename) or PRIMARY_RE.match(filename)
        if not m:
            unparseable.append(sound_id)
            continue

        branch_key = normalize(m.group("branch"))
        n = int(m.group("n"))
        cids = title_to_cids.get(branch_key)

        if not cids:
            branch_not_found.append(sound_id)
            continue
        if len(cids) > 1:
            branch_ambiguous.append(sound_id)
            continue

        cid = cids[0]
        if (cid, n) not in real_dentries:
            dentry_not_found.append(sound_id)
            continue

        result[sound_id] = (cid, n)

    return {
        "result": result,
        "unparseable": unparseable,
        "branch_not_found": branch_not_found,
        "branch_ambiguous": branch_ambiguous,
        "dentry_not_found": dentry_not_found,
        "total_sound_files": len(sound_rows),
    }


def report_overlap(sound_db_path, matches):
    """Grading only - reads the sound db's ground-truth branch_id/dentry_id
    columns (if present) purely to score this script's output. Never used
    by match() itself."""
    conn = sqlite3.connect(f"file:{sound_db_path}?mode=ro", uri=True)
    try:
        truth = dict(
            (r[0], (r[1], r[2]))
            for r in conn.execute("SELECT id, branch_id, dentry_id FROM voice_files WHERE matched = 1")
        )
    except sqlite3.OperationalError:
        print("(no ground-truth columns available in this sound db - skipping overlap report)")
        return

    correct = 0
    checked = 0
    for sound_id, guess in matches.items():
        if sound_id in truth:
            checked += 1
            if guess == truth[sound_id]:
                correct += 1

    print(f"\n--- overlap against ground truth ---")
    print(f"sound files with a known correct answer: {len(truth)}")
    print(f"this script produced a guess for:         {checked} of those")
    print(f"guess matched ground truth exactly:        {correct} ({correct / len(truth) * 100:.1f}% of all known-correct files)")


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print(f"usage: {sys.argv[0]} <dialogue.db> <sound.db>")
        sys.exit(1)

    dialogue_db_path, sound_db_path = sys.argv[1], sys.argv[2]
    out = match(dialogue_db_path, sound_db_path)

    print("--- matching steps ---")
    print(f"1. total voice files in sound db:              {out['total_sound_files']}")
    print(f"2. filename didn't fit either naming convention: {len(out['unparseable'])}")
    print(f"3. branch title not found in dialogue db:       {len(out['branch_not_found'])}")
    print(f"4. branch title ambiguous (>1 conversationid):  {len(out['branch_ambiguous'])}")
    print(f"5. (branch, N) not a real dentry:                {len(out['dentry_not_found'])}")
    print(f"6. resolved to a real (branch, dentry) match:    {len(out['result'])}")

    report_overlap(sound_db_path, out["result"])
