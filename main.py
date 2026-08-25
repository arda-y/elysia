"""
elysia - a searchable database of Disco Elysium dialogue lines.

Database is the real source schema (mirrored 1:1) - see readme.md for
provenance. The working db at mountpoint/DiscoElysium.db is a copy of the
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

from contextlib import asynccontextmanager
from pathlib import Path
import re
import shutil
import sqlite3

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import FileResponse, PlainTextResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from slowapi.util import get_remote_address
from sqlalchemy import event, text
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
        "rate_limits": {"default": "20/minute", "database_download": "2/7 days"},
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


DATABASE_URL = "sqlite+aiosqlite:///./mountpoint/DiscoElysium.db"
# A separate copy, used only by the two /search/* endpoints, adding FTS5
# trigram-tokenized indexes over dentries.dialoguetext and dialogues.title
# (verified: identical result sets to the old LIKE '%q%' scan, ~80x faster
# on a full scan). Nothing else reads from this - /branches/*, /characters,
# and /database/download all still use the original file untouched, so a
# download always gets the exact same file this project sources from, not
# a derived/bloated copy (43MB vs 23MB - trigram indexes are large).
SEARCH_DATABASE_URL = "sqlite+aiosqlite:///./mountpoint/OptimizedDE.db"
_SOURCE_DB_PATH = Path("mountpoint/DiscoElysium.db")
_OPTIMIZED_DB_PATH = Path("mountpoint/OptimizedDE.db")


def ensure_optimized_db() -> None:
    """Builds mountpoint/OptimizedDE.db from mountpoint/DiscoElysium.db if
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
    # No table creation here on purpose - mountpoint/DiscoElysium.db is a
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


@app.get("/")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def root(request: Request):
    return FileResponse("index.html", media_type="text/html")


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


def build_fts_query(raw: str) -> str | None:
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
    """
    phrases = re.findall(r'"([^"]*)"', raw)
    remainder = re.sub(r'"[^"]*"', " ", raw)
    words = remainder.split()
    terms = [t.strip() for t in (phrases + words) if t.strip()][:MAX_SEARCH_TERMS]
    if not terms:
        return None
    return " AND ".join('"' + t.replace('"', '""') + '"' for t in terms)


@app.get("/search/lines")
@limiter.limit(RATE_LIMIT_DEFAULT)
async def api_search_lines(
    request: Request,
    q: str | None = None,
    character: str | None = None,
    index: int = 0,
):
    fts_query = build_fts_query(q) if q else None
    if not fts_query and not character:
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
    fts_query = build_fts_query(title) if title.strip() else None
    if not fts_query:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="title must not be empty")
    index = max(0, index)

    async with AsyncSearchSessionLocal() as session:
        params = {"title": fts_query, "index": index, "limit": PAGE_SIZE}

        count_result = await session.execute(
            text(
                """
                SELECT COUNT(*) FROM dialogues_fts f
                JOIN dialogues dl ON dl.rowid = f.rowid
                WHERE f.title MATCH :title
                """
            ),
            params,
        )
        total = count_result.scalar_one()

        result = await session.execute(
            text(
                """
                SELECT dl.id, dl.title, dl.description
                FROM dialogues_fts f
                JOIN dialogues dl ON dl.rowid = f.rowid
                WHERE f.title MATCH :title
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
            _orb_branches_cache = [
                {"branch_id": row.id, "title": row.title, "description": row.description}
                for row in result.fetchall()
                if _ORB_WORD_RE.search(row.title)
            ]
            _orb_branches_cache.sort(key=lambda b: b["title"])
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


def _build_entry(row, check_row, modifier_rows, alternate_rows) -> dict:
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
            # whatever order the source db happened to store them in
            "modifiers": [
                {"variable": mod.variable, "value": mod.modifier, "tooltip": mod.tooltip}
                for mod in sorted(modifier_rows, key=lambda m: -m.modifier)
            ],
        }

    alternates = [{"condition": alt.condition, "text": alt.alternateline} for alt in alternate_rows]

    # "0" is dentries' placeholder-for-nothing everywhere else in this
    # schema (dialoguetext, sequence, ...) - conditionstring follows the
    # same convention, so it's treated as "no gate" rather than a literal
    # condition.
    condition = row.conditionstring if row.conditionstring not in (None, "", "0") else None

    return {
        "dentry_id": row.id,
        "title": row.title,
        "speaker": row.speaker,
        "text": row.dialoguetext,
        "is_system": is_system,
        "is_player_choice": row.actor == 387,  # "You" - see actors table
        "condition": condition,
        "check": check,
        "alternates": alternates,
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
                       de.isgroup, de.hascheck, de.hasalts, de.conditionstring
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

        entries = [
            _build_entry(
                row,
                checks_by_dentry.get(row.id),
                modifiers_by_dentry.get(row.id, []),
                alternates_by_dentry.get(row.id, []),
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

        return {
            "branch_id": dialogue.id,
            "title": dialogue.title,
            "description": dialogue.description,
            "entries": entries,
            "links": links,
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
                       de.isgroup, de.hascheck, de.hasalts, de.conditionstring
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

        entry = _build_entry(row, check_row, modifier_rows, alternate_rows)
        entry["branch_id"] = conversation_id
        entry["branch_title"] = dialogue.title
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
    """Direct download of the working DiscoElysium.db - the same file this
    app reads from, read-only, nothing generated on the fly. Deliberately
    much stricter than every other endpoint here (2/week vs 20/minute) -
    it's a ~23MB file, not a cheap query."""
    return FileResponse(
        "mountpoint/DiscoElysium.db",
        media_type="application/x-sqlite3",
        filename="DiscoElysium.db",
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
