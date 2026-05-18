"""Wipe all chat content while keeping users, channels, and channel
membership intact.

Deletes messages, attachments, reactions, edits, pin records, message
status, and read markers. Preserves users + channels so the iOS
simulator and desktop client keep working without re-onboarding.
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "commclient.db"

# Tables we'll touch. We list them and only delete from the ones that
# actually exist — schema can vary across server versions.
CANDIDATE_TABLES = [
    "messages",
    "message_attachments",
    "message_reactions",
    "message_edits",
    "message_pins",
    "message_reads",
    "message_status",
    "channel_pins",          # pinned messages on a channel (but keep prefs)
    "files",                 # uploaded blobs referenced from messages
    "file_metadata",
]


def existing_tables(conn: sqlite3.Connection) -> set[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    return {r[0] for r in rows}


def main() -> int:
    if not DB.exists():
        print(f"DB not found: {DB}")
        return 1

    print(f"DB: {DB}")
    conn = sqlite3.connect(DB, timeout=10)

    tables = existing_tables(conn)
    print(f"Schema has {len(tables)} tables")

    counts_before: dict[str, int] = {}
    for t in CANDIDATE_TABLES:
        if t in tables:
            (n,) = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
            counts_before[t] = n

    if not counts_before:
        print("No message-bearing tables found; nothing to wipe.")
        return 0

    print("\nBefore wipe:")
    for t, n in counts_before.items():
        print(f"  {t:<28} {n:>6}")

    try:
        conn.execute("BEGIN")
        for t in counts_before:
            conn.execute(f"DELETE FROM {t}")
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"FAILED: {e}")
        return 2

    print("\nAfter wipe:")
    for t in counts_before:
        (n,) = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()
        print(f"  {t:<28} {n:>6}")

    # Survey what we kept.
    for label, table in [("users", "users"), ("channels", "channels"),
                         ("channel_members", "channel_members")]:
        if table in tables:
            (n,) = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
            print(f"  kept {label:<22} {n:>6}")

    conn.close()
    print("\n✓ Messages wiped. Users + channels intact.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
