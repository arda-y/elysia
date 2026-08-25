-- OptimizedDE.sql
-- Applies every data-quality/derived fix elysia's runtime otherwise builds
-- on first startup (see main.py's ensure_optimized_db()) directly against
-- a copy of the original DiscoElysium.db, without needing Docker/Python/
-- the app itself running at all.
--
-- Usage:
--   cp DiscoElysium.db OptimizedDE.db
--   sqlite3 OptimizedDE.db < OptimizedDE.sql
--
-- Why this exists as a standalone file (arda, 2026-08-25):
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

-- 1. Coerce the 4 confirmed bad-type modifiers.modifier rows to 0.
--    Cherry-picked (see main.py's _normalize_numeric_columns for the
--    general version the live app uses, which stays broad in case a
--    substituted source file has this issue somewhere else).
UPDATE modifiers SET modifier = 0 WHERE conversationid = 1015 AND dialogueid = 2 AND tooltip = 'MERC TRIBUNAL' AND typeof(modifier) NOT IN ('integer', 'real');
UPDATE modifiers SET modifier = 0 WHERE conversationid = 1015 AND dialogueid = 2 AND tooltip = 'EIGHTH HARDIE' AND typeof(modifier) NOT IN ('integer', 'real');
UPDATE modifiers SET modifier = 0 WHERE conversationid = 1015 AND dialogueid = 2 AND tooltip = 'DRUG TRADE' AND typeof(modifier) NOT IN ('integer', 'real');
UPDATE modifiers SET modifier = 0 WHERE conversationid = 1021 AND dialogueid = 26 AND tooltip = 'EIGHTH HARDIE' AND typeof(modifier) NOT IN ('integer', 'real');

-- 2. modifiers.modifier is stored inverted relative to the player-facing
--    bonus/penalty every other source agrees on - see main.py's
--    _invert_modifier_signs for the full writeup (confirmed 10-for-10
--    against discoelysium.wiki.gg on branch 1370/11).
UPDATE modifiers SET modifier = -modifier WHERE modifier IS NOT NULL;

-- 3. Strip leftover Chat Mapper authoring-tool boilerplate text.
UPDATE dentries SET title = NULLIF(RTRIM(REPLACE(title, '--[[ Variable[ ]]', '')), '') WHERE title LIKE '%' || '--[[ Variable[ ]]' || '%';
UPDATE dentries SET conditionstring = NULLIF(RTRIM(REPLACE(conditionstring, '--[[ Variable[ ]]', '')), '') WHERE conditionstring LIKE '%' || '--[[ Variable[ ]]' || '%';
UPDATE dentries SET userscript = NULLIF(RTRIM(REPLACE(userscript, '--[[ Variable[ ]]', '')), '') WHERE userscript LIKE '%' || '--[[ Variable[ ]]' || '%';
UPDATE modifiers SET variable = NULLIF(RTRIM(REPLACE(variable, '--[[ Variable[ ]]', '')), '') WHERE variable LIKE '%' || '--[[ Variable[ ]]' || '%';
UPDATE alternates SET condition = NULLIF(RTRIM(REPLACE(condition, '--[[ Variable[ ]]', '')), '') WHERE condition LIKE '%' || '--[[ Variable[ ]]' || '%';

-- 4. Curated view excluding branch 1428's synthetic difficulty-tier
--    reference checks from the cross-branch checks browser (the branch
--    itself is untouched and still viewable directly).
CREATE VIEW checks_curated AS SELECT * FROM checks WHERE conversationid NOT IN (1428);

-- 5. dlinks (134,491 rows) has no index in the source schema at all -
--    this is what /predecessors (the branch explorer's "back" button)
--    filters on every call.
CREATE INDEX idx_dlinks_destination ON dlinks(destinationconversationid, destinationdialogueid);

-- 6. Confirmed decommissioned duplicate dentries - each duplicates a
--    checks.flagname that's live and reachable elsewhere, and is
--    unreachable from both its own branch's dentry 0 and every one of
--    that branch's real cross-branch entry points. See main.py's
--    _DECOMMISSIONED_DENTRIES for the full per-row writeup.
DELETE FROM dentries WHERE conversationid = 703 AND id = 503;
DELETE FROM checks WHERE conversationid = 703 AND dialogueid = 503;
DELETE FROM modifiers WHERE conversationid = 703 AND dialogueid = 503;
DELETE FROM alternates WHERE conversationid = 703 AND dialogueid = 503;
DELETE FROM dlinks WHERE (originconversationid = 703 AND origindialogueid = 503) OR (destinationconversationid = 703 AND destinationdialogueid = 503);
DELETE FROM dentries WHERE conversationid = 1235 AND id = 2;
DELETE FROM checks WHERE conversationid = 1235 AND dialogueid = 2;
DELETE FROM modifiers WHERE conversationid = 1235 AND dialogueid = 2;
DELETE FROM alternates WHERE conversationid = 1235 AND dialogueid = 2;
DELETE FROM dlinks WHERE (originconversationid = 1235 AND origindialogueid = 2) OR (destinationconversationid = 1235 AND destinationdialogueid = 2);
DELETE FROM dentries WHERE conversationid = 1015 AND id = 2;
DELETE FROM checks WHERE conversationid = 1015 AND dialogueid = 2;
DELETE FROM modifiers WHERE conversationid = 1015 AND dialogueid = 2;
DELETE FROM alternates WHERE conversationid = 1015 AND dialogueid = 2;
DELETE FROM dlinks WHERE (originconversationid = 1015 AND origindialogueid = 2) OR (destinationconversationid = 1015 AND destinationdialogueid = 2);
DELETE FROM dentries WHERE conversationid = 1375 AND id = 19;
DELETE FROM checks WHERE conversationid = 1375 AND dialogueid = 19;
DELETE FROM modifiers WHERE conversationid = 1375 AND dialogueid = 19;
DELETE FROM alternates WHERE conversationid = 1375 AND dialogueid = 19;
DELETE FROM dlinks WHERE (originconversationid = 1375 AND origindialogueid = 19) OR (destinationconversationid = 1375 AND destinationdialogueid = 19);
DELETE FROM dentries WHERE conversationid = 1376 AND id = 2;
DELETE FROM checks WHERE conversationid = 1376 AND dialogueid = 2;
DELETE FROM modifiers WHERE conversationid = 1376 AND dialogueid = 2;
DELETE FROM alternates WHERE conversationid = 1376 AND dialogueid = 2;
DELETE FROM dlinks WHERE (originconversationid = 1376 AND origindialogueid = 2) OR (destinationconversationid = 1376 AND destinationdialogueid = 2);
DELETE FROM dentries WHERE conversationid = 1403 AND id = 27;
DELETE FROM checks WHERE conversationid = 1403 AND dialogueid = 27;
DELETE FROM modifiers WHERE conversationid = 1403 AND dialogueid = 27;
DELETE FROM alternates WHERE conversationid = 1403 AND dialogueid = 27;
DELETE FROM dlinks WHERE (originconversationid = 1403 AND origindialogueid = 27) OR (destinationconversationid = 1403 AND destinationdialogueid = 27);

-- 7. 411 precomputed welds reconnecting disconnected branch content
--    to its nearest reachable dead-end, one dlinks row each - the exact
--    output of _weld_disconnected_hubs's graph traversal, baked in as
--    literal values rather than re-walked here. See main.py for the
--    algorithm this was generated by.
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (142, 65, 142, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (164, 0, 164, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (164, 0, 164, 39, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (181, 9, 181, 354, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (181, 9, 181, 355, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (181, 9, 181, 356, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (181, 9, 181, 361, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (181, 9, 181, 363, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (181, 9, 181, 364, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (181, 9, 181, 367, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (181, 9, 181, 374, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (182, 1308, 182, 765, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (182, 1308, 182, 1138, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (182, 1308, 182, 1145, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (182, 1308, 182, 1220, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (182, 1308, 182, 1251, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (182, 1308, 182, 1293, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (182, 1308, 182, 1294, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (238, 0, 238, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (238, 0, 238, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (238, 0, 238, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (238, 0, 238, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (238, 0, 238, 12, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (238, 0, 238, 13, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (238, 0, 238, 15, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (238, 0, 238, 16, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (392, 38, 392, 13, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (414, 0, 414, 25, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (414, 0, 414, 26, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 215, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 251, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 253, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 625, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 722, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 723, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 725, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 804, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 805, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 806, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 807, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (424, 1039, 424, 1692, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (426, 332, 426, 14, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (426, 332, 426, 109, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (426, 332, 426, 280, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (426, 332, 426, 305, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (426, 332, 426, 454, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (458, 143, 458, 130, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (461, 12, 461, 579, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (461, 12, 461, 580, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (461, 12, 461, 581, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (461, 12, 461, 582, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (461, 12, 461, 583, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (469, 280, 469, 77, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (469, 280, 469, 255, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (469, 280, 469, 256, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (469, 280, 469, 258, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (469, 280, 469, 260, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (469, 280, 469, 262, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (469, 280, 469, 265, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (469, 280, 469, 324, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (473, 46, 473, 14, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (491, 0, 491, 20, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (506, 0, 506, 20, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (506, 0, 506, 27, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (506, 0, 506, 32, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (506, 0, 506, 46, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (507, 0, 507, 73, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (527, 298, 527, 87, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (527, 298, 527, 96, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (566, 78, 566, 839, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (566, 78, 566, 850, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (566, 78, 566, 935, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (566, 78, 566, 937, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (571, 246, 571, 252, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (574, 311, 574, 179, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (576, 161, 576, 588, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (576, 161, 576, 593, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (588, 8, 588, 805, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (588, 8, 588, 806, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (595, 76, 595, 73, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (595, 76, 595, 284, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (600, 70, 600, 94, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (608, 0, 608, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (612, 1, 612, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (612, 1, 612, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (612, 1, 612, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (612, 1, 612, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (612, 1, 612, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (612, 1, 612, 24, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (612, 1, 612, 26, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (612, 1, 612, 32, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (613, 1, 613, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (613, 1, 613, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (616, 1, 616, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (616, 1, 616, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (616, 1, 616, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (616, 1, 616, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (617, 1, 617, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (617, 1, 617, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (617, 1, 617, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (617, 1, 617, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (617, 1, 617, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (617, 1, 617, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (626, 0, 626, 124, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (626, 0, 626, 128, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (639, 545, 639, 76, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (645, 0, 645, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (691, 69, 691, 374, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (691, 69, 691, 472, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (693, 0, 693, 467, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (700, 1, 700, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (700, 1, 700, 884, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (704, 900, 704, 598, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (706, 0, 706, 400, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (706, 0, 706, 470, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (713, 0, 713, 290, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (713, 0, 713, 682, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (713, 0, 713, 722, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (713, 0, 713, 728, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (715, 0, 715, 58, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (718, 0, 718, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (718, 0, 718, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (718, 0, 718, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (718, 0, 718, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (718, 0, 718, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (718, 0, 718, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (744, 1, 744, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (744, 1, 744, 246, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (744, 1, 744, 306, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (746, 1, 746, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (756, 2, 756, 25, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (758, 114, 758, 185, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (759, 1, 759, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (759, 1, 759, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (759, 1, 759, 14, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (765, 836, 765, 124, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (765, 836, 765, 1140, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (765, 836, 765, 1200, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (766, 278, 766, 843, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (776, 0, 776, 39, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (776, 0, 776, 40, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (782, 0, 782, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (782, 0, 782, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (782, 0, 782, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (782, 0, 782, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (782, 0, 782, 12, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (783, 0, 783, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (783, 0, 783, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (783, 0, 783, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (783, 0, 783, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (783, 0, 783, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (784, 0, 784, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (784, 0, 784, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (784, 0, 784, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (784, 0, 784, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (784, 0, 784, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (784, 0, 784, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (784, 0, 784, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (784, 0, 784, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (784, 0, 784, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (784, 0, 784, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (790, 0, 790, 427, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (790, 0, 790, 429, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (790, 0, 790, 433, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (825, 199, 825, 456, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (825, 199, 825, 458, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (825, 199, 825, 533, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (832, 203, 832, 177, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (849, 0, 849, 246, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (849, 0, 849, 247, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (856, 0, 856, 49, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (858, 0, 858, 13, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (858, 0, 858, 14, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (858, 0, 858, 15, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (858, 0, 858, 16, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (861, 5, 861, 458, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (869, 1, 869, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (869, 1, 869, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (869, 1, 869, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (869, 1, 869, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (869, 1, 869, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (870, 0, 870, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (870, 0, 870, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (870, 0, 870, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (870, 0, 870, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (870, 0, 870, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (870, 0, 870, 12, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (871, 0, 871, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (871, 0, 871, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (871, 0, 871, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (871, 0, 871, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (871, 0, 871, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (920, 63, 920, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (920, 63, 920, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (920, 63, 920, 48, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (976, 0, 976, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (980, 0, 980, 1193, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (980, 0, 980, 1258, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (980, 0, 980, 1327, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (980, 0, 980, 1556, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (980, 0, 980, 1590, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (980, 0, 980, 1701, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (980, 0, 980, 1726, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (982, 0, 982, 161, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (982, 0, 982, 224, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1000, 0, 1000, 69, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1012, 1, 1012, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1012, 1, 1012, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1012, 1, 1012, 87, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1012, 1, 1012, 117, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1012, 1, 1012, 118, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1012, 1, 1012, 119, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1012, 1, 1012, 120, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1012, 1, 1012, 172, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1012, 1, 1012, 175, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1013, 1, 1013, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1013, 1, 1013, 26, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1013, 1, 1013, 29, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1013, 1, 1013, 30, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1013, 1, 1013, 114, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1013, 1, 1013, 118, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1015, 1, 1015, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1015, 1, 1015, 146, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1015, 1, 1015, 473, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1015, 1, 1015, 484, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1021, 51, 1021, 18, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1021, 51, 1021, 34, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1025, 1, 1025, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1025, 1, 1025, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1025, 1, 1025, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1025, 1, 1025, 677, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1025, 1, 1025, 678, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1025, 1, 1025, 679, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1025, 1, 1025, 682, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1025, 1, 1025, 1028, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1025, 1, 1025, 1030, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1025, 1, 1025, 1093, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1028, 0, 1028, 22, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1028, 0, 1028, 23, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1029, 87, 1029, 338, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1033, 15, 1033, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1033, 15, 1033, 77, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1033, 15, 1033, 78, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1037, 143, 1037, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1037, 143, 1037, 114, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1038, 3, 1038, 473, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1040, 83, 1040, 45, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1040, 83, 1040, 59, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1040, 83, 1040, 60, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1049, 1, 1049, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1049, 1, 1049, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1049, 1, 1049, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1049, 1, 1049, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1049, 1, 1049, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1096, 0, 1096, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1096, 0, 1096, 34, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1096, 0, 1096, 36, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1096, 0, 1096, 95, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1096, 0, 1096, 100, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1096, 0, 1096, 101, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1096, 0, 1096, 102, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1096, 0, 1096, 104, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1135, 81, 1135, 46, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1135, 81, 1135, 133, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1135, 81, 1135, 141, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1135, 81, 1135, 142, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1136, 167, 1136, 498, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1139, 287, 1139, 80, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1139, 287, 1139, 135, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1154, 0, 1154, 66, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1158, 212, 1158, 93, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1158, 212, 1158, 304, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1158, 212, 1158, 333, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1181, 0, 1181, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1185, 0, 1185, 161, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1185, 0, 1185, 443, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1185, 0, 1185, 445, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1185, 0, 1185, 572, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1185, 0, 1185, 808, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1186, 1, 1186, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1186, 1, 1186, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1194, 135, 1194, 291, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1201, 0, 1201, 32, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1206, 0, 1206, 31, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1216, 1, 1216, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1216, 1, 1216, 90, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1216, 1, 1216, 162, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1216, 1, 1216, 187, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1221, 283, 1221, 226, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1235, 1, 1235, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1235, 1, 1235, 293, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1237, 335, 1237, 32, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1237, 335, 1237, 38, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1257, 479, 1257, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1257, 479, 1257, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1257, 479, 1257, 374, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1257, 479, 1257, 375, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1257, 479, 1257, 439, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1257, 479, 1257, 686, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1257, 479, 1257, 693, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1257, 479, 1257, 738, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1264, 498, 1264, 368, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1264, 498, 1264, 520, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1275, 5, 1275, 562, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1275, 5, 1275, 680, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1275, 5, 1275, 1359, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1275, 5, 1275, 1378, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1319, 615, 1319, 283, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1319, 615, 1319, 284, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1319, 615, 1319, 287, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1319, 615, 1319, 762, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1319, 615, 1319, 769, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1319, 615, 1319, 772, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1319, 615, 1319, 775, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1322, 20, 1322, 772, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1323, 23, 1323, 376, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1323, 23, 1323, 377, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1370, 169, 1370, 58, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1370, 169, 1370, 142, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1370, 169, 1370, 257, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1370, 169, 1370, 558, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1371, 1, 1371, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1371, 1, 1371, 64, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1371, 1, 1371, 67, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1372, 1, 1372, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1372, 1, 1372, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1372, 1, 1372, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1372, 1, 1372, 47, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1372, 1, 1372, 55, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1372, 1, 1372, 155, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1373, 1, 1373, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1373, 1, 1373, 28, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1374, 1, 1374, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1375, 1, 1375, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1375, 1, 1375, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1375, 1, 1375, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1375, 1, 1375, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1375, 1, 1375, 20, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1376, 1, 1376, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1377, 1, 1377, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1377, 1, 1377, 14, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 12, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1395, 1, 1395, 13, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1396, 1, 1396, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1396, 1, 1396, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1396, 1, 1396, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1396, 1, 1396, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1396, 1, 1396, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1396, 1, 1396, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1396, 1, 1396, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1396, 1, 1396, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1396, 1, 1396, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1396, 1, 1396, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1397, 1, 1397, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1397, 1, 1397, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1397, 1, 1397, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1397, 1, 1397, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1397, 1, 1397, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1397, 1, 1397, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1397, 1, 1397, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1397, 1, 1397, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1397, 1, 1397, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1397, 1, 1397, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1398, 1, 1398, 12, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1399, 1, 1399, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1399, 1, 1399, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1399, 1, 1399, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1399, 1, 1399, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1399, 1, 1399, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1399, 1, 1399, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1399, 1, 1399, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1399, 1, 1399, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1399, 1, 1399, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 2, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 4, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 5, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 6, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 7, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 8, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 9, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 10, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 11, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 12, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1400, 1, 1400, 13, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1403, 173, 1403, 3, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1403, 173, 1403, 28, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1403, 173, 1403, 288, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1403, 173, 1403, 539, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1403, 173, 1403, 612, 0, 2);
INSERT INTO dlinks (originconversationid, origindialogueid, destinationconversationid, destinationdialogueid, isConnector, priority) VALUES (1407, 0, 1407, 335, 0, 2);

-- 8. FTS5 trigram search indexes over dialogue text and branch titles.
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

COMMIT;
