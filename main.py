"""
elysia - a searchable database of Disco Elysium dialogue lines.

Database is the real source schema (mirrored 1:1) - see readme.md for
provenance. The working db at db/DiscoElysium.db is a copy of the
imported source file; nothing here writes to it. Grab a copy of it
yourself via GET /database/download.

Source tables used:
  actors    (id, name, ...)                      - speaker lookup
  dialogues (id, title, description, ...)         - one row per "branch"/conversation
  dentries  (conversationid, id, dialoguetext, actor, ...) - the actual lines
  dlinks    (originconversationid, origindialogueid,
             destinationconversationid, destinationdialogueid, ...) - tree edges
  checks / modifiers / alternates                 - per-line skill checks, their
                                                     modifiers, and conditional
                                                     line variants

API contract (restructured, resource-oriented - this replaced an earlier
ad hoc /search-line + /get-branch + a redundant /get-dialogue-branch alias):

  GET /characters                        - full actor list, for pickers
  GET /search/lines?q=&character=        - search dialogue lines (AND logic
                                            between q and character), each
                                            result carries its branch_id
  GET /search/branches?title=            - search branches/conversations by
                                            title (new - lets the landing
                                            page's "find a branch" mode work
                                            without already knowing an id)
  GET /branches/{id}                     - full branch: dialogue metadata +
                                            every entry (classified) + every
                                            graph edge
  GET /branches/{id}/lines/{line_id}     - a single line's full detail (new -
                                            lets a "line page" load fast
                                            without pulling the whole branch,
                                            same classification logic shared
                                            with /branches/{id} via _build_entry)

All queries are read-only and parametrized (no raw string interpolation of
user input).
"""

from collections import defaultdict, deque
from contextlib import asynccontextmanager
from pathlib import Path
import re
import shutil
import asyncio
import sqlite3
import time

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request, Response, status
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sqlalchemy import bindparam, event, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
import uvicorn
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware


def load_config() -> dict:
    """config.yaml is expected to exist (shipped in the repo) - this only
    falls back to hardcoded defaults if it's somehow missing/unreadable at
    runtime, so a broken/absent config degrades gracefully rather than
    crashing startup."""
    defaults = {
        "rate_limits": {
            "default": "20/minute",
            "database_download": "5/7 days",
            "optimizer_download": "5/7 days",
        },
        "audio_playback": {
            "per_minute_limit": 12,
            "ban_after_per_hour": 600,
            "ban_duration_seconds": 3600,
        },
        "update_check": {"enabled": True, "github_repo": "arda-y/elysia"},
        "telemetry": {"enabled": False},
    }
    try:
        with open("config.yaml", encoding="utf-8") as f:
            loaded = yaml.safe_load(f) or {}
        # shallow-merge over defaults so a partial/older config.yaml doesn't
        # crash on a missing key
        for section, values in defaults.items():
            loaded.setdefault(section, {})
            for key, value in values.items():
                loaded[section].setdefault(key, value)
        return loaded
    except (OSError, yaml.YAMLError) as e:
        print(f"WARNING: could not load config.yaml ({e}), using defaults", flush=True)
        return defaults


CONFIG = load_config()
RATE_LIMIT_DEFAULT = CONFIG["rate_limits"]["default"]
RATE_LIMIT_DOWNLOAD = CONFIG["rate_limits"]["database_download"]
RATE_LIMIT_OPTIMIZER_DOWNLOAD = CONFIG["rate_limits"]["optimizer_download"]


def get_docker_gateway_ip() -> str | None:
    """See guestbook/main.py - same trick, same reasoning: nginx reaches this
    container through the Docker bridge gateway, which can change whenever
    the compose network is recreated, so it's resolved at startup rather
    than hardcoded."""
    try:
        with open("/proc/net/route", encoding="ascii") as f:
            for line in f.readlines()[1:]:
                fields = line.strip().split()
                if len(fields) < 3:
                    continue
                if fields[1] == "00000000":
                    gateway_int = int(fields[2], 16)
                    gateway_bytes = gateway_int.to_bytes(4, byteorder="little")
                    return ".".join(str(b) for b in gateway_bytes)
    except (OSError, ValueError):
        pass
    return None


# Every live read in this app - /branches/*, /characters, /search/*, all of
# it - goes through OptimizedDE.db, a derived copy of DiscoElysium.db built
# once at startup (see ensure_optimized_db below). It adds FTS5
# trigram-tokenized indexes for /search/* (verified: identical result sets
# to the old LIKE '%q%' scan, ~80x faster on a full scan) and, just as
# importantly, is where any data-quality fix belongs (see
# _normalize_numeric_columns) - the *only* place a bad-type cell in the
# source data ever gets corrected, rather than papered over with defensive
# coercion scattered across API code. DiscoElysium.db itself is never
# written to by anything here and is read directly only by
# /database/download, so a download always hands out the exact original
# file a self-hoster substituted, malformed cells and all - protecting
# the original file, even if it's malformed, is a deliberate design call
# not an oversight.
DATABASE_URL = "sqlite+aiosqlite:///./db/OptimizedDE.db"
SEARCH_DATABASE_URL = "sqlite+aiosqlite:///./db/OptimizedDE.db"
_SOURCE_DB_PATH = Path("db/DiscoElysium.db")
_OPTIMIZED_DB_PATH = Path("db/OptimizedDE.db")


# Every column the source schema declares as a numeric/boolean type
# (INT/BOOL/REAL/INTEGER, per readme.md's schema) - SQLite doesn't
# actually enforce this, so a column can silently hold any type per-row
# regardless of what it's declared as. Audited the *entire* database (not
# just the one column that already crashed a request) via
# `SELECT typeof(col) FROM table GROUP BY typeof(col)` against every entry
# below - confirmed the only mismatch anywhere in the current dataset is
# modifiers.modifier (4 rows total: 3 on branch 1015, 1 on branch 1021 -
# category-header-style rows like "MERC TRIBUNAL" with an empty string
# instead of a real +/- value, not something main.py's own defensive
# coercion in _build_entry happened to already cover for the /branches
# read path). Kept as a full column list rather than hardcoding just that
# one case, since readme.md documents someone can substitute their own
# DiscoElysium.db here, and this same class of issue could show up
# elsewhere in a different source file.
_NUMERIC_COLUMNS = {
    "dialogues": ["id", "actor", "conversant"],
    "actors": ["id", "talkativeness"],
    "dentries": [
        "id", "actor", "conversant", "conversationid", "difficultypass",
        "isgroup", "hascheck", "hasalts",
    ],
    "dlinks": [
        "originconversationid", "origindialogueid",
        "destinationconversationid", "destinationdialogueid",
        "isConnector", "priority",
    ],
    "checks": ["conversationid", "dialogueid", "isred", "difficulty", "forced"],
    "modifiers": ["conversationid", "dialogueid", "modifier"],
    "alternates": ["conversationid", "dialogueid"],
    "variables": ["id"],
}


def _normalize_numeric_columns(conn: sqlite3.Connection) -> None:
    """Coerces any row where a numeric/boolean-declared column holds a
    non-numeric value (confirmed real, see _NUMERIC_COLUMNS's comment) to
    0 in the OptimizedDE.db copy - never touches DiscoElysium.db itself,
    so /database/download still hands out the exact untouched source
    file. NULL is left alone (a real, expected absence, not a type
    mismatch); only text/blob values in a numeric column get coerced."""
    for table, cols in _NUMERIC_COLUMNS.items():
        for col in cols:
            cur = conn.execute(
                f"UPDATE {table} SET {col} = 0 "
                f"WHERE {col} IS NOT NULL AND typeof({col}) NOT IN ('integer', 'real')"
            )
            if cur.rowcount:
                print(f"  normalized {cur.rowcount} bad-type row(s) in {table}.{col}", flush=True)


# modifiers.modifier is stored inverted relative to the player-facing
# bonus/penalty every other source (the wiki, the actual game) agrees on -
# confirmed 10-for-10 against branch 1370/11 ("Shoot Kortenaer" - Hand/Eye
# Coordination): every single modifier's sign in the source db is the
# exact opposite of what discoelysium.wiki.gg documents for it (e.g. "Got
# him talking" is +1 per the wiki, -1 here; "Just stood there?!" is -1 per
# the wiki, +1 here). Most likely explanation: the source dump captured
# whatever the internal game engine computes at the check/difficulty
# level (where a positive adjustment makes the check *harder*, i.e. worse
# for the player) rather than the player-facing roll bonus shown on
# screen (where positive always means "helping you"). Whichever the exact
# mechanism, the practical fix is the same: flip the sign so a positive
# value here always means "green, helps the player," matching what a
# person looking at this data (or the real game) would expect - a
# deliberate call, not something to leave "technically as
# extracted."
def _invert_modifier_signs(conn: sqlite3.Connection) -> None:
    cur = conn.execute("UPDATE modifiers SET modifier = -modifier WHERE modifier IS NOT NULL")
    print(f"  inverted sign on {cur.rowcount} modifiers.modifier row(s)", flush=True)


# The exact same inert Chat Mapper authoring-tool boilerplate comment
# shows up verbatim on five columns across three tables - always this
# exact literal text, never anything real between the brackets. Confirmed
# counts: dentries.title (390 rows), dentries.conditionstring (234),
# dentries.userscript (10,659), modifiers.variable (11), alternates.
# condition (10). Previously stripped at request time in _build_entry (one
# helper, five call sites) - moved here instead so the data reaching the
# API is just already clean, the same reasoning as _normalize_numeric_
# columns. RTRIM (not TRIM) matches the removed helper's old .rstrip()
# exactly - the boilerplate is always a trailing suffix, so only trailing
# whitespace needs cleaning up after removing it. NULLIF turns a
# now-fully-empty string back into a real NULL, same as the old helper's
# "or None" - a handful of these columns are otherwise-empty entries whose
# entire content WAS the boilerplate.
_BOILERPLATE_COLUMNS = [
    ("dentries", "title"),
    ("dentries", "conditionstring"),
    ("dentries", "userscript"),
    ("modifiers", "variable"),
    ("alternates", "condition"),
]
_BOILERPLATE_TEXT = "--[[ Variable[ ]]"


def _clean_boilerplate_text(conn: sqlite3.Connection) -> None:
    for table, col in _BOILERPLATE_COLUMNS:
        cur = conn.execute(
            f"UPDATE {table} SET {col} = NULLIF(RTRIM(REPLACE({col}, ?, '')), '') "
            f"WHERE {col} LIKE '%' || ? || '%'",
            (_BOILERPLATE_TEXT, _BOILERPLATE_TEXT),
        )
        if cur.rowcount:
            print(f"  stripped boilerplate from {cur.rowcount} row(s) in {table}.{col}", flush=True)


# actors.description is NOT flat text with trailing junk - it's a real
# structured Chat Mapper format: ":{quip}:{tagline}\n\n{body}", where
# quip is a short italic line, tagline an all-caps "COOL FOR: ..." line
# (which can itself contain colons - "COOL FOR: X" - so only the FIRST
# colon after the quip actually separates the two fields, everything
# after belongs to tagline), and body the actual prose paragraph(s).
# Two earlier versions of this fix, both corrected against live data the
# same session: v1 treated the whole thing as flat text with a trailing "::"
# to strip (right by accident for fully-empty rows, wrong for every real
# one - never split the fields apart, missed the ":0:0" variant of the
# same "0"-placeholder convention used everywhere else in this schema).
# v2 split the fields correctly but only ever attempted it when the
# string had a leading ':' - one actor (Perception) has the exact same
# structure with that leading colon simply missing in the source data,
# which v2 silently left unsplit. Distinguishing "real quip:tagline
# structure, just missing its leading colon" from "prose that happens to
# contain a colon as part of its OWN trailing junk" (Bed: "...you
# sleep!:0:0", no blank line anywhere) needs a second signal beyond the
# leading colon alone - a genuine blank line (the quip/tagline block is
# always separated from the body by one) - confirmed by testing against
# every one of the 421 non-empty raw rows, not assumed: zero rows where
# this reads a false split from prose-with-trailing-junk.
def _parse_actor_description(raw: str | None) -> dict | None:
    if not raw:
        return None
    starts_with_colon = raw.startswith(":")
    working = raw[1:] if starts_with_colon else raw
    head, blank_line, body_part = working.partition("\n\n")
    quip = tagline = body = None
    if (starts_with_colon or blank_line) and ":" in head:
        quip, _, tagline = head.partition(":")
        quip = quip.strip() or None
        tagline = tagline.strip() or None
        if blank_line:
            body = body_part.strip() or None
    elif starts_with_colon:
        quip = head.strip() or None
    else:
        body = raw.strip() or None

    if quip in ("0", ""):
        quip = None
    if tagline in ("0", ""):
        tagline = None
    # The trailing junk marker on the no-leading-colon/no-blank-line
    # shape - either "::" (49 rows) or ":0:0" (37 rows, same
    # "0"-placeholder convention, just at the end here instead of the
    # start) - verified against all 421 raw rows before applying: zero
    # false negatives (nothing with real content left over after
    # stripping colons/0/whitespace was ever nulled).
    if body:
        body = re.sub(r"[:0]{2,}$", "", body).strip() or None
    if not quip and not tagline and not body:
        return None
    return {"quip": quip, "tagline": tagline, "body": body}


# "Stage directions test dialogue" is a leftover developer reference
# table, not real game content - one check per raw difficulty tier index
# (0-14), purely so a writer could look up what DC each tier maps to (see
# the DIFFICULTY_TIERS comment above, which is how that mapping was
# originally confirmed). Verified unreachable from anywhere else in the
# graph: zero dlinks rows point into it from a different branch - every
# link touching it is internal to itself. Its 15 checks are real,
# correctly-typed rows though - unlike the boilerplate/type-mismatch
# cases above, this isn't malformed data, it's a curation call about what
# belongs in the cross-branch checks browser specifically. Excluding it
# via a view (checks_curated) rather than deleting the rows means its own
# branch page still shows its own check badges correctly when viewed
# directly - only the aggregate browser (/search/checks, /checks/meta)
# needs to not treat it as a real in-game check.
#
# By TITLE, not a hardcoded id - this branch's numeric id
# (1428 in this project's original Mar 2021 export) is only stable
# within one specific Chat Mapper export; a newer one (Dec 2021,
# confirmed) renumbers every branch from scratch. Resolved here, inside
# the same connection ensure_optimized_db() already has open, since this
# runs before the dynamic actor-id resolution near the top of the file
# even exists yet.
_TEST_ONLY_BRANCH_TITLES = {"Stage directions test dialogue"}


def _create_checks_curated_view(conn: sqlite3.Connection) -> None:
    placeholders = ",".join("?" * len(_TEST_ONLY_BRANCH_TITLES))
    rows = conn.execute(f"SELECT id FROM dialogues WHERE title IN ({placeholders})", tuple(_TEST_ONLY_BRANCH_TITLES)).fetchall()
    ids = ",".join(str(r[0]) for r in rows) or "-1"  # -1: no matching branch found - exclude nothing rather than error
    conn.execute(f"CREATE VIEW checks_curated AS SELECT * FROM checks WHERE conversationid NOT IN ({ids})")


# Confirmed decommissioned duplicate content, not a whole-branch issue -
# each of these branches otherwise has substantial content that's
# genuinely, legitimately reachable via its real cross-branch entry point;
# it's specifically these individual dentries that are stale authoring
# forks. Each duplicates a checks.flagname that's live and reachable
# elsewhere, and is unreachable both from its own branch's dentry 0 *and*
# from every one of its branch's real cross-branch entry points, so
# there's no natural path to any of these regardless of how you arrive:
#   627/147  dup of 627/199          (YARD / HANGED MAN BULLET, same branch)
#   280/160  dup of 553/79           (BOARDWALK / THE PIGS RED CHECK)
#   810/381  dup of 640/392          (WHIRLING F1 / RHETORIC WC)
#   822/22   dup of 537/2            (TRIBUNAL / LEGITIMACY OF THIS TRIBUNAL)
#   823/35   dup of 537/564          (TRIBUNAL / I GOT TO KNOW THE HANGED MAN)
#   850/70   dup of 381/241          (MEASUREHEAD / FASCHA DQ)
# Re-derived against a newer Chat Mapper export (Dec 2021,
# vs this project's original Mar 2021 one) whose every id was renumbered
# from scratch - matched by content (branch title + line text + actor
# name + condition), not by the old numbers, which no longer mean
# anything in this file. One of the six (the YARD/HANGED MAN BULLET pair)
# had two textually-identical candidates in the new export too - resolved
# the same way the original analysis did: the real dentry has live
# incoming links, the decommissioned one has none at all, checked
# directly, not assumed. Deleted here, before _weld_disconnected_hubs
# runs, so the weld pass naturally never sees them as something to
# reconnect.
_DECOMMISSIONED_DENTRIES = {
    (627, 147),
    (280, 160),
    (810, 381),
    (822, 22),
    (823, 35),
    (850, 70),
}


def _delete_decommissioned_dentries(conn: sqlite3.Connection) -> None:
    for cid, did in _DECOMMISSIONED_DENTRIES:
        conn.execute("DELETE FROM dentries WHERE conversationid = ? AND id = ?", (cid, did))
        conn.execute("DELETE FROM checks WHERE conversationid = ? AND dialogueid = ?", (cid, did))
        conn.execute("DELETE FROM modifiers WHERE conversationid = ? AND dialogueid = ?", (cid, did))
        conn.execute("DELETE FROM alternates WHERE conversationid = ? AND dialogueid = ?", (cid, did))
        conn.execute(
            "DELETE FROM dlinks WHERE (originconversationid = ? AND origindialogueid = ?) "
            "OR (destinationconversationid = ? AND destinationdialogueid = ?)",
            (cid, did, cid, did),
        )
    print(f"  deleted {len(_DECOMMISSIONED_DENTRIES)} decommissioned duplicate dentry(ies)", flush=True)


# 5 branches whose real narrative entry point is a check that lives in a
# DIFFERENT branch entirely - a "Jump to: [flag]" system node routes into
# them (see the DIFFICULTY_TIERS-adjacent write-up below on why this jump
# node, and the local Variable[flag] junction it lands on, are structurally
# redundant re-checks of a flag the jump already decided). Confirmed for
# each: the jump node's *only* predecessor within its own branch is
# exactly this check dentry, and the check is real and correctly typed.
#
# The system hub each of these branches has locally (e.g. 822's own
# local junction) is NOT being removed or bypassed - it's load-bearing for legitimate
# in-branch looping once you're actually inside the conversation (several
# threads reconverging on the same point without leaving the branch), and
# the real cross-branch link chain into it (check -> jump node -> this
# hub) already works correctly for anyone arriving via normal play. The
# problem is narrower: only when a branch like this is opened *cold*
# (a direct URL/search-result with no specific line pinned, or the
# "restart branch" button) should the tool say so, instead of silently
# entering through the local hub as if it were a real, unconditional
# starting point. This table backs exactly that - consulted only by
# runBranchExplorer's no-`at` path and restartBranch(), nowhere else.
# Re-derived against a newer Chat Mapper export (Dec 2021,
# vs this project's original Mar 2021 one) whose every id was renumbered
# from scratch - matched by content (branch title + line text + actor
# name + condition), not by the old numbers. All 10 resolved to exactly
# one unambiguous candidate each in the new export.
_BRANCH_TRUE_ENTRY = {
    1086: (383, 181),
    810: (640, 392),
    814: (640, 349),
    822: (537, 2),
    823: (537, 564),
    5: (29, 233),
    818: (537, 490),
    820: (537, 240),
    821: (537, 470),
    824: (537, 477),
}

# The specific local junction each of the 10 branches above would otherwise
# have gotten a synthetic same-branch weld into (the Variable[flag] node a
# jump lands on) - suppressed in _weld_disconnected_hubs since it's not
# actually unreachable, just unreachable *from this branch's own dentry
# 0*, which _BRANCH_TRUE_ENTRY now handles correctly instead.
#
# INCOMPLETE after re-derivation against the newer export - only 3 of the original
# 10 could be re-matched with confidence. Those 3 were real content lines
# with distinctive dialoguetext, matched precisely by content the same
# way as everything else in this file. The other 7 are is_group system
# nodes with empty conditionstring and a generic/placeholder actor - no
# distinguishing content at all, and confirmed NOT reachable via a direct
# cross-branch dlink from the corresponding check either (checked
# directly: 640/392's own outgoing links stay entirely within its own
# branch) - the original "jump" mechanism connecting them is evidently
# script/flag-based, not a graph edge, so it can't be traced structurally
# either. Each of those 7 branches now has MULTIPLE weld candidates in
# the new export (unlike a clean single pick), and picking the wrong one
# risks silently suppressing a legitimately-needed weld instead of the
# intended redundant one - safer to under-suppress (a minor, cosmetic
# extra option in the branch explorer for these 7 branches specifically)
# than guess wrong. Needs the same kind of manual, careful analysis the
# original 10 got, not automated re-derivation - left as a known gap
# rather than a silent wrong guess.
_SUPPRESSED_WELD_TARGETS = {
    (820, 24),  # was 1373/2 (TRIBUNAL / YOU ARE DRUNK!)
    (821, 30),  # was 1374/2 (TRIBUNAL / JOYCE WOULDN'T LIKE THIS!)
    (824, 4),   # was 1377/14 (TRIBUNAL / WHERE IS KLAASJE?)
}


def _create_true_entry_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE branch_true_entry (conversationid INTEGER PRIMARY KEY, "
        "true_entry_conversationid INTEGER NOT NULL, true_entry_dialogueid INTEGER NOT NULL)"
    )
    conn.executemany(
        "INSERT INTO branch_true_entry VALUES (?, ?, ?)",
        [(cid, tcid, tdid) for cid, (tcid, tdid) in _BRANCH_TRUE_ENTRY.items()],
    )


# dlinks has 134,491 rows across the whole db and, in the source schema,
# no index at all - /branches/{id}/lines/{id}/predecessors (what the
# branch explorer's "back" button calls, once per hop it walks backward)
# filters on exactly these two columns and was doing a full table scan
# every single call as a result. Confirmed via EXPLAIN QUERY PLAN ("SCAN
# dlinks") and measured directly: ~12ms per call unindexed vs ~0.06ms
# indexed, a ~200x difference - and "back" can need several sequential
# hops (one predecessors call + one lines call each, not parallelized) to
# walk through a chain of system/junction nodes back to the nearest real
# line, so this compounds into real, felt latency on branches with a few
# hops to cross. Not a regression - dlinks never had this index, even in
# the original DiscoElysium.db - just the right place to finally add it,
# same reasoning as every other OptimizedDE.db-layer fix here.
def _create_dlinks_index(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE INDEX idx_dlinks_destination ON dlinks(destinationconversationid, destinationdialogueid)"
    )


def _is_system_row(isgroup, actor, dialoguetext) -> bool:
    """Same is_system rule _build_entry uses (isgroup=1, or actor 0 "HUB"
    with placeholder text) - duplicated here rather than shared, since
    this runs standalone in the build step against plain sqlite3 rows,
    not the ORM row objects _build_entry works with."""
    return bool(isgroup) or (actor == 0 and dialoguetext in (None, "", "0"))


# Confirmed by walking the whole graph (not assumed): 81 branches have
# real content dentry 0 can't reach. The first version of this fix (see
# git history) only checked whether 0 reaches *any* real content at all,
# stopping its search the moment it found one - which correctly caught
# branches with zero reachable real content (e.g. 1235, where "input"
# dead-ends and the real scene starts at a totally separate dentry), but
# silently missed branches that reach *some* real content from 0 while a
# second, third, or further disconnected island of real content sits
# completely unreachable alongside it. Branch 1375 is the case that
# caught this: dentry 0 does reach one real line (a lone dead-end
# dentry), but the actual 39-line "is this tribunal legitimate" scene
# sits in its own separate connected component nothing links to. Some
# branches (e.g. 1395-1400, "barks" collections - a pile of independent
# one-liner lines with no connections between them by design) have a
# dozen or more such islands.
#
# This version finds *every* unreached island, not just detects that one
# exists: full traversal from dentry 0 (not stopping at the first real
# node), then the leftover nodes are grouped into their own weakly-
# connected components (an edge counts regardless of direction, since a
# component's own internal entry point may only have *incoming* edges
# from within that same component). Each component containing real
# content gets its own weld - a component's "local root" (a node with no
# incoming edge from another node in the same component) if one exists,
# same reasoning as the original single-target case; its lowest-id real
# dentry as a fallback for a component that's a pure cycle with no
# natural entry (e.g. 616, 700, 746, 759, 1025, 1186, 1371, 1373).
# Welding a system-node root (not just a real one) matters here too -
# branch 1375's real 39-line island's own entry point is a junction node,
# not a real line, and the existing forward walk already knows how to
# transparently skip through a system node exactly like every other
# branch's own HUB.
#
# All welds for one branch land on the same dead-end leaf reachable from
# 0 - if a branch has multiple disconnected islands, that leaf now has
# multiple outgoing edges, and the explorer presents them as a real
# choice between fragments instead of transparently continuing through
# one. That's an honest representation of what's actually in the data
# (several unconnected pieces), not an artifact of how this was fixed.
def _weld_disconnected_hubs(conn: sqlite3.Connection) -> None:
    dentries_by_branch: dict[int, dict[int, tuple]] = {}
    for cid, did, isgroup, actor, text in conn.execute(
        "SELECT conversationid, id, isgroup, actor, dialoguetext FROM dentries"
    ):
        dentries_by_branch.setdefault(cid, {})[did] = (isgroup, actor, text)

    succs_by_branch: dict[int, dict[int, list[int]]] = {}
    preds_by_branch: dict[int, dict[int, list[int]]] = {}
    for cid, origin, dest in conn.execute(
        "SELECT originconversationid, origindialogueid, destinationdialogueid FROM dlinks "
        "WHERE originconversationid = destinationconversationid"
    ):
        succs_by_branch.setdefault(cid, {}).setdefault(origin, []).append(dest)
        preds_by_branch.setdefault(cid, {}).setdefault(dest, []).append(origin)

    welds: list[tuple[int, int, int]] = []  # (branch, dead_leaf, weld_target)
    for cid, dmap in dentries_by_branch.items():
        if 0 not in dmap:
            continue
        succs = succs_by_branch.get(cid, {})
        preds = preds_by_branch.get(cid, {})
        all_ids = set(dmap.keys())
        real_ids = {did for did, row in dmap.items() if not _is_system_row(*row)}

        # full reachable set from 0 - no early exit, need everything it covers
        seen: set[int] = set()
        stack = [0]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            for s in succs.get(n, []):
                if s not in seen:
                    stack.append(s)

        unreached_real = real_ids - seen
        if not unreached_real:
            continue  # every real dentry already reachable from 0

        # Prefer a dead-end system leaf within the already-reached region -
        # welding there keeps the walk transparent for the common case (a
        # branch that otherwise dead-ends cleanly, like the original 30).
        # Not every branch has one though: 980 reaches ~99% of its real
        # content from 0 through many different paths and never dead-ends
        # into a clean system leaf at all, so its one small leftover
        # island had nowhere to attach - falls back to dentry 0 itself,
        # which always exists and is always a valid weld-from point. For
        # a branch like 980 that already presents an early multi-way
        # choice, one more option there is consistent with how it already
        # works, not a new kind of screen.
        dead_leaves = [n for n in seen if _is_system_row(*dmap[n]) and not succs.get(n)]
        leaf = min(dead_leaves) if dead_leaves else 0

        unreached = all_ids - seen

        def neighbors(n: int) -> set[int]:
            return set(succs.get(n, [])) | set(preds.get(n, []))

        visited: set[int] = set()
        for start in unreached:
            if start in visited:
                continue
            component: set[int] = set()
            comp_stack = [start]
            while comp_stack:
                x = comp_stack.pop()
                if x in visited or x not in unreached:
                    continue
                visited.add(x)
                component.add(x)
                for nb in neighbors(x):
                    if nb in unreached and nb not in visited:
                        comp_stack.append(nb)

            real_in_component = component & real_ids
            if not real_in_component:
                continue  # component is all-system, nothing real to reach here

            # Picking entry points by "zero in-component incoming edges,
            # or one arbitrary node if none" isn't reliable on its own -
            # confirmed on two different branches: 424 had *two* separate
            # zero-incoming nodes (722 and 723), and welding only the
            # first left the second unreachable; 1323 had a component
            # that looked like a single cycle (every node has an
            # in-component predecessor, so "no local root" applied) but
            # was actually a junction with three dead-end offshoots and
            # only one branch that loops back - the arbitrary fallback
            # pick landed on a dead-end offshoot, which only reaches
            # itself and exits the component immediately, missing
            # everything else including the loop.
            #
            # Greedy coverage sidesteps both failure modes without needing
            # full strongly-connected-component analysis: repeatedly pick
            # the lowest-id node not yet covered, walk forward from it
            # (within the whole graph, not just this component - if it
            # exits into already-reached territory that's harmless), mark
            # everything that walk touches as covered, and repeat until
            # nothing's left. A clean local root naturally gets picked
            # first and covers its own reachable set in one pass (no
            # different from before); a bad arbitrary pick just means the
            # next pass picks another node and covers what the first one
            # missed, so the component ends up fully covered regardless
            # of its internal cycle structure.
            covered: set[int] = set()
            while True:
                remaining = component - covered
                if not remaining:
                    break
                pick = min(remaining)
                # Don't fabricate a second, local entry into a node that's
                # already legitimately reachable through the real graph -
                # just from a different branch (see _BRANCH_TRUE_ENTRY).
                # Still counted as covered below so nothing else tries to
                # weld to it either.
                if (cid, pick) not in _SUPPRESSED_WELD_TARGETS:
                    welds.append((cid, leaf, pick))
                walk_seen: set[int] = set()
                walk_stack = [pick]
                while walk_stack:
                    x = walk_stack.pop()
                    if x in walk_seen:
                        continue
                    walk_seen.add(x)
                    for s in succs.get(x, []):
                        if s not in walk_seen:
                            walk_stack.append(s)
                covered |= walk_seen

    for cid, leaf, target in welds:
        conn.execute(
            "INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) "
            "VALUES (?, ?, ?, ?, 0, 2)",
            (cid, leaf, cid, target),
        )
    if welds:
        branches = len({cid for cid, _, _ in welds})
        print(f"  welded {len(welds)} disconnected content island(s) across {branches} branch(es) to reachable entry points", flush=True)


def ensure_optimized_db() -> None:
    """Builds db/OptimizedDE.db from db/DiscoElysium.db if
    it doesn't already exist. Deliberately NOT committed to the repo (it's
    a large, fully reproducible derived artifact, not source data) and
    deliberately rebuilt from whatever DiscoElysium.db is actually present
    rather than shipped as a static file - readme.md documents that a
    different/updated DiscoElysium.db can be substituted before starting,
    and a stale search index for someone's own data would defeat the
    point. Synchronous (plain sqlite3, not the app's async engine) since
    this runs once at import time, before anything else needs a DB
    connection - a few seconds of one-time indexing on ~111k rows."""
    if _OPTIMIZED_DB_PATH.exists():
        return
    if not _SOURCE_DB_PATH.exists():
        print(f"WARNING: {_SOURCE_DB_PATH} not found, cannot build search index", flush=True)
        return

    print(f"Building {_OPTIMIZED_DB_PATH} (FTS5 search index) - one-time, may take a few seconds...", flush=True)
    _OPTIMIZED_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(_SOURCE_DB_PATH, _OPTIMIZED_DB_PATH)
    conn = sqlite3.connect(_OPTIMIZED_DB_PATH)
    try:
        _normalize_numeric_columns(conn)
        _invert_modifier_signs(conn)
        _clean_boilerplate_text(conn)
        _create_checks_curated_view(conn)
        _create_dlinks_index(conn)
        _delete_decommissioned_dentries(conn)
        _create_true_entry_table(conn)
        _weld_disconnected_hubs(conn)
        conn.executescript(
            """
            CREATE VIRTUAL TABLE dentries_fts USING fts5(
                dialoguetext, content='dentries', content_rowid='rowid',
                tokenize='trigram case_sensitive 0'
            );
            INSERT INTO dentries_fts(rowid, dialoguetext) SELECT rowid, dialoguetext FROM dentries;

            CREATE VIRTUAL TABLE dialogues_fts USING fts5(
                title, content='dialogues', content_rowid='rowid',
                tokenize='trigram case_sensitive 0'
            );
            INSERT INTO dialogues_fts(rowid, title) SELECT rowid, title FROM dialogues WHERE title IS NOT NULL;
            """
        )
        conn.commit()
    finally:
        conn.close()
    print(f"{_OPTIMIZED_DB_PATH} build complete.", flush=True)


ensure_optimized_db()

# The 28 skill actors + "You" (the player), resolved by NAME instead of a
# hardcoded id - a numeric actor id is only stable within one specific
# Chat Mapper export. Confirmed the hard way: comparing
# against a newer export (Dec 2021 vs this project's original Mar 2021
# one) showed every single actor id renumbered from scratch, even though
# the *names* stayed identical for the base 24. That newer export also
# split the base "Perception" skill into 4 separate sense-specific actors
# (Sight/Hearing/Smell/Taste) alongside the original - 672 dentries use
# one of those 4 specifically, none of which matched this list until they
# were added here, so every one of those lines silently rendered as an
# ordinary line instead of a passive check (no error - the missing-name
# check below only guards against a name in this list NOT resolving, not
# against a real skill actor existing outside it).
_SKILL_NAMES = (
    "Conceptualization", "Logic", "Encyclopedia", "Rhetoric", "Drama", "Visual Calculus",
    "Empathy", "Inland Empire", "Volition", "Authority", "Suggestion", "Esprit de Corps",
    "Endurance", "Physical Instrument", "Shivers", "Pain Threshold", "Electrochemistry",
    "Half Light", "Hand/Eye Coordination", "Reaction Speed", "Savoir Faire", "Interfacing",
    "Composure", "Perception", "Perception (Sight)", "Perception (Hearing)",
    "Perception (Smell)", "Perception (Taste)",
)


def _resolve_key_actor_ids() -> tuple[int, set[int]]:
    conn = sqlite3.connect(_OPTIMIZED_DB_PATH)
    try:
        you_row = conn.execute("SELECT id FROM actors WHERE name = 'You'").fetchone()
        if you_row is None:
            raise RuntimeError("no actor named 'You' found in actors - is_player_choice can't be determined")
        placeholders = ",".join("?" * len(_SKILL_NAMES))
        skill_rows = conn.execute(f"SELECT id FROM actors WHERE name IN ({placeholders})", _SKILL_NAMES).fetchall()
        if len(skill_rows) != len(_SKILL_NAMES):
            found = {r[0] for r in conn.execute(f"SELECT name FROM actors WHERE name IN ({placeholders})", _SKILL_NAMES)}
            missing = set(_SKILL_NAMES) - found
            raise RuntimeError(f"expected {len(_SKILL_NAMES)} skill actors by name, missing: {missing}")
        return you_row[0], {r[0] for r in skill_rows}
    finally:
        conn.close()


YOU_ACTOR_ID, SKILL_ACTOR_IDS = _resolve_key_actor_ids()

engine = create_async_engine(DATABASE_URL, echo=False)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

search_engine = create_async_engine(SEARCH_DATABASE_URL, echo=False)
AsyncSearchSessionLocal = sessionmaker(search_engine, class_=AsyncSession, expire_on_commit=False)


# Connection-scoped PRAGMAs only - neither persists anything to either .db
# file itself (unlike e.g. journal_mode=WAL, which writes into the file
# header and creates -wal/-shm siblings). mmap_size memory-maps the file
# for faster reads instead of going through read() syscalls each time;
# cache_size raises SQLite's in-memory page cache from its ~2MB default.
def _set_sqlite_pragmas(dbapi_connection, connection_record):  # noqa: ARG001
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA mmap_size=268435456")  # 256MB
    cursor.execute("PRAGMA cache_size=-65536")  # 64MB (negative = KiB, not pages)
    cursor.close()


event.listens_for(engine.sync_engine, "connect")(_set_sqlite_pragmas)
event.listens_for(search_engine.sync_engine, "connect")(_set_sqlite_pragmas)

limiter = Limiter(key_func=get_remote_address)


TELEMETRY_URL = "https://arda0.net/elysia/telemetry"


async def send_telemetry_ping() -> None:
    """One fire-and-forget POST on startup, always this exact fixed body -
    no hostname/IP/version/anything box-identifying is added to it. Always
    targets the canonical arda0.net deployment regardless of where this
    instance is actually running (see readme.md) - that's the point, it's
    a "the project's maintainer knows another box woke up" signal, not a
    generic configurable telemetry endpoint. Any failure (offline, DNS,
    timeout, arda0.net down) never delays or breaks startup - but it is
    logged, not silently dropped, so a self-hoster who enabled this can
    actually tell whether it's working."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            await client.post(TELEMETRY_URL, json={"message": "a new box has woken up"})
    except httpx.HTTPError as e:
        print(f"WARNING: telemetry ping failed ({e}) - ignoring, startup continues", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):  # ignore: unused-argument
    # No table creation here on purpose - db/DiscoElysium.db is a
    # copy of the imported source database, not something this app builds.
    async with engine.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM dentries"))
        count = result.scalar_one()
    async with search_engine.connect() as conn:
        result = await conn.execute(text("SELECT COUNT(*) FROM dentries_fts"))
        search_count = result.scalar_one()
    print("\n" + "=" * 40, flush=True)
    print(f"DATABASE: connected, {count} dialogue entries found.", flush=True)
    print(f"SEARCH DATABASE: connected, {search_count} indexed entries found.", flush=True)
    print("=" * 40 + "\n", flush=True)

    if CONFIG["telemetry"]["enabled"]:
        await send_telemetry_ping()

    yield


app = FastAPI(lifespan=lifespan)

# Without these three lines, every @limiter.limit(...) decorator below is
# silently inert - slowapi needs the limiter registered on app.state and
# an exception handler for RateLimitExceeded, or a request that should be
# throttled just... isn't. Confirmed this was actually missing (25 rapid
# requests against a "20/minute" endpoint all returned 200) before adding it.
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)

_trusted_hosts = ["127.0.0.1"]
_gateway_ip = get_docker_gateway_ip()
if _gateway_ip:
    _trusted_hosts.append(_gateway_ip)
print(f"Trusted proxy hosts: {_trusted_hosts}", flush=True)

app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=_trusted_hosts)


@app.middleware("http")
async def add_timing_header(request: Request, call_next):
    """Adds X-Query-Time-Ms to every response - added once here rather than
    timing each endpoint individually, so it can never drift out of sync as
    endpoints are added/changed. The frontend reads this header and shows it
    next to results on every page, so what's displayed is the real backend
    processing time for that specific request, not a client-side estimate
    that would also include network latency."""
    start = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Query-Time-Ms"] = f"{(time.perf_counter() - start) * 1000:.1f}"
    return response


# Voice-line playback (v2.0.0). Public, on the normal port/domain like
# everything else - a loopback-only port (the original design) would
# have kept it safe from mass scraping too, but it also made playback
# impossible for anyone but whoever's logged into the box itself, which
# defeats the point of a feature real visitors are meant to use. Scraping
# resistance instead comes from _check_audio_rate_limit below: a
# per-minute throttle no real listener would ever notice, plus a much
# coarser per-hour tripwire that outright bans an IP for an hour once
# tripped - see config.yaml's audio_playback section for the actual
# numbers.
_VOICE_DB_PATH = Path("db/voice_archive.db")


def _load_voiced_actor_names() -> frozenset[str]:
    """Every actor name with at least one matched voice line anywhere in
    voice_archive.db - used by _build_entry to decide whether a line's
    play button should render at all, not just whether *this exact*
    dentry has audio.

    Why per-actor rather than per-dentry: "You" (the
    player) alone accounts for 23,475 real dialogue lines with zero
    voice-over - the game was never going to record VO for player
    choices - plus another ~230 lines spread across 33 minor background
    NPCs (crowd scabs, unnamed strikers, ...) who likewise have no VO at
    all. Checking "does this actor ever have a voice line" up front (one
    query, at startup) kills the button for all of those in one go,
    instead of a per-dentry existence check that would either cost an
    extra round trip per line or bloat every /branches/{id} response.
    The trade-off: an actor who mostly has VO but is missing it on one
    specific rare line still shows a button that turns out to be a dead
    click - acceptably rare, and still handled gracefully client-side
    (see playVoiceLine's 404 path)."""
    if not _VOICE_DB_PATH.exists():
        return frozenset()
    conn = sqlite3.connect(_VOICE_DB_PATH)
    try:
        return frozenset(
            row[0] for row in conn.execute("SELECT DISTINCT actor FROM voice_files WHERE matched = 1")
        )
    finally:
        conn.close()


VOICED_ACTOR_NAMES = _load_voiced_actor_names()
print(f"Voice archive: {len(VOICED_ACTOR_NAMES)} actors have at least one voiced line", flush=True)
_AUDIO_CFG = CONFIG["audio_playback"]
_AUDIO_LOG_DIR = Path("logs")
_AUDIO_REQUEST_LOG = _AUDIO_LOG_DIR / "audio_requests.log"  # pruned to the last 24h on every write
_AUDIO_BAN_LOG = _AUDIO_LOG_DIR / "audio_bans.log"  # kept forever - rare, worth a permanent record

# ip -> deque of unix timestamps, oldest first, pruned to the last hour
# on each request - the per-minute check below is just this same deque's
# last-60-seconds slice, no separate structure needed.
_audio_hits: dict[str, deque] = defaultdict(deque)
# ip -> unix timestamp the ban lifts at (absent/expired = not banned)
_audio_banned_until: dict[str, float] = {}


def _log_audio_line(path: Path, line: str) -> None:
    # Best-effort - a logging failure (e.g. the bind mount missing) should
    # never take playback down with it.
    try:
        _AUDIO_LOG_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError as e:
        print(f"WARNING: could not write to {path}: {e}", flush=True)


def _prune_request_log() -> None:
    """Keeps _AUDIO_REQUEST_LOG to roughly the last 24h - rewritten in
    place each call rather than tracked in memory, since the point of
    this file is surviving a container restart, not runtime speed (one
    audio request every few seconds at most, per _AUDIO_CFG's own
    per-minute cap)."""
    if not _AUDIO_REQUEST_LOG.exists():
        return
    cutoff = time.time() - 86400
    try:
        lines = _AUDIO_REQUEST_LOG.read_text(encoding="utf-8").splitlines()
        kept = [ln for ln in lines if _line_epoch(ln) is None or _line_epoch(ln) >= cutoff]
        if len(kept) != len(lines):
            _AUDIO_REQUEST_LOG.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
    except OSError as e:
        print(f"WARNING: could not prune {_AUDIO_REQUEST_LOG}: {e}", flush=True)


def _line_epoch(line: str) -> float | None:
    # Every logged line starts with "<epoch> ..." - see _log_audio_line
    # callers below. Malformed/foreign lines are kept rather than dropped
    # (fails safe - never silently loses a line it doesn't understand).
    try:
        return float(line.split(" ", 1)[0])
    except (ValueError, IndexError):
        return None


def _check_audio_rate_limit(ip: str) -> tuple[bool, str | None]:
    """Returns (allowed, block_reason). block_reason is None when allowed,
    else one of "banned" (already serving a ban), "just_banned" (this
    request is the one that tripped it), or "minute_limit" (ordinary
    per-minute throttle, no ban). Every attempt updates the hourly window
    regardless of outcome - a request the per-minute throttle turns away
    still counts toward the hourly ban threshold, otherwise someone could
    stay just under the per-minute cap forever without ever tripping it."""
    now = time.time()

    ban_until = _audio_banned_until.get(ip)
    if ban_until is not None:
        if now < ban_until:
            return False, "banned"
        del _audio_banned_until[ip]  # ban expired

    hits = _audio_hits[ip]
    hits.append(now)
    while hits and now - hits[0] > 3600:
        hits.popleft()

    if len(hits) > _AUDIO_CFG["ban_after_per_hour"]:
        _audio_banned_until[ip] = now + _AUDIO_CFG["ban_duration_seconds"]
        _log_audio_line(_AUDIO_BAN_LOG, f"{now:.0f} BAN ip={ip} hits_in_window={len(hits)}")
        return False, "just_banned"

    last_minute = sum(1 for t in hits if now - t <= 60)
    if last_minute > _AUDIO_CFG["per_minute_limit"]:
        return False, "minute_limit"

    return True, None


def _voice_row(branch_id: int, dentry_id: int, alt_index: int | None = None):
    if not _VOICE_DB_PATH.exists():
        return None
    # Plain sqlite3 in a thread (via asyncio.to_thread below), not the
    # async engines above - a single indexed point lookup
    # (idx_voice_branch_dentry) isn't worth a second async engine +
    # connection pool for what both DBs it'd otherwise share are already
    # serving fine.
    #
    # alt_index (frontend rewrite): an alternates-table row can
    # have its own distinct recording, separate from the parent line's -
    # voice_files.alt_index is the 0-based ordinal embedded directly in
    # the original game asset filename itself (e.g.
    # "alternative-2-...-2.fsb"), not something the (now-retired)
    # matching pipeline invented, and it's meant to line up positionally
    # with GET /branches/{id}'s own `alternates` array (built from
    # `SELECT ... FROM alternates WHERE conversationid=:cid AND
    # dialogueid=...` with no ORDER BY, i.e. natural/insertion order -
    # the same one-pass-import order the original Chat Mapper export's
    # row order would have been preserved in). Confirmed against real
    # data before wiring this up: every one of the 1,085 dentries with
    # any matched alt-audio at all has an *exact* count match between its
    # alt_index recordings and its real `alternates` rows - never a
    # dentry with e.g. 3 texts but only 2 recordings - so there's no
    # count-mismatch case to even worry about breaking the positional
    # assumption. alt_index=None keeps the original behavior (the
    # parent line's own non-alternate recording).
    conn = sqlite3.connect(_VOICE_DB_PATH)
    try:
        if alt_index is None:
            return conn.execute(
                "SELECT data FROM voice_files WHERE branch_id = ? AND dentry_id = ? AND matched = 1 AND alt_index IS NULL LIMIT 1",
                (branch_id, dentry_id),
            ).fetchone()
        return conn.execute(
            "SELECT data FROM voice_files WHERE branch_id = ? AND dentry_id = ? AND matched = 1 AND alt_index = ? LIMIT 1",
            (branch_id, dentry_id, alt_index),
        ).fetchone()
    finally:
        conn.close()


async def _serve_voice_line(request: Request, branch_id: int, dentry_id: int, alt_index: int | None):
    ip = get_remote_address(request)
    allowed, reason = _check_audio_rate_limit(ip)
    _log_audio_line(
        _AUDIO_REQUEST_LOG,
        f"{time.time():.0f} ip={ip} branch={branch_id} dentry={dentry_id} alt={alt_index if alt_index is not None else '-'} blocked={reason or '-'}",
    )
    _prune_request_log()

    if not allowed:
        if reason in ("banned", "just_banned"):
            raise HTTPException(
                status_code=429,
                detail="scraping detected - this IP is temporarily blocked for 1 hour",
            )
        raise HTTPException(status_code=429, detail="too many voice-line requests - slow down")

    row = await asyncio.to_thread(_voice_row, branch_id, dentry_id, alt_index)
    if not row:
        raise HTTPException(status_code=404, detail="no voice line for this dentry")

    return Response(
        content=row[0],
        media_type="audio/ogg",
        # This exact (branch_id, dentry_id[, alt_index]) will only ever
        # resolve to this exact audio - voice_archive.db is static,
        # rebuilt wholesale, not edited in place - so a long, immutable
        # cache is safe: once a browser has played a line, clicking it
        # again never hits this endpoint (or the per-IP counters above)
        # a second time.
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@app.get("/audio/{branch_id}/{dentry_id}")
async def get_voice_line(request: Request, branch_id: int, dentry_id: int):
    return await _serve_voice_line(request, branch_id, dentry_id, alt_index=None)


@app.get("/audio/{branch_id}/{dentry_id}/alt/{alt_index}")
async def get_voice_line_alt(request: Request, branch_id: int, dentry_id: int, alt_index: int):
    # alt_index here is the 0-based position within THIS dentry's own
    # `alternates` array, as returned by GET /branches/{id} - see
    # _voice_row's own comment for why that's expected to line up with
    # voice_files.alt_index directly, and misc/frontend-rewrite-
    # decisions.md for the count-verification behind trusting it for all
    # 1,085 matched dentries, not just the single-alternate ones.
    return await _serve_voice_line(request, branch_id, dentry_id, alt_index=alt_index)


class _NoStoreStaticFiles(StaticFiles):
    """Same no-store reasoning as root()'s index.html below - the frontend
    rewrite splits JS into real files under frontend/, and a
    plain browser cache serving a stale module after a deploy would be the
    exact same class of bug as the v1.3.3 stale-index.html one, just for a
    file that isn't even the top-level page a refresh normally re-fetches
    for free."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store"
        return response


app.mount("/frontend", _NoStoreStaticFiles(directory="frontend"), name="frontend")


@app.get("/")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def root(request: Request):
    # index.html is the one file that changes on every release but was
    # otherwise cacheable by default - a plain browser refresh could keep
    # serving a stale copy after a deploy (hit once after v1.3.1, only
    # resolved by "a rebuild" clearing whatever had cached it).
    # no-store forces every load to actually re-fetch.
    return FileResponse(
        "index.html",
        media_type="text/html",
        headers={"Cache-Control": "no-store"},
    )


_characters_cache: list[dict] | None = None  # the actors table never changes at runtime (read-only db) - fetch once


@app.get("/characters")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_characters(request: Request):
    """Backs the "pick a character from a list" filter - the frontend
    shouldn't be free-typing actor names against the db. Cached after the
    first call - this is the character dropdown's data source, hit on
    every visit to the search-lines mode, and the underlying table is
    static for the app's whole lifetime."""
    global _characters_cache
    if _characters_cache is None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT id, name FROM actors WHERE name IS NOT NULL AND name != '' ORDER BY name")
            )
            _characters_cache = [{"id": row.id, "name": row.name} for row in result.fetchall()]
    return _characters_cache


# actors.description - real character/object flavor text (421 non-empty
# raw rows, but only 117 actually meaningful once properly parsed - see
# _parse_actor_description above for why: most of the rest are the same
# "0"-placeholder convention used everywhere else in this schema, just
# wrapped in the quip/tagline structure, not literally empty text).
# Previously fetched nowhere and shown nowhere; added as structured
# hover text on speaker names + its own browsable section,
# same idea as _characters_cache. Values are the parsed {quip, tagline,
# body} dict (or None), never the raw string.
_actor_descriptions_cache: dict[int, dict] | None = None


async def _get_actor_descriptions() -> dict[int, dict]:
    global _actor_descriptions_cache
    if _actor_descriptions_cache is None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT id, description FROM actors WHERE description IS NOT NULL AND description != ''")
            )
            parsed = ((row.id, _parse_actor_description(row.description)) for row in result.fetchall())
            _actor_descriptions_cache = {actor_id: desc for actor_id, desc in parsed if desc is not None}
    return _actor_descriptions_cache


@app.get("/actors/{actor_id}")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_actor_detail(request: Request, actor_id: int):
    """One actor's name + description, for the hover tooltip and the
    actor detail/browse page - a thin, cheap lookup against the same
    cache /search/actors itself is built from."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT id, name FROM actors WHERE id = :id"), {"id": actor_id}
        )
        row = result.first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no actor with that id")
    descriptions = await _get_actor_descriptions()
    return {"id": row.id, "name": row.name, "description": descriptions.get(row.id)}


@app.get("/search/actors")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_search_actors(request: Request, q: str = "", index: int = 0):
    """Browsable actor list, same shape/pagination as /search/orbs -
    name + description, filtered by a plain substring match on either
    (q optional, empty = everyone). Actors with no name at all (a
    handful of internal-only placeholder rows) are excluded, same as
    /characters."""
    limit = 100
    async with AsyncSessionLocal() as session:
        where = "WHERE name IS NOT NULL AND name != ''"
        params: dict = {"limit": limit, "offset": index}
        if q:
            where += " AND (name LIKE :q OR description LIKE :q)"
            params["q"] = f"%{q}%"
        total_result = await session.execute(text(f"SELECT COUNT(*) AS c FROM actors {where}"), params)
        total = total_result.scalar_one()
        result = await session.execute(
            text(f"SELECT id, name, description FROM actors {where} ORDER BY name LIMIT :limit OFFSET :offset"),
            params,
        )
        rows = result.fetchall()
    return {
        "results": [{"id": r.id, "name": r.name, "description": _parse_actor_description(r.description)} for r in rows],
        "total": total,
        "index": index,
        "limit": limit,
        "has_more": index + limit < total,
    }


# variables.description - real plain-English explanations of what a raw
# Variable["x.y.z"] flag actually means (7,823 of 10,513 rows have one;
# the rest are the same "0"-means-nothing placeholder convention used
# everywhere else in this schema, not a separate cleanup pass). Same
# addition/reasoning as actors.description above.
@app.get("/variables/lookup")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_variable_lookup(request: Request, name: str):
    """Single-variable lookup by its exact name, for the hover tooltip on
    a Variable["..."] reference in a condition/effect. A query param, not
    a path segment - variable names contain dots and the occasional
    other punctuation that would need escaping in a path."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text("SELECT name, initialvalue, description FROM variables WHERE name = :name"),
            {"name": name},
        )
        row = result.first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no variable with that name")
    description = row.description if row.description not in (None, "", "0") else None
    return {"name": row.name, "initial_value": row.initialvalue, "description": description}


@app.get("/search/variables")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_search_variables(request: Request, q: str = "", index: int = 0):
    """Browsable variable list, same shape/pagination as /search/orbs -
    name + description, filtered by a plain substring match on either (q
    optional, empty = every variable with a real description - the
    ~2,690 "0"-placeholder rows are excluded by default since they'd
    otherwise dominate an unfiltered browse with rows that say nothing)."""
    limit = 100
    async with AsyncSessionLocal() as session:
        where = "WHERE description IS NOT NULL AND description NOT IN ('', '0')"
        params: dict = {"limit": limit, "offset": index}
        if q:
            where += " AND (name LIKE :q OR description LIKE :q)"
            params["q"] = f"%{q}%"
        total_result = await session.execute(text(f"SELECT COUNT(*) AS c FROM variables {where}"), params)
        total = total_result.scalar_one()
        result = await session.execute(
            text(f"SELECT name, initialvalue, description FROM variables {where} ORDER BY name LIMIT :limit OFFSET :offset"),
            params,
        )
        rows = result.fetchall()
    return {
        "results": [{"name": r.name, "initial_value": r.initialvalue, "description": r.description} for r in rows],
        "total": total,
        "index": index,
        "limit": limit,
        "has_more": index + limit < total,
    }


async def _resolve_character_id(session: AsyncSession, character: str) -> int:
    """character is matched against the actors list (id, or name -
    case-insensitively) - rejects anything not actually on that list
    rather than silently falling back to a free-text LIKE match."""
    result = await session.execute(
        text("SELECT id FROM actors WHERE LOWER(name) = LOWER(:name) OR CAST(id AS TEXT) = :name"),
        {"name": character},
    )
    row = result.first()
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"'{character}' is not a known character - see /characters for the valid list",
        )
    return row.id


PAGE_SIZE = 100  # fixed page size for every paginated (search) endpoint

MAX_SEARCH_TERMS = 10  # matches fayde's own "up to 10 words/part words" limit


def build_fts_query(raw: str) -> tuple[str | None, list[str]]:
    """Turns free-typed search text into an FTS5 query: "..."-quoted
    segments become literal phrases, remaining whitespace-separated words
    each become their own term, and every term is required (AND) rather
    than any-of - e.g. `kim "streets and sodium lights" harry` requires
    all three. Matches fayde's documented search behavior (quote for an
    exact phrase, otherwise up to N separate words) rather than the
    previous behavior, which treated the whole input as one literal
    phrase (a bare two-word query like "harry kim" - not adjacent in any
    real line - used to silently return zero results).

    Every term is wrapped in its own double quotes before reaching FTS5,
    not just the ones the user quoted - this is what makes typing FTS5
    query syntax (AND/OR/NOT/-/*) into the search box behave as literal
    text instead of being parsed as query operators, since a quoted
    phrase's contents are never re-interpreted as syntax.

    Terms under 3 characters can't go through MATCH at all: the trigram
    tokenizer (see its setup comment) can't form a single trigram out of
    fewer than 3 characters, so a bare 1-2 letter word (very common - "he",
    "to", "a", "is", ...) can *never* match anything via MATCH regardless
    of what the actual text contains. Since every term is AND'd, leaving
    one of these in the MATCH query silently zeroed out the whole thing -
    confirmed: "he needs to kill himself" returned 0 results even though
    that exact sentence exists verbatim in the db, because "he" and "to"
    alone are unmatchable there. Returned separately instead, so the
    caller can still require them via a plain LIKE - cheap since it only
    runs against whatever MATCH already narrowed down, and means a typed
    word actually constrains the results instead of being silently
    dropped from the query without telling the user.

    Returns (fts_query_or_None, short_terms) - fts_query is None only when
    there are no terms of either kind, or every term present is short (the
    caller then has to fall back to a plain table scan for those).
    """
    phrases = re.findall(r'"([^"]*)"', raw)
    remainder = re.sub(r'"[^"]*"', " ", raw)
    words = remainder.split()
    all_terms = [t.strip() for t in (phrases + words) if t.strip()][:MAX_SEARCH_TERMS]
    long_terms = [t for t in all_terms if len(t) >= 3]
    short_terms = [t for t in all_terms if len(t) < 3]
    fts_query = (
        " AND ".join('"' + t.replace('"', '""') + '"' for t in long_terms)
        if long_terms else None
    )
    return fts_query, short_terms


@app.get("/search/lines")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_search_lines(
    request: Request,
    q: str | None = None,
    character: str | None = None,
    index: int = 0,
):
    fts_query, short_terms = build_fts_query(q) if q else (None, [])
    if not fts_query and not short_terms and not character:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="provide at least one of q or character",
        )
    index = max(0, index)

    async with AsyncSearchSessionLocal() as session:
        actor_id = await _resolve_character_id(session, character) if character else None

        # AND logic: when both q and character are given, a result must
        # satisfy both, not either. Two shapes depending on whether q is
        # given at all - MATCH needs a real search term, it can't be run
        # against an empty/absent one, so a character-only search skips
        # the FTS5 join entirely rather than passing it a blank query.
        conditions = ["de.dialoguetext IS NOT NULL", "de.dialoguetext != ''", "de.dialoguetext != '0'"]
        params: dict = {"index": index, "limit": PAGE_SIZE}
        if fts_query:
            from_clause = "dentries_fts f JOIN dentries de ON de.rowid = f.rowid"
            conditions.append("f.dialoguetext MATCH :q")
            params["q"] = fts_query
        else:
            from_clause = "dentries de"
        # Short (<3 char) terms can't go through MATCH - see build_fts_query.
        # Required here via a plain LIKE instead, which only has to scan
        # whatever MATCH already narrowed down to (or the whole table, in
        # the rare case every term was short and there's no MATCH at all).
        for i, term in enumerate(short_terms):
            conditions.append(f"de.dialoguetext LIKE :short{i} ESCAPE '\\'")
            params[f"short{i}"] = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        if actor_id is not None:
            conditions.append("de.actor = :actor_id")
            params["actor_id"] = actor_id
        where_clause = " AND ".join(conditions)

        count_result = await session.execute(
            text(f"SELECT COUNT(*) FROM {from_clause} WHERE {where_clause}"), params
        )
        total = count_result.scalar_one()

        query = f"""
            SELECT de.conversationid, de.id, de.dialoguetext, a.name AS speaker
            FROM {from_clause}
            LEFT JOIN actors a ON a.id = de.actor
            WHERE {where_clause}
            ORDER BY de.conversationid, de.id
            LIMIT :limit OFFSET :index
        """
        result = await session.execute(text(query), params)
        results = [
            {
                "branch_id": row.conversationid,
                "dentry_id": row.id,
                "speaker": row.speaker,
                "text": row.dialoguetext,
            }
            for row in result.fetchall()
        ]

        return {
            "results": results,
            "total": total,
            "index": index,
            "limit": PAGE_SIZE,
            "has_more": index + len(results) < total,
        }


@app.get("/search/branches")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_search_branches(request: Request, title: str, index: int = 0):
    fts_query, short_terms = build_fts_query(title) if title.strip() else (None, [])
    if not fts_query and not short_terms:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="title must not be empty")
    index = max(0, index)

    async with AsyncSearchSessionLocal() as session:
        conditions = []
        params: dict = {"index": index, "limit": PAGE_SIZE}
        if fts_query:
            from_clause = "dialogues_fts f JOIN dialogues dl ON dl.rowid = f.rowid"
            conditions.append("f.title MATCH :title")
            params["title"] = fts_query
        else:
            from_clause = "dialogues dl"
        # Short (<3 char) terms can't go through MATCH - see build_fts_query.
        for i, term in enumerate(short_terms):
            conditions.append(f"dl.title LIKE :short{i} ESCAPE '\\'")
            params[f"short{i}"] = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        where_clause = " AND ".join(conditions)

        count_result = await session.execute(
            text(f"SELECT COUNT(*) FROM {from_clause} WHERE {where_clause}"), params
        )
        total = count_result.scalar_one()

        result = await session.execute(
            text(
                f"""
                SELECT dl.id, dl.title, dl.description
                FROM {from_clause}
                WHERE {where_clause}
                ORDER BY dl.title
                LIMIT :limit OFFSET :index
                """
            ),
            params,
        )
        results = [
            {"branch_id": row.id, "title": row.title, "description": row.description}
            for row in result.fetchall()
        ]

        return {
            "results": results,
            "total": total,
            "index": index,
            "limit": PAGE_SIZE,
            "has_more": index + len(results) < total,
        }


# "ORB" is a real category in this dataset, not something invented here -
# mostly self-contained flavor/inspection vignettes (781 of 1428 branches),
# almost always never cross-linking out to another branch (checked: only 2
# do, and even those only link to another ORB, never back into a main
# dialogue tree - there's no "returns once it's done" edge anywhere in the
# graph; that hand-off is handled by the game's own UI, not represented
# here). Matched on the real word "orb" via a regex word boundary, not a
# plain substring - a plain `LIKE '%orb%'` would wrongly catch e.g.
# "DOOMED / ELECTRONIC DOORBELL" (contains "orb" inside "doorbell").
# Since traversal needs nothing special (an ORB is just a branch, and the
# existing explorer already handles arbitrary branches with zero
# cross-branch links just fine), this only needs a way to find them - the
# branch explorer covers the rest for free.
_ORB_WORD_RE = re.compile(r"\borb\b", re.IGNORECASE)
_orb_branches_cache: list[dict] | None = None


async def _get_orb_branches() -> list[dict]:
    global _orb_branches_cache
    if _orb_branches_cache is None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(text("SELECT id, title, description FROM dialogues WHERE title IS NOT NULL"))
            orbs = [
                {"branch_id": row.id, "title": row.title, "description": row.description}
                for row in result.fetchall()
                if _ORB_WORD_RE.search(row.title)
            ]
            orbs.sort(key=lambda b: b["title"])

            # has_content: whether this branch has any real (non-system)
            # dentry - checked: 671 of 781 ORB branches don't, their entire
            # content is the description above. The frontend uses this to
            # skip offering a branch explorer that has nothing to show.
            orb_ids = [o["branch_id"] for o in orbs]
            has_content_ids: set[int] = set()
            if orb_ids:
                rows = await session.execute(
                    text(
                        f"SELECT DISTINCT conversationid FROM dentries "
                        f"WHERE conversationid IN ({','.join(str(i) for i in orb_ids)}) "
                        f"AND isgroup = 0 "
                        f"AND NOT (actor = 0 AND (dialoguetext IS NULL OR dialoguetext IN ('', '0')))"
                    )
                )
                has_content_ids = {row.conversationid for row in rows.fetchall()}
            for o in orbs:
                o["has_content"] = o["branch_id"] in has_content_ids

            _orb_branches_cache = orbs
    return _orb_branches_cache


@app.get("/search/orbs")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_search_orbs(request: Request, q: str | None = None, index: int = 0):
    """Like /search/branches, but scoped to the ORB category, and q is
    optional - a blank query browses the full list (matching fayde's
    separate "Contemplate ORBS" browsing mode), a query filters by
    title/description substring within that list."""
    index = max(0, index)
    all_orbs = await _get_orb_branches()

    if q:
        needle = q.lower()
        matching = [
            b for b in all_orbs
            if needle in b["title"].lower() or (b["description"] and needle in b["description"].lower())
        ]
    else:
        matching = all_orbs

    total = len(matching)
    page = matching[index : index + PAGE_SIZE]

    return {
        "results": page,
        "total": total,
        "index": index,
        "limit": PAGE_SIZE,
        "has_more": index + len(page) < total,
    }


# checks.difficulty is NOT the target number shown in-game (6-20) - it's a
# 0-14 internal authoring-tool tier index. Confirmed, not guessed: branch
# 1428 ("Stage directions test dialogue") is a leftover developer reference
# table with one check per tier index, whose own dialogue text literally
# spells out the real name+DC for each ("Trivial 6", "Impossible 19", ...).
# Showing the raw index (e.g. "difficulty 3") as if it were the DC is
# actively misleading - a real check at tier 3 targets 12, not 3.
DIFFICULTY_TIERS = {
    0: ("Trivial", 6), 1: ("Easy", 8), 2: ("Normal", 10), 3: ("Challenging", 12),
    4: ("Difficult", 14), 5: ("Very Difficult", 16), 6: ("Heroic", 18), 7: ("Impossible", 20),
    8: ("Easy", 7), 9: ("Normal", 9), 10: ("Challenging", 11), 11: ("Difficult", 13),
    12: ("Very Difficult", 15), 13: ("Heroic", 17), 14: ("Impossible", 19),
}

# The 24 skills themselves acting as speakers (unprompted "passive"
# commentary - Volition chiming in unbidden, Shivers narrating the city,
# etc.) - SKILL_ACTOR_IDS itself is resolved by name near the top of the
# file (see _resolve_key_actor_ids), not hardcoded here. These lines
# never carry a checks table row (verified: 0 of 14,130 skill-actor lines
# have hascheck=1) - hascheck/checks is only for player-facing checks
# initiated by picking a dialogue option. Instead dentries.difficultypass
# is set directly on ~68% of them (9,544 of 14,130) with the same 0-14 tier
# index as checks.difficulty - this is the passive trigger threshold: roughly
# "your skill needs to clear this DC for the line to fire at all", derived
# from cross-referencing branch 142's Volition/Half Light/Physical Instrument/
# Composure lines against DIFFICULTY_TIERS and finding it lines up exactly.


_VARIABLE_REF_RE = re.compile(r'Variable\["([^"]+)"\]')


async def _variable_descriptions_for_entries(session: AsyncSession, entries: list[dict]) -> dict[str, str]:
    """Every Variable["x.y.z"] name referenced anywhere in this branch's
    own condition/effect/alternate-condition text, resolved against the
    variables table's real descriptions in one batch query - the branch
    response embeds these directly (rather than a per-hover fetch) so a
    condition/effect display can show hover text with zero extra
    round-trips, same reasoning as speaker_description on each entry.
    Names with no real description (the "0"-placeholder convention, see
    /search/variables) are simply absent from the result - a hover with
    nothing to say is just not shown, not shown-and-empty."""
    names: set[str] = set()
    for e in entries:
        for text_val in (e.get("condition"), e.get("effect")):
            if text_val:
                names.update(_VARIABLE_REF_RE.findall(text_val))
        for alt in e.get("alternates", []):
            if alt.get("condition"):
                names.update(_VARIABLE_REF_RE.findall(alt["condition"]))
    if not names:
        return {}
    result = await session.execute(
        text(
            "SELECT name, description FROM variables "
            "WHERE name IN :names AND description IS NOT NULL AND description NOT IN ('', '0')"
        ).bindparams(bindparam("names", expanding=True)),
        {"names": list(names)},
    )
    return {row.name: row.description for row in result.fetchall()}


def _build_entry(row, check_row, modifier_rows, alternate_rows, actor_descriptions: dict[int, str]) -> dict:
    """Shared per-dentry classification logic - used by both /branches/{id}
    (bulk, one call per branch) and /branches/{id}/lines/{line_id} (single
    row), so the two endpoints can never quietly drift apart on what
    is_system/condition/check actually mean."""
    # A "system" entry is graph plumbing, not a real line: isgroup=1 nodes,
    # or nodes attributed to actor 0 ("HUB" - literally "null actor added
    # to allow inner joins" per its own description) with placeholder text.
    # Real dialogue never has both of these at once.
    is_system = bool(row.isgroup) or (row.actor == 0 and row.dialoguetext in (None, "", "0"))

    check = None
    if check_row is not None:
        tier_name, target = DIFFICULTY_TIERS.get(check_row.difficulty, (None, None))
        check = {
            "skill": check_row.skilltype,
            "difficulty_tier": check_row.difficulty,  # raw index, kept for reference/debugging
            "difficulty_name": tier_name,
            "difficulty_target": target,  # the actual in-game target number, or None if out of range
            "is_red": bool(check_row.isred),
            "flag": check_row.flagname,
            # sorted positive-first (descending value) so bonuses and
            # penalties group together instead of interleaving in
            # whatever order the source db happened to store them in.
            # modifier is guaranteed a real int and variable is already
            # boilerplate-free by the time it reaches this code - both
            # fixed upstream in OptimizedDE.db's build step (see
            # _normalize_numeric_columns / _clean_boilerplate_text), not
            # defensively here.
            "modifiers": [
                {
                    "variable": mod.variable,
                    "value": mod.modifier,
                    "tooltip": mod.tooltip,
                }
                for mod in sorted(modifier_rows, key=lambda m: -m.modifier)
            ],
        }

    alternates = [
        {"condition": alt.condition, "text": alt.alternateline}
        for alt in alternate_rows
    ]

    # "0" is dentries' placeholder-for-nothing everywhere else in this
    # schema (dialoguetext, sequence, ...) - conditionstring follows the
    # same convention, so it's treated as "no gate" rather than a literal
    # condition. (Leftover Chat Mapper authoring-tool boilerplate that used
    # to show up here is already stripped upstream in OptimizedDE.db's
    # build step - see _clean_boilerplate_text.)
    condition = row.conditionstring if row.conditionstring not in (None, "", "0") else None

    # userscript is a Chat Mapper Lua snippet run when this line is reached -
    # GainItem(...), SetVariableValue(...), ReputationLowers(...), and so on.
    # Previously queried nowhere and shown nowhere, so a line could grant an
    # item or flip a flag with zero trace of it in the frontend. Shown raw,
    # same as condition/modifiers - no attempt to translate the Lua.
    effect = row.userscript if row.userscript not in (None, "", "0") else None

    # A skill "speaking" unprompted (Volition chiming in, Shivers narrating
    # the city, ...) is a passive check, not a real dialogue choice - see
    # SKILL_ACTOR_IDS. It never has a checks table row (that's only for
    # player-initiated checks), but dentries.difficultypass carries the same
    # 0-14 tier index directly on the line, most of the time (9,544 of
    # 14,130 skill-actor lines) - the threshold the skill needs to clear to
    # trigger at all. Same DIFFICULTY_TIERS conversion as a real check,
    # different field to keep it visibly distinct from a player-facing one.
    passive_check = None
    if row.actor in SKILL_ACTOR_IDS and row.difficultypass:
        tier_name, target = DIFFICULTY_TIERS.get(row.difficultypass, (None, None))
        passive_check = {
            "skill": row.speaker,
            "difficulty_tier": row.difficultypass,
            "difficulty_name": tier_name,
            "difficulty_target": target,
        }

    return {
        "dentry_id": row.id,
        "title": row.title,
        "speaker": row.speaker,
        "text": row.dialoguetext,
        "is_system": is_system,
        "is_player_choice": row.actor == YOU_ACTOR_ID,
        "has_voice_actor": row.speaker in VOICED_ACTOR_NAMES,
        "condition": condition,
        "effect": effect,
        "check": check,
        "passive_check": passive_check,
        "alternates": alternates,
        "speaker_description": actor_descriptions.get(row.actor),
    }


async def _fetch_branch(conversation_id: int):
    async with AsyncSessionLocal() as session:
        dialogue_result = await session.execute(
            text("SELECT id, title, description, actor, conversant FROM dialogues WHERE id = :id"),
            {"id": conversation_id},
        )
        dialogue = dialogue_result.first()
        if dialogue is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="branch not found")

        entries_result = await session.execute(
            text(
                """
                SELECT de.id, de.title, de.dialoguetext, de.actor, a.name AS speaker,
                       de.isgroup, de.hascheck, de.hasalts, de.conditionstring, de.userscript, de.difficultypass
                FROM dentries de
                LEFT JOIN actors a ON a.id = de.actor
                WHERE de.conversationid = :cid
                ORDER BY de.id
                """
            ),
            {"cid": conversation_id},
        )
        raw_entries = entries_result.fetchall()

        # checks/alternates are sparse (235 / 2843 rows total across the
        # whole db) - fetch this branch's rows once and index by dentry_id
        # instead of a query per entry.
        checks_result = await session.execute(
            text("SELECT dialogueid, isred, difficulty, flagname, skilltype FROM checks WHERE conversationid = :cid"),
            {"cid": conversation_id},
        )
        checks_by_dentry = {row.dialogueid: row for row in checks_result.fetchall()}

        modifiers_result = await session.execute(
            text("SELECT dialogueid, variable, modifier, tooltip FROM modifiers WHERE conversationid = :cid"),
            {"cid": conversation_id},
        )
        modifiers_by_dentry: dict[int, list] = {}
        for row in modifiers_result.fetchall():
            modifiers_by_dentry.setdefault(row.dialogueid, []).append(row)

        alternates_result = await session.execute(
            text("SELECT dialogueid, condition, alternateline FROM alternates WHERE conversationid = :cid"),
            {"cid": conversation_id},
        )
        alternates_by_dentry: dict[int, list] = {}
        for row in alternates_result.fetchall():
            alternates_by_dentry.setdefault(row.dialogueid, []).append(row)

        actor_descriptions = await _get_actor_descriptions()
        entries = [
            _build_entry(
                row,
                checks_by_dentry.get(row.id),
                modifiers_by_dentry.get(row.id, []),
                alternates_by_dentry.get(row.id, []),
                actor_descriptions,
            )
            for row in raw_entries
        ]

        links_result = await session.execute(
            text(
                """
                SELECT origindialogueid, destinationconversationid, destinationdialogueid
                FROM dlinks
                WHERE originconversationid = :cid
                ORDER BY origindialogueid
                """
            ),
            {"cid": conversation_id},
        )
        links = [
            {
                "from_dentry_id": row.origindialogueid,
                "to_branch_id": row.destinationconversationid,
                "to_dentry_id": row.destinationdialogueid,
                "leaves_branch": row.destinationconversationid != conversation_id,
            }
            for row in links_result.fetchall()
        ]

        # See _BRANCH_TRUE_ENTRY in the OptimizedDE.db build step - only
        # set for the 5 branches whose real narrative entry is a check
        # living in a different branch entirely. Included so the frontend
        # can redirect a *cold* open (no specific line pinned) or a
        # "restart branch" toward the real entry instead of silently
        # walking in through the local system hub as if it were an
        # unconditional starting point - every other way of reaching this
        # branch (an explicit line, a search result, a mid-conversation
        # choice) is unaffected and still goes straight to its target.
        true_entry_result = await session.execute(
            text(
                "SELECT te.true_entry_conversationid, te.true_entry_dialogueid, "
                "dl.title AS true_entry_branch_title, de.dialoguetext AS true_entry_text "
                "FROM branch_true_entry te "
                "LEFT JOIN dialogues dl ON dl.id = te.true_entry_conversationid "
                "LEFT JOIN dentries de ON de.conversationid = te.true_entry_conversationid "
                "AND de.id = te.true_entry_dialogueid "
                "WHERE te.conversationid = :cid"
            ),
            {"cid": conversation_id},
        )
        true_entry_row = true_entry_result.first()
        true_entry = None
        if true_entry_row:
            true_entry = {
                "branch_id": true_entry_row.true_entry_conversationid,
                "dentry_id": true_entry_row.true_entry_dialogueid,
                "branch_title": true_entry_row.true_entry_branch_title,
                "text": true_entry_row.true_entry_text,
            }

        variables = await _variable_descriptions_for_entries(session, entries)

        return {
            "branch_id": dialogue.id,
            "title": dialogue.title,
            "description": dialogue.description,
            "entries": entries,
            "links": links,
            "true_entry": true_entry,
            "variables": variables,
        }


# The Lua "effect" snippet (dentries.userscript) is one or more function
# calls chained with ";" - GainItem("bullet"), DamageVolition(1),
# SetVariableValue(...), and so on. "Every instance of every effect" means
# every distinct function name used across the whole dataset, the same way
# /characters lists every distinct actor - a free-text box isn't the right
# input for something with a small, fixed, known vocabulary. "once(" and
# "not(" are excluded: they're generic Lua helpers wrapping a value/
# expression (e.g. "+ once(1)", "not(Variable[...])"), not a game effect in
# their own right - everything else found is a real effect call.
_EFFECT_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_EFFECT_DENYLIST = {"once", "not"}
_effects_cache: list[dict] | None = None


async def _get_effects() -> list[dict]:
    global _effects_cache
    if _effects_cache is None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT userscript FROM dentries WHERE userscript IS NOT NULL AND userscript != '' AND userscript != '0'")
            )
            counts: dict[str, int] = {}
            for row in result.fetchall():
                for name in set(_EFFECT_CALL_RE.findall(row.userscript)):
                    if name in _EFFECT_DENYLIST:
                        continue
                    counts[name] = counts.get(name, 0) + 1
            _effects_cache = [{"name": n, "count": c} for n, c in sorted(counts.items())]
    return _effects_cache


@app.get("/effects")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_effects(request: Request):
    """Backs the "pick an effect from a list" dropdown, same idea as
    /characters - every distinct effect function name used in
    dentries.userscript, with how many lines call it."""
    return await _get_effects()


@app.get("/search/effects")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_search_effects(request: Request, effect: str, index: int = 0):
    """Every line whose userscript calls the given effect function
    (exact name, from the /effects list - not a free-text substring)."""
    effect = effect.strip()
    if not effect:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="effect must not be empty")
    index = max(0, index)

    async with AsyncSessionLocal() as session:
        # Match "<name>(" specifically, not just the name anywhere, so
        # e.g. "GainItem" doesn't also match some other call that merely
        # mentions it as a string argument.
        like = "%" + effect.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "(%"
        conditions = "de.userscript IS NOT NULL AND de.userscript != '' AND de.userscript LIKE :effect ESCAPE '\\'"
        params = {"effect": like, "index": index, "limit": PAGE_SIZE}

        count_result = await session.execute(
            text(f"SELECT COUNT(*) FROM dentries de WHERE {conditions}"), params
        )
        total = count_result.scalar_one()

        result = await session.execute(
            text(
                f"""
                SELECT de.conversationid, de.id, de.dialoguetext, de.userscript, a.name AS speaker
                FROM dentries de
                LEFT JOIN actors a ON a.id = de.actor
                WHERE {conditions}
                ORDER BY de.conversationid, de.id
                LIMIT :limit OFFSET :index
                """
            ),
            params,
        )
        results = [
            {
                "branch_id": row.conversationid,
                "dentry_id": row.id,
                "speaker": row.speaker,
                "text": row.dialoguetext if row.dialoguetext not in (None, "", "0") else None,
                "effect": row.userscript,
            }
            for row in result.fetchall()
        ]

        return {
            "results": results,
            "total": total,
            "index": index,
            "limit": PAGE_SIZE,
            "has_more": index + len(results) < total,
        }


# Backs the checks-search filter dropdowns - distinct skills and distinct
# real in-game DC numbers actually present in the checks table, same idea
# as /characters and /effects (pick from what's really there, not free
# text). difficulty_target is the real DC (see DIFFICULTY_TIERS above),
# not the raw 0-14 tier index - that's what "skill level it requires"
# means to a player, the raw index means nothing to them. Reads from
# checks_curated (see _create_checks_curated_view), not checks directly -
# branch 1428's synthetic reference-table checks are excluded there, not
# with a per-query filter here.
_checks_meta_cache: dict | None = None


async def _get_checks_meta() -> dict:
    global _checks_meta_cache
    if _checks_meta_cache is None:
        async with AsyncSessionLocal() as session:
            result = await session.execute(
                text("SELECT DISTINCT skilltype, difficulty FROM checks_curated WHERE skilltype IS NOT NULL")
            )
            skills: set[str] = set()
            targets: set[int] = set()
            for row in result.fetchall():
                skills.add(row.skilltype)
                _, target = DIFFICULTY_TIERS.get(row.difficulty, (None, None))
                if target is not None:
                    targets.add(target)
            _checks_meta_cache = {
                "skills": sorted(skills),
                "difficulty_targets": sorted(targets),
            }
    return _checks_meta_cache


@app.get("/checks/meta")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_checks_meta(request: Request):
    return await _get_checks_meta()


@app.get("/search/checks")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_search_checks(
    request: Request,
    type: str = "all",
    skill: str | None = None,
    difficulty_target: int | None = None,
    index: int = 0,
):
    """Browse every skill check (the checks table, 235 rows total),
    filterable by RED/WHITE, skill, and the required skill level (the real
    DC, not the raw tier index) - with enough of the owning line's context
    (speaker, text preview) to be useful without opening the branch first."""
    type = type.lower()
    if type not in ("all", "red", "white"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="type must be one of: all, red, white")
    index = max(0, index)

    # difficulty_target -> the set of raw tier indices that map to it (see
    # DIFFICULTY_TIERS - two indices can share the same real DC).
    target_tiers = None
    if difficulty_target is not None:
        target_tiers = [tier for tier, (_, target) in DIFFICULTY_TIERS.items() if target == difficulty_target]
        if not target_tiers:
            return {"results": [], "total": 0, "index": index, "limit": PAGE_SIZE, "has_more": False}

    async with AsyncSessionLocal() as session:
        # checks_curated (see _create_checks_curated_view), not checks
        # directly - branch 1428's synthetic checks are excluded there.
        conditions = ["1=1"]
        params: dict = {"index": index, "limit": PAGE_SIZE}
        if type != "all":
            conditions.append("c.isred = :is_red")
            params["is_red"] = 1 if type == "red" else 0
        if skill:
            conditions.append("c.skilltype = :skill")
            params["skill"] = skill
        if target_tiers is not None:
            placeholders = ", ".join(f":tier{i}" for i in range(len(target_tiers)))
            conditions.append(f"c.difficulty IN ({placeholders})")
            for i, tier in enumerate(target_tiers):
                params[f"tier{i}"] = tier
        where_clause = " AND ".join(conditions)

        count_result = await session.execute(
            text(f"SELECT COUNT(*) FROM checks_curated c WHERE {where_clause}"), params
        )
        total = count_result.scalar_one()

        result = await session.execute(
            text(
                f"""
                SELECT c.conversationid, c.dialogueid, c.isred, c.difficulty, c.skilltype,
                       de.dialoguetext, a.name AS speaker
                FROM checks_curated c
                LEFT JOIN dentries de ON de.conversationid = c.conversationid AND de.id = c.dialogueid
                LEFT JOIN actors a ON a.id = de.actor
                WHERE {where_clause}
                ORDER BY c.conversationid, c.dialogueid
                LIMIT :limit OFFSET :index
                """
            ),
            params,
        )
        results = []
        for row in result.fetchall():
            tier_name, target = DIFFICULTY_TIERS.get(row.difficulty, (None, None))
            results.append({
                "branch_id": row.conversationid,
                "dentry_id": row.dialogueid,
                "speaker": row.speaker,
                "text": row.dialoguetext if row.dialoguetext not in (None, "", "0") else None,
                "skill": row.skilltype,
                "is_red": bool(row.isred),
                "difficulty_tier": row.difficulty,
                "difficulty_name": tier_name,
                "difficulty_target": target,
            })

        return {
            "results": results,
            "total": total,
            "index": index,
            "limit": PAGE_SIZE,
            "has_more": index + len(results) < total,
        }


@app.get("/branches/{conversation_id}")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_get_branch(request: Request, conversation_id: int):
    return await _fetch_branch(conversation_id)


@app.get("/branches/{conversation_id}/lines/{line_id}")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_get_line(request: Request, conversation_id: int, line_id: int):
    """A single line's full detail, for a lightweight "line page" that
    doesn't need to pull the whole branch (some branches have 1000+
    entries) just to show one card."""
    async with AsyncSessionLocal() as session:
        dialogue_result = await session.execute(
            text("SELECT id, title FROM dialogues WHERE id = :id"),
            {"id": conversation_id},
        )
        dialogue = dialogue_result.first()
        if dialogue is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="branch not found")

        entry_result = await session.execute(
            text(
                """
                SELECT de.id, de.title, de.dialoguetext, de.actor, a.name AS speaker,
                       de.isgroup, de.hascheck, de.hasalts, de.conditionstring, de.userscript, de.difficultypass
                FROM dentries de
                LEFT JOIN actors a ON a.id = de.actor
                WHERE de.conversationid = :cid AND de.id = :lid
                """
            ),
            {"cid": conversation_id, "lid": line_id},
        )
        row = entry_result.first()
        if row is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="line not found in this branch")

        check_result = await session.execute(
            text(
                "SELECT isred, difficulty, flagname, skilltype FROM checks "
                "WHERE conversationid = :cid AND dialogueid = :lid"
            ),
            {"cid": conversation_id, "lid": line_id},
        )
        check_row = check_result.first()

        modifier_rows = []
        if check_row is not None:
            modifiers_result = await session.execute(
                text(
                    "SELECT variable, modifier, tooltip FROM modifiers "
                    "WHERE conversationid = :cid AND dialogueid = :lid"
                ),
                {"cid": conversation_id, "lid": line_id},
            )
            modifier_rows = modifiers_result.fetchall()

        alternates_result = await session.execute(
            text(
                "SELECT condition, alternateline FROM alternates "
                "WHERE conversationid = :cid AND dialogueid = :lid"
            ),
            {"cid": conversation_id, "lid": line_id},
        )
        alternate_rows = alternates_result.fetchall()

        actor_descriptions = await _get_actor_descriptions()
        entry = _build_entry(row, check_row, modifier_rows, alternate_rows, actor_descriptions)
        entry["branch_id"] = conversation_id
        entry["branch_title"] = dialogue.title
        entry["variables"] = await _variable_descriptions_for_entries(session, [entry])
        return entry


@app.get("/branches/{conversation_id}/lines/{line_id}/predecessors")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_get_predecessors(request: Request, conversation_id: int, line_id: int):
    """Every dlinks edge that points INTO this exact (branch, line) pair,
    from anywhere in the whole database - not just within this branch.
    /branches/{id}'s own `links` only ever contains edges *out of* that
    branch, so this is the only way to walk the tree backwards, including
    across a branch boundary a forward walk crossed into. Powers the
    branch explorer's "back" button, which is graph-native (follows real
    predecessor edges) rather than a replay of clicks in this session -
    it needs to work even when the user jumped straight into the middle
    of a branch (search result, a line address, a direct URL) with no
    click history to replay at all.
    """
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            text(
                "SELECT originconversationid, origindialogueid FROM dlinks "
                "WHERE destinationconversationid = :cid AND destinationdialogueid = :lid"
            ),
            {"cid": conversation_id, "lid": line_id},
        )
        return [
            {"branch_id": row.originconversationid, "dentry_id": row.origindialogueid}
            for row in result.fetchall()
        ]


@app.post("/telemetry")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def receive_telemetry(request: Request):
    """The receiving end of send_telemetry_ping() above - since every
    self-hosted instance's ping always targets arda0.net specifically
    (not wherever it's actually running), this only does anything
    meaningful on that canonical deployment. Just logs receipt - no
    storage, no per-box tracking, nothing to correlate pings by identity
    beyond what any HTTP request already reveals at the network level
    (the source IP, which isn't read or recorded here)."""
    try:
        body = await request.json()
    except Exception:
        body = {}
    print(f"TELEMETRY: {body.get('message', '(no message)')}", flush=True)
    return {"status": "received"}


@app.get("/database/download")
@limiter.limit(RATE_LIMIT_DOWNLOAD)
async def download_database(request: Request):
    """Direct download of the original DiscoElysium.db - the exact source
    file, untouched, not the OptimizedDE.db copy every other endpoint
    actually reads from (see DATABASE_URL's comment) - nothing generated
    on the fly. Deliberately much stricter than every other endpoint here
    (5/week vs 20/minute) - it's a ~23MB file, not a cheap query."""
    return FileResponse(
        "db/DiscoElysium.db",
        media_type="application/x-sqlite3",
        filename="DiscoElysium.db",
    )


@app.get("/optimizer/download")
@limiter.limit(RATE_LIMIT_OPTIMIZER_DOWNLOAD)
async def download_optimizer_sql(request: Request):
    """Direct download of OptimizedDE.sql - see readme.md's Data
    Provenance section. Applies every fix ensure_optimized_db() applies
    at runtime (type normalization, the modifier sign fix, boilerplate
    stripping, the dlinks index, the checks_curated view, decommissioned-
    dentry removal, hub welding, FTS5 search indexes) as plain SQL
    against a copy of DiscoElysium.db - no Docker or Python needed, just
    sqlite3. Much smaller than the database itself (~80KB vs ~23MB) but
    still a real file, not a cheap query - same rate limit as the
    database download for the same reason."""
    return FileResponse(
        "misc/OptimizedDE.sql",
        media_type="application/sql",
        filename="OptimizedDE.sql",
    )


VERSION = Path("version.txt").read_text(encoding="utf-8").strip() if Path("version.txt").exists() else "unknown"


@app.get("/version.txt")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def version_txt(request: Request):
    """Plain text, matching what the same file looks like at the repo
    root on GitHub - the frontend fetches both and compares them."""
    return PlainTextResponse(VERSION)


@app.get("/app-config")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def app_config(request: Request):
    """The subset of config.yaml the frontend actually needs - rate-limit
    values are a backend-only concern and deliberately not exposed here."""
    return {
        "version": VERSION,
        "update_check_enabled": bool(CONFIG["update_check"]["enabled"]),
        "github_repo": CONFIG["update_check"]["github_repo"],
    }


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=2005, log_level="info")
