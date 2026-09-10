# Elysia project status

Last updated: 2026-09-10.

## What this project is

A Disco Elysium dialogue explorer. It reads a Chat Mapper export of the
game's dialogue tree and serves it as a web app. Prod runs at
`arda0.net/elysia/`. A dev instance for a frontend rewrite runs at
`arda0.net/elysia-dev/`. arda plans to rebuild the backend later. This
file tracks project state, not the rebuild design (see
`README.md` and the elysia-project-notes memory for that).

## Backend architecture

Three SQLite files drive the app:

- `db/DiscoElysium.db` — the source dialogue tree. Small (~23MB). Baked
  into the Docker image. Never written to at runtime.
- `db/OptimizedDE.db` — a derived copy with search indexes and data
  fixes. Rebuilt on first container start if missing.
- `db/voice_archive.db` — optional voice-line audio (2.9GB). Bind-mounted
  read-only. The app works without it; it just serves no audio.

## The voice archive problem

### Root cause

Voice-over was recorded once, against an older (TFC-era) version of the
dialogue tree. Each recording's filename carries that old version's
dentry ID. The current game (JV) renumbered many branches and lines
later. Audio content is genuinely from JV — game files were dumped from
a JV install — but the filenames' IDs point at TFC-era numbers, which
mean nothing in the current database.

### Fix history and success rate at each step

All counts are "files correctly matched to a (branch, dentry) pair in
the current JV database" out of 47,155 total voice files.

| Version | Method | Matched | Rate | Notes |
|---|---|---|---|---|
| v1 | Branch title + literal N as a JV dentry ID | 46,270 | ~0% correct | N is not a valid JV lookup key at all. Every "match" was coincidence. Confirmed by later actor-verification: 22.7% resolved to a "You" (player) dentry, which is never voiced. |
| v2 | v1 + actor cross-check using the raw dump's folder name | 9,399 | proved the problem, fixed nothing | Correctly rejected ~80% of v1 as wrong. Useful diagnostic, not a fix — still resolving against the wrong (JV) dialogue DB. |
| v3 | Resolve against an OLD (TFC-era) DB pulled from this repo's own git history, then bridge to JV by matching dialogue text within the same-titled branch | 46,121 | 97.8% (unverified) | The real fix, but tier 1 (unique text match) never checked actor agreement. |
| v4 | v3 + actor cross-check at every tier, not just when a text was ambiguous | 44,887 | 95.2% (verified) | Found and rejected 1,234 of v3's matches as genuinely wrong (two different lines sharing identical text). Lower count than v3, but every match is now actor-checked. |
| v5 | v4 + fixed a filename-parsing bug | 45,501 | 96.5% | Actor names containing a hyphen (e.g. "Mega Rich Light-Bending Guy") broke the label/branch split. Fixed by anchoring the parse on the raw dump's own folder name instead of guessing from the first hyphen. Recovered 614 files. |
| v6 | v5 + a same-number/same-actor fallback tier, + fixed a second parsing bug | 45,807 | 97.1% | New tier: when text matching fails, check whether a JV dentry exists at the file's own old ID, same branch, same actor. Real signal, partial coverage (resolved 272). Also fixed 16 files with a literal "fixed-" prefix in the filename that broke the same anchor parse. |
| v7 | v6 + trusting the OLD database's own actor tag when the raw dump's folder name disagrees | 46,205 | 98.0% | The raw dump's folder name is sometimes itself wrong. When it disagrees with the OLD db's actor tag, and the OLD actor uniquely matches a JV candidate, trust the OLD actor. Recovered 385 files — the single biggest jump from one formula. |
| v8 | v7 + resolving genuinely-new-JV-only branches directly against JV | 46,472 | 98.5% | 269 files belong to branches that never existed in the OLD database at all (real new content, not a bug). Since they never touch the OLD pipeline, matching them straight against JV carries no collision risk. Recovered 267/268 (99.6% hit rate) this way. |

Plus roughly 40 files resolved by hand this session (listen to the
audio, search the dialogue text, confirm the branch/dentry) — mostly
cases where automated matching found no candidate at all, not cases a
formula could reach.

### Current state (as of this file)

- **46,472 / 47,155 matched (98.5%)**
- **683 unmatched**, broken down:
  - ~344 — a JV dentry with the expected text exists, but is spoken by
    the wrong actor, and neither actor source (folder name, OLD db tag)
    gives real signal — the OLD db's own tag is a structural placeholder
    (`HUB` or `You`, the player, which is never voiced). This is the
    real floor: no further formula found yet, needs individual review.
  - ~258 — same shape, but even the OLD actor tag is itself ambiguous
    across multiple candidates.
  - ~52 — actor-filtered candidates still tied with no tiebreak.
  - ~16 — a repeated line where the old and JV occurrence counts don't
    line up (a real added/removed repetition, not resolvable from data
    alone).
  - ~13 — small residual categories (old dentry ID out of range, no
    text match anywhere in the JV branch, ambiguous branch title).

### Where the matching code lives

`misc/match_voice_files_v1.py` through `v8.py`, in this repo. Each file's
own docstring explains what changed and why, with the scale-tested
numbers behind the decision — not just what the fix does, but how much
it was checked before being trusted. `misc/VOICE_ARCHIVE_TFC_JV_FIX.md`
has the detailed narrative for the v3/v4 root-cause discovery.

Two review tools exist in `frontend/` for manual work on what automated
matching can't reach: `voice-casefile.html`, `stuck-casefile.html`,
`actor-mismatch-sample.html`, `missing-dentry-casefile.html`, and
`category-casefile.html` — self-contained pages with embedded audio,
verdict buttons, and an export-to-text button for reporting findings
back.

### Lesson learned this session

Early manual review (listen to a file, search for its text, apply one
fix) found real problems but didn't scale — one round of 22 hand-picked
fixes recovered 22 files. The next two rounds instead derived and
scale-tested a formula from the same kind of finding before applying
it broadly — 385 files (v7) and 267 files (v8) from two formulas found
the same way. Formula-derivation beat cherry-picking by roughly 15-17x
for the same amount of investigative effort. Worth defaulting to
"can this generalize?" before applying an individual fix, not after.

## Frontend rewrite status

A separate effort, landing on `elysia-dev` only. See
`elysia-project-notes` memory and
`/home/arda/projects/elysia-notes/frontend-rewrite-decisions.md` (not
part of this repo) for that history — out of scope for this file.
