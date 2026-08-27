# elysia

A searchable database and interactive tree explorer for Disco Elysium's
dialogue, built on top of the game's own Chat Mapper export (the raw
authoring format the writers actually used - actors, dialogue trees,
skill checks, modifiers, conditional line variants, all present).

## What it does

- **Search dialogue lines** by text and/or speaking character (AND logic
  between the two), and **search branches/scenes** by title - both
  FTS5-indexed (trigram tokenizer, so ordinary mid-word substrings still
  match, not just whole words) and paginated at a fixed 100 results per
  page, with `total`/`has_more` in the response so a result set past one
  page is never silently truncated.
- **Multi-word and quoted-phrase search** - separate words are AND'd
  together in any order (`grey sky` finds lines with both), and a
  `"quoted phrase"` requires that exact sequence. Because of the trigram
  index, each word also matches as a substring wherever it occurs, so a
  loose/misspelled fragment like `reets sodium` still finds "streets and
  sodium lights."
- **Browse ORBs** - a dedicated mode that lists the game's optional
  flavor/contemplation branches on their own, rather than mixed in with
  the main dialogue search.
- **Look up an exact line or branch** directly, by id.
- **Interactive branch explorer** - walks the dialogue tree starting at a
  branch's root, automatically skipping the tree's internal
  junction/routing nodes so you only ever see real lines and real
  choices. Options that lead into a skill check show the check (skill,
  real in-game difficulty, and its modifiers) *before* you commit to
  them, not just after. Options gated by a game-state variable show that
  condition inline, rather than presenting them as if they were always
  available. A check's pass/fail branches are labeled as such wherever
  the data makes that determinable.
- **Graph-native "back"** - not a replay of your own clicks. It follows
  the tree's actual predecessor edges, so it works identically whether
  you arrived by clicking through, pasting a line address, or a search
  result - and it can walk all the way back to a branch's true root.
- **Shareable addresses** - every line and branch has a copyable,
  reloadable URL (`?view=line&branch=...&id=...` /
  `?view=branch&branch=...`).
- **Database download** - grab the exact SQLite file the app is running
  on directly from the running instance (see below).

## Data provenance

The bundled dataset is a third-party Chat Mapper export of Disco
Elysium's dialogue (a `.db` file dated 2021-03-29), not authored by this
project. It's baked into the image at `db/DiscoElysium.db` (no
volume - there's nothing here that's ever written at runtime, so nothing
needs to persist outside the container). You can:

- download the exact copy the running instance was built with, via `GET
  /database/download`, or
- substitute your own compatible SQLite file at that same repo path
  *before building the image*, if you have a different/updated export.
  The search index (`db/OptimizedDE.db`) rebuilds itself
  automatically on first startup from whatever `DiscoElysium.db` is
  actually present, so it can never go stale against a substituted file.

`OptimizedDE.db` is a derived copy that every live endpoint actually
reads from (not just search) - it's not committed (it's fully
reproducible, and shipping a ~44MB generated file alongside a ~23MB
source one is wasteful) and it's never what `/database/download` serves.
`/database/download` and nothing else always reads/serves the original
`DiscoElysium.db` untouched - the source file is never written to by
anything here.

Beyond the FTS5 search indexes, building `OptimizedDE.db` also applies
every data-quality fix this project has found in the source export, so
the original is never modified to get them: coercing a handful of
bad-type cells, un-inverting `modifiers.modifier`'s sign (confirmed
backwards relative to the real game - see `main.py`'s
`_invert_modifier_signs`), stripping leftover Chat Mapper authoring-tool
boilerplate text, indexing `dlinks` (unindexed in the source schema),
welding branches whose graph doesn't connect their own start to their
real content, and removing a handful of confirmed decommissioned
duplicate lines. See `main.py`'s `ensure_optimized_db()` for the full,
current list and the reasoning behind each one.

**`misc/OptimizedDE.sql`** (committed, ~80KB - also the exact file `GET
/optimizer/download` serves) applies the exact same fixes in one pass of
plain SQL, for anyone who wants the optimized database without running
this project at all - no Docker, no Python, just `sqlite3` (paths below
assume you've downloaded both files fresh, not a repo checkout):

```sh
cp DiscoElysium.db OptimizedDE.db
sqlite3 OptimizedDE.db < OptimizedDE.sql
```

`misc/gen_optimized_sql.py` regenerates `OptimizedDE.sql` itself from
`misc/weld_dump.json` (the baked-in graph-walk result mentioned below) -
dev tooling, not something a normal run of this project ever touches.

The graph-derived fixes (which branches got welded, which dentries got
removed) were computed once by actually walking the whole dialogue graph
in Python - since the source dataset is static, that file bakes in the
literal result of that traversal as plain `INSERT`/`DELETE` statements
rather than re-deriving it every time. If `DiscoElysium.db` is ever
replaced with a different export, this file's baked-in graph fixes won't
necessarily still apply correctly - run the app once against the new
file and let `ensure_optimized_db()` regenerate `OptimizedDE.db` from
scratch instead.

The schema is used as-is (`actors`, `dialogues`, `dentries`, `dlinks`,
`checks`, `modifiers`, `alternates`) - nothing beyond the fixes above is
transformed, everything else is only interpreted at query time (see
`main.py`'s `_build_entry` and the `DIFFICULTY_TIERS` table for one such
correction: the raw `checks.difficulty` column is an internal 0-14
authoring-tool tier index, not the in-game target number, and is
converted to the real 6-20 DC here).

## Running it

Docker Compose (recommended):

```bash
git clone <this repo> elysia
cd elysia
docker compose up -d --build
```

The app listens on `127.0.0.1:2005` by default (see `docker-compose.yml`
to change the published port). Put a reverse proxy in front of it if
you want it reachable from outside the host - it doesn't bind to
`0.0.0.0`'s public interface on its own.

Plain Docker, no compose:

```bash
docker build -t elysia .
docker run -d --name elysia -p 127.0.0.1:2005:2005 elysia
```

Without Docker (needs Python 3.11+):

```bash
pip install -r requirements.txt
python main.py
```

Shorthand `start.sh` / `stop.sh` / `restart.sh` wrap the Docker Compose
commands above, if you'd rather not type them out each time.

## Configuration

`config.yaml` (shipped with defaults, restart the container after
changing it):

- `rate_limits` - request limits per endpoint. Everything is `20/minute`
  except `/database/download`, which is `2` per 7 days (it's a ~23MB
  file, not a cheap query).
- `update_check.enabled` - on by default. The frontend compares this
  deployment's `version.txt` against the same file on the configured
  `github_repo`'s `main` branch and shows a small notice if they differ.
  Entirely client-side, entirely optional, fails silently if GitHub is
  unreachable or the repo isn't published.
- `telemetry.enabled` - **off by default**, see below.

## Telemetry (off by default)

If you turn `telemetry.enabled: true` on in `config.yaml`, this instance
sends exactly one HTTP POST to `https://arda0.net/elysia/telemetry` each
time it starts up, with this exact fixed body and nothing else:

```json
{"message": "a new box has woken up"}
```

That's the whole payload - no hostname, IP, version, config, or anything
else about your box is included in it. This always targets the
canonical arda0.net deployment specifically, regardless of where your
own instance is actually running - it exists purely as an "another
self-hosted copy is alive" signal for the project's maintainer, not as a
generic/configurable telemetry pipeline. The one thing that's
unavoidable and worth being upfront about: like any HTTP request to any
server, the receiving end can see the source IP address at the network
level - that's true of every request everywhere, not something this
project adds on top, and it isn't read, stored, or logged on the
receiving end beyond whatever the normal web server access log already
does for every visitor. If the request fails for any reason (offline,
DNS, arda0.net down), it never delays or blocks startup - but it does
print a warning to your own logs, so it's not silently unclear whether
telemetry is actually working if you've turned it on.
