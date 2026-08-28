import json
import os

NUMERIC_COLUMNS = {
    "dialogues": ["id", "actor", "conversant"],
    "actors": ["id", "talkativeness"],
    "dentries": ["id", "actor", "conversant", "conversationid", "difficultypass", "isgroup", "hascheck", "hasalts"],
    "dlinks": ["originconversationid", "origindialogueid", "destinationconversationid", "destinationdialogueid", "isConnector", "priority"],
    "checks": ["conversationid", "dialogueid", "isred", "difficulty", "forced"],
    "modifiers": ["conversationid", "dialogueid", "modifier"],
    "alternates": ["conversationid", "dialogueid"],
    "variables": ["id"],
}
BOILERPLATE_COLUMNS = [
    ("dentries", "title"),
    ("dentries", "conditionstring"),
    ("dentries", "userscript"),
    ("modifiers", "variable"),
    ("alternates", "condition"),
]
BOILERPLATE_TEXT = "--[[ Variable[ ]]"
TEST_ONLY_BRANCH_IDS = [1428]
DECOMMISSIONED_DENTRIES = [
    (703, 503), (1235, 2), (1015, 2), (1375, 19), (1376, 2), (1403, 27),
]

HERE = os.path.dirname(os.path.abspath(__file__))

welds = json.load(open(os.path.join(HERE, "weld_dump.json")))

BRANCH_TRUE_ENTRY = {
    616: (605, 198),
    1015: (1002, 358),
    1025: (1002, 29),
    1375: (1370, 534),
    1376: (1370, 536),
    1186: (995, 660),
    1371: (1370, 389),
    1373: (1370, 8),
    1374: (1370, 12),
    1377: (1370, 135),
}

out = []
out.append("""-- OptimizedDE.sql
-- Applies every data-quality/derived fix elysia's runtime otherwise builds
-- on first startup (see main.py's ensure_optimized_db()) directly against
-- a copy of the original DiscoElysium.db, without needing Docker/Python/
-- the app itself running at all.
--
-- Usage:
--   cp DiscoElysium.db OptimizedDE.db
--   sqlite3 OptimizedDE.db < OptimizedDE.sql
--
-- Why this exists as a standalone file (2026-08-25):
--   1. Lets someone reproduce the optimized/search-ready database with
--      nothing but the sqlite3 CLI - no Docker, no Python, no dependency
--      install, just this file and a copy of the source .db.
--   2. The disconnected-branch welds and decommissioned-duplicate deletes
--      below were originally computed by walking the whole dialogue graph
--      in Python (see _weld_disconnected_hubs / _DECOMMISSIONED_DENTRIES
--      in main.py) - real traversal work, done once. Since DiscoElysium.db
--      is a static, unchanging dataset, there's no reason to re-walk that
--      graph every time this file runs: the exact results of that
--      traversal are baked in below as literal INSERT/DELETE statements
--      instead, so applying this file is pure O(1)-per-statement SQL, not
--      a graph search.
--
-- DiscoElysium.db itself is never modified by anything here or in the
-- app - this always targets a separate copy (OptimizedDE.db).
--
-- If DiscoElysium.db is ever replaced with a different/updated source
-- file, this script's baked-in welds and deletions may no longer apply
-- correctly (a different file has a different graph) - run the app once
-- against the new file instead and let it regenerate OptimizedDE.db from
-- scratch, then regenerate this file from that if you want an updated
-- standalone script (see main.py's build step for the live logic this
-- was derived from).

BEGIN TRANSACTION;

""")

# 1. normalize numeric columns - cherry-picked, not a general column scan.
# main.py's _normalize_numeric_columns checks every numeric/boolean-
# declared column across the whole schema, because the live app has to
# stay correct if someone substitutes their own DiscoElysium.db (see
# readme.md). This file targets a specific, known, static copy of the
# dataset instead - scanning ~30 columns to catch 4 known rows in one of
# them is exactly the kind of traversal this file exists to avoid, so
# these are the 4 exact rows, found once and confirmed (all in
# modifiers.modifier, none anywhere else - see main.py's
# _NUMERIC_COLUMNS comment for the full audit this came from).
out.append("-- 1. Coerce the 4 confirmed bad-type modifiers.modifier rows to 0.\n")
out.append("--    Cherry-picked (see main.py's _normalize_numeric_columns for the\n")
out.append("--    general version the live app uses, which stays broad in case a\n")
out.append("--    substituted source file has this issue somewhere else).\n")
BAD_MODIFIER_ROWS = [
    (1015, 2, "MERC TRIBUNAL"),
    (1015, 2, "EIGHTH HARDIE"),
    (1015, 2, "DRUG TRADE"),
    (1021, 26, "EIGHTH HARDIE"),
]
for cid, did, tooltip in BAD_MODIFIER_ROWS:
    tooltip_escaped = tooltip.replace("'", "''")
    out.append(
        f"UPDATE modifiers SET modifier = 0 WHERE conversationid = {cid} AND dialogueid = {did} "
        f"AND tooltip = '{tooltip_escaped}' AND typeof(modifier) NOT IN ('integer', 'real');\n"
    )
out.append("\n")

# 2. invert modifier signs
out.append("-- 2. modifiers.modifier is stored inverted relative to the player-facing\n")
out.append("--    bonus/penalty every other source agrees on - see main.py's\n")
out.append("--    _invert_modifier_signs for the full writeup (confirmed 10-for-10\n")
out.append("--    against discoelysium.wiki.gg on branch 1370/11).\n")
out.append("UPDATE modifiers SET modifier = -modifier WHERE modifier IS NOT NULL;\n\n")

# 3. boilerplate stripping
out.append("-- 3. Strip leftover Chat Mapper authoring-tool boilerplate text.\n")
for table, col in BOILERPLATE_COLUMNS:
    out.append(
        f"UPDATE {table} SET {col} = NULLIF(RTRIM(REPLACE({col}, '{BOILERPLATE_TEXT}', '')), '') "
        f"WHERE {col} LIKE '%' || '{BOILERPLATE_TEXT}' || '%';\n"
    )
out.append("\n")

# 4. checks_curated view
out.append("-- 4. Curated view excluding branch 1428's synthetic difficulty-tier\n")
out.append("--    reference checks from the cross-branch checks browser (the branch\n")
out.append("--    itself is untouched and still viewable directly).\n")
ids = ",".join(str(i) for i in TEST_ONLY_BRANCH_IDS)
out.append(f"CREATE VIEW checks_curated AS SELECT * FROM checks WHERE conversationid NOT IN ({ids});\n\n")

# 5. dlinks index
out.append("-- 5. dlinks (134,491 rows) has no index in the source schema at all -\n")
out.append("--    this is what /predecessors (the branch explorer's \"back\" button)\n")
out.append("--    filters on every call.\n")
out.append("CREATE INDEX idx_dlinks_destination ON dlinks(destinationconversationid, destinationdialogueid);\n\n")

# 6. decommissioned deletes (literal)
out.append("-- 6. Confirmed decommissioned duplicate dentries - each duplicates a\n")
out.append("--    checks.flagname that's live and reachable elsewhere, and is\n")
out.append("--    unreachable from both its own branch's dentry 0 and every one of\n")
out.append("--    that branch's real cross-branch entry points. See main.py's\n")
out.append("--    _DECOMMISSIONED_DENTRIES for the full per-row writeup.\n")
for cid, did in DECOMMISSIONED_DENTRIES:
    out.append(f"DELETE FROM dentries WHERE conversationid = {cid} AND id = {did};\n")
    out.append(f"DELETE FROM checks WHERE conversationid = {cid} AND dialogueid = {did};\n")
    out.append(f"DELETE FROM modifiers WHERE conversationid = {cid} AND dialogueid = {did};\n")
    out.append(f"DELETE FROM alternates WHERE conversationid = {cid} AND dialogueid = {did};\n")
    out.append(
        f"DELETE FROM dlinks WHERE (originconversationid = {cid} AND origindialogueid = {did}) "
        f"OR (destinationconversationid = {cid} AND destinationdialogueid = {did});\n"
    )
out.append("\n")

# 7. true-entry metadata table
out.append("-- 7. 10 branches whose real narrative entry is a check living in a\n")
out.append("--    different branch entirely (a redundant 'Jump to: [flag]' system\n")
out.append("--    node routes into them, re-testing a flag the jump already decided) -\n")
out.append("--    see main.py's _BRANCH_TRUE_ENTRY for the full writeup. The local\n")
out.append("--    system hub in each is untouched (still load-bearing for in-branch\n")
out.append("--    looping) - this is metadata the frontend consults only on a cold\n")
out.append("--    branch open or restart, not a graph edge.\n")
out.append(
    "CREATE TABLE branch_true_entry (conversationid INTEGER PRIMARY KEY, "
    "true_entry_conversationid INTEGER NOT NULL, true_entry_dialogueid INTEGER NOT NULL);\n"
)
for cid, (tcid, tdid) in BRANCH_TRUE_ENTRY.items():
    out.append(f"INSERT INTO branch_true_entry VALUES ({cid}, {tcid}, {tdid});\n")
out.append("\n")

# 8. welds (literal, precomputed)
out.append(f"-- 8. {len(welds)} precomputed welds reconnecting disconnected branch content\n")
out.append("--    to its nearest reachable dead-end, one dlinks row each - the exact\n")
out.append("--    output of _weld_disconnected_hubs's graph traversal, baked in as\n")
out.append("--    literal values rather than re-walked here. See main.py for the\n")
out.append("--    algorithm this was generated by.\n")
for o_cid, o_did, d_cid, d_did, is_conn, prio in welds:
    out.append(
        f"INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) "
        f"VALUES ({o_cid}, {o_did}, {d_cid}, {d_did}, {is_conn}, {prio});\n"
    )
out.append("\n")

# 8. FTS5
out.append("-- 9. FTS5 trigram search indexes over dialogue text and branch titles.\n")
out.append("""CREATE VIRTUAL TABLE dentries_fts USING fts5(
    dialoguetext, content='dentries', content_rowid='rowid',
    tokenize='trigram case_sensitive 0'
);
INSERT INTO dentries_fts(rowid, dialoguetext) SELECT rowid, dialoguetext FROM dentries;

CREATE VIRTUAL TABLE dialogues_fts USING fts5(
    title, content='dialogues', content_rowid='rowid',
    tokenize='trigram case_sensitive 0'
);
INSERT INTO dialogues_fts(rowid, title) SELECT rowid, title FROM dialogues WHERE title IS NOT NULL;
""")

out.append("\nCOMMIT;\n")

out_path = os.path.join(HERE, "OptimizedDE.sql")
with open(out_path, "w") as f:
    f.write("".join(out))

print("wrote", sum(len(x) for x in out), "chars to", out_path)
