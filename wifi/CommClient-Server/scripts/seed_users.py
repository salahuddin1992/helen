"""Bulk-seed N users into the local SQLite DB for load-testing.

Bypasses the HTTP /register flow (bcrypt + single-writer contention) and
inserts rows in batched transactions so very large counts (100k → 10M)
stay tractable.

Every user gets a fresh 64-char alphanumeric share_code via
SystemRandom.choices (CSPRNG) — matches app/core/share_code.py exactly.
Single shared bcrypt hash for all seeded users so /login still works
with the common password.

Usage:
    python scripts/seed_users.py --n 100000
    python scripts/seed_users.py --n 100000 --prefix load --db <path>
    python scripts/seed_users.py --n 100000 --out /tmp/users.json  # mints tokens
"""
from __future__ import annotations
import argparse, json, os, sqlite3, secrets, time, uuid
from pathlib import Path

# Reuse the exact hash of the test password so /login works.
# Generated once with bcrypt cost 4 (cheap) for 'Str0ng!Pass-42'.
PW = "Str0ng!Pass-42"

# Must match app/core/share_code.py exactly.
_ALPHABET = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "abcdefghijklmnopqrstuvwxyz"
    "0123456789"
)
_RNG = secrets.SystemRandom()

BATCH_SIZE = 10_000

def _bcrypt_hash(pw: str) -> str:
    import bcrypt
    # Cost 4 = fast hash, acceptable for ephemeral test accounts.
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt(rounds=4)).decode()

def _gen_share_code() -> str:
    # SystemRandom.choices is ~50× faster than secrets.choice in a loop
    # and still CSPRNG-backed — 62^64 keyspace = 4.4e114.
    return "".join(_RNG.choices(_ALPHABET, k=64))

def _default_db() -> Path:
    # Match the dev/packaged DB path the server uses by default on Windows.
    appdata = os.environ.get("APPDATA")
    if appdata:
        p = Path(appdata) / "CommClient" / "data" / "commclient.db"
        if p.exists():
            return p
    return Path("commclient.db")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, required=True)
    ap.add_argument("--prefix", type=str, default="seed")
    ap.add_argument("--db", type=Path, default=None,
                    help="Path to SQLite DB (default: %APPDATA%/CommClient/data/commclient.db)")
    ap.add_argument("--out", type=Path, default=None,
                    help="Optional JSON dump with tokens — skipped for huge runs when omitted")
    args = ap.parse_args()

    db_path = args.db or _default_db()
    if not db_path.exists():
        raise SystemExit(f"[seed] DB not found: {db_path}")

    t0 = time.time()
    hash_str = _bcrypt_hash(PW)
    print(f"[seed] hash ready in {time.time()-t0:.2f}s, target db = {db_path}")

    now = time.strftime("%Y-%m-%d %H:%M:%S")
    user_records: list[dict] = [] if args.out else []

    print(f"[seed] inserting {args.n:,} users in batches of {BATCH_SIZE:,}")
    t1 = time.time()
    conn = sqlite3.connect(str(db_path), timeout=60.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        insert_sql = (
            "INSERT INTO users ("
            "username, share_code, display_name, password_hash, avatar_url, bio, status, "
            "status_message, status_expires_at, last_seen, is_active, role, id, created_at, updated_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )

        done = 0
        batch: list[tuple] = []
        for i in range(args.n):
            uid = uuid.uuid4().hex
            uname = f"{args.prefix}_{i}_{secrets.token_hex(3)}"
            share_code = _gen_share_code()
            batch.append((
                uname, share_code, uname, hash_str, None, None,
                'offline', None, None, now, 1, 'user',
                uid, now, now,
            ))
            if args.out is not None:
                user_records.append(
                    {"username": uname, "password": PW, "user_id": uid, "share_code": share_code},
                )
            if len(batch) >= BATCH_SIZE:
                conn.executemany(insert_sql, batch)
                conn.commit()
                done += len(batch)
                batch.clear()
                elapsed = time.time() - t1
                rate = done / elapsed if elapsed > 0 else 0
                print(f"[seed]   {done:,}/{args.n:,} ({rate:,.0f}/s, {elapsed:.1f}s elapsed)")

        if batch:
            conn.executemany(insert_sql, batch)
            conn.commit()
            done += len(batch)
            batch.clear()
    finally:
        conn.close()
    print(f"[seed] insert done — {args.n:,} rows in {time.time()-t1:.1f}s")

    if args.out is not None:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from app.core.security import create_access_token  # noqa: E402
        for rec in user_records:
            rec["token"] = create_access_token(rec["user_id"], role="user")
        args.out.write_text(json.dumps(user_records))
        print(f"[seed] wrote {args.out} ({args.n:,} users) with tokens minted")

if __name__ == "__main__":
    main()
