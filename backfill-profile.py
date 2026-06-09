#!/usr/bin/env python3
"""
Backfill User Profile from existing OpenCode sessions.

Extracts user prompts from ~/.local/share/opencode/opencode.db,
inserts them into opencode-mem's user-prompts.db, and triggers
the profile learning API.

The profile system learns your preferences, patterns, and workflows
by analysing your prompts. By backfilling 145 sessions worth of
prompts, the AI gets a rich dataset to build an accurate profile.

Usage:
    python3 ~/.config/opencode/scripts/backfill-profile.py [--dry-run]
"""

import sqlite3
import json
import os
import sys
import time
import argparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ── Config ──────────────────────────────────────────────────────────────
OPENCODE_DB = os.path.expanduser("~/.local/share/opencode/opencode.db")
USER_PROMPTS_DB = os.path.expanduser("~/.opencode-mem/data/user-prompts.db")
REFRESH_API = "http://127.0.0.1:4747/api/user-profile/refresh"
INSERT_DELAY = 0.05  # seconds between inserts


def get_table_schema(db_path):
    """Return dict of table_name → list of column names."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    tables = {}
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
    for (table,) in cursor.fetchall():
        cursor.execute(f"PRAGMA table_info({table})")
        tables[table] = [row[1] for row in cursor.fetchall()]
    conn.close()
    return tables


def get_existing_ids(db_path):
    """Return set of existing prompt IDs to avoid duplicates."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    try:
        cursor.execute("SELECT id FROM user_prompts")
        ids = {row[0] for row in cursor.fetchall()}
    except sqlite3.Error:
        ids = set()
    conn.close()
    return ids


def extract_user_prompts(db_path):
    """Extract distinct user prompts from OpenCode sessions."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT p.data, p.session_id, p.message_id, m.time_created,
               s.title, s.directory, s.agent
        FROM part p
        JOIN message m ON m.id = p.message_id
        JOIN session s ON s.id = p.session_id
        WHERE (m.data LIKE '%"role":"user"%' OR m.data LIKE '%"role":"assistant"%')
        AND p.data LIKE '%"type":"text"%'
        ORDER BY m.time_created ASC
    """)
    prompts = []
    seen = set()
    for row in cursor.fetchall():
        try:
            data = json.loads(row[0])
            if data.get("type") != "text":
                continue
            text = data.get("text", "").strip()
            if not text or len(text) < 20:
                continue
            # Skip tool-call artifacts / file reads injected into prompts
            if text.startswith("Called the ") or text.startswith("<path>"):
                continue
            key = text[:80]
            if key in seen:
                continue
            seen.add(key)
            prompts.append({
                "text": text,
                "session_id": row[1],
                "message_id": row[2],
                "created_at": row[3],
                "title": row[4] or "",
                "directory": row[5] or "",
                "agent": row[6] or "",
            })
        except:
            continue
    conn.close()
    return prompts


def build_insert_sql(table, cols):
    """Build INSERT SQL dynamically from actual table columns.

    Every column gets a placeholder. Columns we don't have data for get NULL.
    Returns (sql, ordered_cols, value_keys) where value_keys tells do_inserts
    how to build each value: '__id__', '__captured__', '__null__', or a dict key.
    """
    placeholders = []
    value_keys = []

    # Map known column names to value keys
    known = {
        "id":                     "__id__",
        "session_id":             "session_id",
        "message_id":             "message_id",     # from p dict
        "project_path":           "directory",
        "content":                "text",
        "prompt_text":            "text",
        "text":                   "text",
        "created_at":             "created_at",
        "timestamp":              "created_at",
        "captured":               "0",              # not yet captured
        "user_learning_captured": "0",              # mark as unanalyzed
        "linked_memory_id":       "__null__",       # no linked memory
    }

    for col in cols:
        vk = known.get(col, "__null__")
        placeholders.append("?")
        value_keys.append(vk)

    cols_str = ", ".join(cols)
    vals_str = ", ".join(placeholders)
    sql = f"INSERT INTO {table} ({cols_str}) VALUES ({vals_str})"
    return sql, cols, value_keys


def do_inserts(db_path, table, sql, cols, value_keys, prompts, existing, dry_run):
    """Execute batch inserts. Returns count inserted."""
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    inserted = 0

    vals = []  # type: list
    for i, p in enumerate(prompts):
        prompt_id = f"bfill_{p['session_id'][:8]}_{i}"
        if prompt_id in existing:
            continue

        try:
            # Build the values tuple matching the column order
            vals = []
            for vk in value_keys:
                if vk == "__id__":
                    vals.append(prompt_id)
                elif vk == "__null__":
                    vals.append(None)
                elif vk == "__captured__" or vk == "0":
                    vals.append(0)
                else:
                    vals.append(p.get(vk, ""))
            vals_tuple = tuple(vals)

            if dry_run:
                if i < 3:
                    print(f"  [DRY RUN] Would insert: {cols[:4]} = {vals_tuple[:4]}")
                inserted += 1
                existing.add(prompt_id)
                if i % 500 == 0 and i > 0:
                    print(f"  ... would insert {inserted} prompts so far")
                continue

            cursor.execute(sql, vals_tuple)
            conn.commit()
            inserted += 1
            existing.add(prompt_id)

            if i % 100 == 0 and i > 0:
                print(f"  ... inserted {inserted} prompts so far")

            time.sleep(INSERT_DELAY)
        except sqlite3.Error as e:
            if i < 3:
                vlen = len(vals) if 'vals' in dir() else '?'
                print(f"  ⚠ Schema mismatch — table has {len(cols)} cols, "
                      f"attempting {vlen} values: {e}")
                print(f"    SQL: {sql[:120]}")
            continue

    conn.close()
    return inserted


def trigger_refresh(dry_run=False):
    """Trigger profile learning via API."""
    if dry_run:
        print("  [DRY RUN] Would call POST /api/user-profile/refresh")
        return
    try:
        req = Request(REFRESH_API, method="POST")
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            print(f"  ✓ Refresh response: {data}")
    except (URLError, HTTPError) as e:
        print(f"  ✗ Refresh failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="Backfill OpenCode user profile")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--skip-check", action="store_true")
    args = parser.parse_args()

    # ── Sanity checks ────────────────────────────────────────────
    if not os.path.exists(OPENCODE_DB):
        print(f"✗ OpenCode DB not found: {OPENCODE_DB}")
        sys.exit(1)
    if not os.path.exists(USER_PROMPTS_DB):
        print(f"✗ user-prompts.db not found: {USER_PROMPTS_DB}")
        print("  Is OpenCode running with opencode-mem loaded?")
        sys.exit(1)
    if not args.skip_check and not args.dry_run:
        try:
            urlopen(Request("http://127.0.0.1:4747/api/stats"), timeout=5)
            print("✓ opencode-mem API is running\n")
        except:
            print("✗ opencode-mem API not reachable. Start OpenCode first!")
            sys.exit(1)

    # ── Inspect schema ───────────────────────────────────────────
    tables = get_table_schema(USER_PROMPTS_DB)
    print(f"Available tables: {list(tables.keys())}")
    if "user_prompts" not in tables:
        print(f"✗ 'user_prompts' table not found in user-prompts.db")
        sys.exit(1)

    cols = tables["user_prompts"]
    print(f"user_prompts columns: {cols}")

    # Build SQL dynamically from actual columns
    sql, ordered_cols, value_keys = build_insert_sql("user_prompts", cols)
    if not sql:
        print(f"✗ Could not build INSERT from columns: {cols}")
        sys.exit(1)
    print(f"SQL: {sql[:100]}...")

    # ── Extract prompts ──────────────────────────────────────────
    print(f"\nExtracting prompts from OpenCode sessions...")
    prompts = extract_user_prompts(OPENCODE_DB)
    print(f"  Found {len(prompts)} unique user prompts across sessions")
    if args.limit > 0:
        prompts = prompts[:args.limit]
        print(f"  Limited to {args.limit} prompts")

    if not prompts:
        print("✗ No prompts found")
        return

    # ── Check existing ───────────────────────────────────────────
    existing = get_existing_ids(USER_PROMPTS_DB)
    print(f"  {len(existing)} prompts already in user-prompts.db")

    # ── Insert ───────────────────────────────────────────────────
    print(f"\nInserting prompts into user-prompts.db...")
    inserted = do_inserts(
        USER_PROMPTS_DB, "user_prompts",
        sql, ordered_cols, value_keys,
        prompts, existing, args.dry_run
    )
    print(f"  ✓ Inserted {inserted} new prompts")

    # ── Trigger profile learning ─────────────────────────────────
    print(f"\nTriggering profile learning...")
    trigger_refresh(dry_run=args.dry_run)

    print(f"\n{'='*60}")
    print(f"Profile backfill complete: {inserted} prompts inserted")
    print(f"The profile builds when 10+ unanalyzed prompts accumulate.")
    print(f"Check: http://127.0.0.1:4747 → User Profile tab")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
