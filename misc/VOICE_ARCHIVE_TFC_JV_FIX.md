# Voice archive TFC/JV ID mismatch - root cause and fix (2026-09-10)

## Symptom

`voice_archive.db` had real `matched=1` rows with real `branch_id`/
`dentry_id` values, but almost every `/audio/{branch}/{dentry}` request
either 404'd or - worse - silently served the wrong line's audio.

## Root cause

Voice-over was recorded once, against whatever Chat-Mapper
dialogue-tool numbering existed *at recording time* - the TFC-era
structure, for the bulk of the game's lines. Each recorded line's
export filename (`{actor}-{branch title}-{N}.fsb`, where `N` is a
literal `dentries.id`) is baked in at that same authoring moment. When
Jamais Vu (JV) shipped later, it restructured/renumbered branches and
individual dentries under the hood, but did not need to re-record or
re-export already-existing VO, since the performances carried over
unchanged. Net effect: the audio *content* dumped from a JV game
install is genuinely JV, but the *filenames* embedded in those assets
are TFC-era fossils. `N` is a valid `dentries.id` only against the old
(TFC) dialogue db - never against the current JV one.

Verified concretely with one file (`Electrochemistry-INITIATION  SMOKING
HABIT-17.fsb`): under current JV `DiscoElysium.db`, this branch title
resolves to `conversationid=1071`, and dentry 17 there is an unrelated
structural placeholder with no real content. Under an older `DiscoElysium.db`
snapshot pulled from this repo's own git history (commit blob
`98655cf6598c4fff8bb6af4ce4fc9b0722f0aa9b`, tagged `v2.0.0`,
2026-08-27), the *same branch title* resolves to a different
`conversationid` (161 - branches were renumbered wholesale, not just
dentries within them), and `(161, 17)` there is exactly the right line
the file actually contains, confirmed by ear. The correct current-JV
dentry for that same line turned out to be id 64 in branch 1071 - a
47-off, non-obvious delta with no simple arithmetic or positional-rank
pattern (both were tested and disproven).

## Why two naive matching attempts failed first

1. **v1** (`match_voice_files.py`, pre-existing in this repo) resolves
   branch title -> current JV `conversationid`, then trusts `N` as a
   literal JV `dentries.id`. Produced 46,270/47,155 "matches" against
   the current JV db, but 10,508 of those (22.7%) resolved to a dentry
   whose real actor is "You" (the player line, categorically never
   voiced) - because `N` was never a valid JV lookup key at all; any
   "success" was coincidence.
2. **v2** (`match_voice_files_v2.py`, added this session) added an
   actor cross-check against the raw dump's own folder-name-as-actor
   path segment (`{location}/{actor}/{file}.fsb`). Correctly rejected
   ~80% of v1's candidates as wrong - proving the scale of the problem,
   but for the same underlying reason (wrong dialogue db), not because
   the actor cross-check itself found a new error class.

## The actual fix (v3)

`match_voice_files_v3.py` - a two-stage match, using **both** dialogue
db vintages at once:

1. **Resolve** `N` against the **old TFC-era dialogue db** (same
   branch-title + literal-`N`-as-`dentries.id` method as v1, just
   pointed at the correct db this time). Resolves 46,236/47,155 (98%) -
   confirming this is indeed the schema `N` was authored against.
2. **Bridge** old dentry -> current JV dentry by matching
   `dialoguetext` content within same-titled branches, three tiers:
   - unique text match within the branch
   - text ties broken by actor agreement
   - still-tied cases (same text+actor repeated K times in the branch,
     on both old and JV sides) resolved by **sorted-rank pairing**: the
     1st old occurrence of a repeated line maps to the 1st JV
     occurrence, 2nd to 2nd, etc. - narrative order survives the
     renumbering even when IDs don't. Verified against 15 manually
     reviewed samples from the single largest tied-case branch before
     trusting it at scale; resolved 97% of what the first two tiers
     left behind.

**Final result: 46,121/47,155 files (97.8%) correctly matched.** The
remaining 104 are genuine old/JV *occurrence-count* mismatches for a
repeated line (JV added, removed, or rewrote a repetition) - not
resolvable from data alone, left unmatched on purpose rather than
guessed. Re-run the script to regenerate that candidate list
(`ambiguous_occurrence_count_mismatch` in its stats output) for manual
review.

## How to reproduce / re-run

```
# extract the old (TFC-era) dialogue db from this repo's own git history
git cat-file -p 98655cf6598c4fff8bb6af4ce4fc9b0722f0aa9b > db/DE_dialogue_TFC.db

# the current dialogue db is just db/DiscoElysium.db, copy or symlink it
cp db/DiscoElysium.db db/DE_dialogue_JV.db

# the raw voice dump, stripped of any prior (possibly stale) match
# metadata, keeping only filename/rel_path/folder/sz/mtime/data - see
# git history of this file / elysia-project-notes memory for how that
# stripped copy was produced from a prior voice_archive.db

python3 misc/match_voice_files_v3.py db/DE_dialogue_TFC.db db/DE_dialogue_JV.db db/DE_audio_raw.db
```

Applying the result also requires two follow-up fixes the raw match
alone doesn't cover (both are data-hygiene issues, not matching-logic
issues, so they're not built into `match_voice_files_v3.py`'s output
directly - handle them in the apply step):

- `voice_files.actor` (the actor **name** column) has to be explicitly
  populated from the resolved JV dentry for every matched row -
  `main.py`'s `_load_voiced_actor_names()` reads `SELECT DISTINCT actor
  FROM voice_files WHERE matched=1` directly, not derived from
  branch/dentry. Leaving it NULL makes the frontend's "does this actor
  ever have a voice line" gate see ~1 actor total even though direct
  `/audio/{b}/{d}` requests still work fine - a silent, easy-to-miss
  breakage.
- `voice_files.alt_index` is filename-derived (the `N` in
  `alternative-{alt}-...-{n}-{s}.fsb`), not matched/derived data. If a
  "strip back to raw" pass on the dump ever nulls it along with the
  real matched columns, every `GET /audio/.../alt/{i}` alt-recording
  lookup breaks even after re-matching, since nothing else in the
  pipeline recomputes it. Re-parse it from `filename` via `ALT_RE` for
  every row, matched or not.

## Why this document exists

A previous fix for this exact problem existed once before (unknown
date, pre-2026-09-04) as a live mutation directly to `voice_archive.db`'s
columns - never committed as code anywhere. A 2026-09-04 datacenter
restore (rolled the box back to an Aug 25 snapshot) silently wiped it,
which is why the problem "resurfaced" rather than being new. This time
the method is committed to git, not just applied live, so a future
restore costs a few seconds' re-run instead of a from-scratch
re-investigation.
