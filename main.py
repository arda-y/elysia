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
import time

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
# file a self-hoster substituted, malformed cells and all - "protect the
# original file, even if it's malformed" was arda's explicit call
# (2026-08-25), not an oversight.
DATABASE_URL = "sqlite+aiosqlite:///./mountpoint/OptimizedDE.db"
SEARCH_DATABASE_URL = "sqlite+aiosqlite:///./mountpoint/OptimizedDE.db"
_SOURCE_DB_PATH = Path("mountpoint/DiscoElysium.db")
_OPTIMIZED_DB_PATH = Path("mountpoint/OptimizedDE.db")


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


# Branch 1428 ("Stage directions test dialogue") is a leftover developer
# reference table, not real game content - one check per raw difficulty
# tier index (0-14), purely so a writer could look up what DC each tier
# maps to (see the DIFFICULTY_TIERS comment above, which is how that
# mapping was originally confirmed). Verified unreachable from anywhere
# else in the graph: zero dlinks rows have destinationconversationid=1428
# with a different originconversationid - every link touching it is
# internal to itself. Its 15 checks are real, correctly-typed rows though -
# unlike the boilerplate/type-mismatch cases above, this isn't malformed
# data, it's a curation call about what belongs in the cross-branch checks
# browser specifically. Excluding it via a view (checks_curated) rather
# than deleting the rows means /branches/1428 itself still shows its own
# check badges correctly when viewed directly - only the aggregate browser
# (/search/checks, /checks/meta) needs to not treat it as a real in-game
# check.
_TEST_ONLY_BRANCH_IDS = {1428}


def _create_checks_curated_view(conn: sqlite3.Connection) -> None:
    ids = ",".join(str(i) for i in _TEST_ONLY_BRANCH_IDS)
    conn.execute(f"CREATE VIEW checks_curated AS SELECT * FROM checks WHERE conversationid NOT IN ({ids})")


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
        _normalize_numeric_columns(conn)
        _clean_boilerplate_text(conn)
        _create_checks_curated_view(conn)
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

# actors 389-412 are the 24 skills themselves acting as speakers (unprompted
# "passive" commentary - Volition chiming in unbidden, Shivers narrating the
# city, etc.), confirmed contiguous and complete against the actors table.
# These lines never carry a checks table row (verified: 0 of 14,130 skill-
# actor lines have hascheck=1) - hascheck/checks is only for player-facing
# checks initiated by picking a dialogue option. Instead dentries.difficultypass
# is set directly on ~68% of them (9,544 of 14,130) with the same 0-14 tier
# index as checks.difficulty - this is the passive trigger threshold: roughly
# "your skill needs to clear this DC for the line to fire at all", derived
# from cross-referencing branch 142's Volition/Half Light/Physical Instrument/
# Composure lines against DIFFICULTY_TIERS and finding it lines up exactly.
SKILL_ACTOR_IDS = set(range(389, 413))


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
        "is_player_choice": row.actor == 387,  # "You" - see actors table
        "condition": condition,
        "effect": effect,
        "check": check,
        "passive_check": passive_check,
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
            "default_entry_dentry_id": _compute_default_entry(entries, links),
        }


def _compute_default_entry(entries: list[dict], links: list[dict]) -> int:
    """Normally dentry 0 (via its is_system HUB/"input" chain) reaches the
    branch's real content - the frontend already walks/skips system nodes
    to find it, so 0 is always a safe default start. But confirmed via a
    full scan of every branch in the db (not assumed): 30 branches have a
    disconnected graph where dentry 0's own reachable component contains
    zero real (non-system) content, while the actual scene sits in a
    separate component nothing links back into 0 for. Branch 1235 is one -
    "input" (dentry 1) dead-ends with no outgoing links at all, while the
    real "Time to get my gun!" scene starts at dentry 2, which has zero
    incoming links of its own. Previously the explorer always opened at 0
    regardless, so these branches just rendered as empty/"end of visible
    branch content" with no explanation. Falls back to the lowest-id real
    dentry that has no incoming link within the branch - the same kind of
    "true root" dentry 0 already is for every normal branch - only when
    0's own component turns out to have no real content."""
    entries_by_id = {e["dentry_id"]: e for e in entries}
    if 0 not in entries_by_id:
        return 0
    succs: dict[int, list[int]] = {}
    for l in links:
        if not l["leaves_branch"]:
            succs.setdefault(l["from_dentry_id"], []).append(l["to_dentry_id"])

    def reaches_real_content(start: int) -> bool:
        seen: set[int] = set()
        stack = [start]
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n)
            e = entries_by_id.get(n)
            if e and not e["is_system"]:
                return True
            for s in succs.get(n, []):
                if s not in seen:
                    stack.append(s)
        return False

    if reaches_real_content(0):
        return 0

    has_incoming = {l["to_dentry_id"] for l in links if not l["leaves_branch"]}
    real_roots = sorted(
        did for did, e in entries_by_id.items()
        if not e["is_system"] and did not in has_incoming
    )
    return real_roots[0] if real_roots else 0


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
    """Direct download of the original DiscoElysium.db - the exact source
    file, untouched, not the OptimizedDE.db copy every other endpoint
    actually reads from (see DATABASE_URL's comment) - nothing generated
    on the fly. Deliberately much stricter than every other endpoint here
    (2/week vs 20/minute) - it's a ~23MB file, not a cheap query."""
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
