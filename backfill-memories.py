#!/usr/bin/env python3
"""
Backfill OpenCode Memory from existing OpenCode sessions.

Reads all sessions from ~/.local/share/opencode/opencode.db and posts
summaries to the opencode-mem REST API at http://127.0.0.1:4747/api/memories.

Run AFTER restarting OpenCode (so the opencode-mem plugin is loaded and
the web server is running on port 4747).

Usage:
    python3 ~/.config/opencode/scripts/backfill-memories.py [--dry-run] [--limit N]
"""

import sqlite3
import json
import os
import sys
import hashlib
import time
import argparse
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

# ── Config ──────────────────────────────────────────────────────────────
DB_PATH = os.path.expanduser("~/.local/share/opencode/opencode.db")
MEM_API = "http://127.0.0.1:4747/api/memories"
BATCH_DELAY = 0.5  # seconds between API calls to avoid overwhelming


def get_project_tag(directory: str) -> str:
    """Generate container tag matching opencode-mem's format."""
    if not directory:
        directory = os.getcwd()
    # Use directory path as hash source (same as opencode-mem)
    h = hashlib.sha256(directory.encode()).hexdigest()[:16]
    return f"opencode_project_{h}"


def extract_session_summary(cursor, session_id: str) -> list:
    """Extract user prompts and AI responses from a session."""
    # Get user messages (role=user)
    cursor.execute("""
        SELECT p.data, m.time_created
        FROM part p
        JOIN message m ON m.id = p.message_id
        WHERE p.session_id = ? 
        AND p.data LIKE '%"type":"text"%'
        ORDER BY m.time_created ASC
    """, (session_id,))

    prompts = []

    for row in cursor.fetchall():
        try:
            data = json.loads(row[0])
            if data.get("type") == "text":
                text = data.get("text", "").strip()
                if text and len(text) > 10:  # Skip tiny fragments
                    prompts.append(text)
        except:
            continue

    return prompts


def build_memory_content(title: str, directory: str, agent: str, prompts: list) -> str:
    """Build a concise memory summary from session data."""
    # Take first 3 user prompts as context
    context = "\n".join(prompts[:3])
    if len(context) > 2000:
        context = context[:2000] + "..."

    return f"""## Session: {title or 'Untitled'}

**Project:** {os.path.basename(directory) if directory else 'Unknown'}
**Agent:** {agent or 'unknown'}

### Key Interactions:
{context}
"""


def post_memory(content: str, container_tag: str, session_id: str, title: str, dry_run: bool = False) -> bool:
    """Post a memory to the opencode-mem API."""
    payload = {
        "content": content,
        "containerTag": container_tag,
        "type": "session-summary",
        "tags": ["backfill", "session", session_id],
        "metadata": {
            "source": "backfill",
            "session_id": session_id,
            "original_title": title
        }
    }

    if dry_run:
        print(f"  [DRY RUN] Would post memory for: {title[:60]}")
        return True

    try:
        req = Request(
            MEM_API,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read())
            return True
    except (URLError, HTTPError) as e:
        print(f"  [ERROR] Failed to post: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Backfill OpenCode memories")
    parser.add_argument("--dry-run", action="store_true", help="Don't actually post")
    parser.add_argument("--limit", type=int, default=0, help="Max sessions to process (0=all)")
    parser.add_argument("--skip-check", action="store_true", help="Skip API health check")
    args = parser.parse_args()

    # Check API is available
    if not args.skip_check and not args.dry_run:
        try:
            req = Request("http://127.0.0.1:4747/api/stats", method="GET")
            with urlopen(req, timeout=5) as resp:
                print("✓ opencode-mem API is running")
        except:
            print("✗ opencode-mem API is not running. Start OpenCode first!")
            sys.exit(1)

    # Connect to database
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    # Get all sessions
    cursor.execute("""
        SELECT s.id, s.title, s.directory, s.agent, s.time_created
        FROM session s
        ORDER BY s.time_created DESC
    """)
    sessions = cursor.fetchall()
    print(f"Found {len(sessions)} sessions")

    if args.limit > 0:
        sessions = sessions[:args.limit]
        print(f"Processing first {args.limit} sessions")

    success = 0
    failed = 0

    for i, (session_id, title, directory, agent, time_created) in enumerate(sessions):
        print(f"\n[{i+1}/{len(sessions)}] {title or 'Untitled':60}")

        # Extract prompts
        prompts = extract_session_summary(cursor, session_id)
        if not prompts:
            print("  [SKIP] No content found")
            continue

        # Build memory
        content = build_memory_content(title, directory, agent, prompts)
        container_tag = get_project_tag(directory)

        # Post to API
        if post_memory(content, container_tag, session_id, title or "Untitled", args.dry_run):
            success += 1
            print(f"  ✓ Posted ({len(prompts)} prompts)")
        else:
            failed += 1

        # Rate limiting
        if not args.dry_run:
            time.sleep(BATCH_DELAY)

    conn.close()

    print(f"\n{'='*60}")
    print(f"Backfill complete: {success} success, {failed} failed")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
