#!/usr/bin/env python3
"""
Materialize a full 100-server chaos compose file from the skeleton.

The skeleton at ``docker-compose.chaos.yml`` declares servers 001-005
by hand (so the structure is reviewable in version control); this
helper emits the remaining ones into ``docker-compose.chaos.full.yml``
ready for ``docker compose up``.

Usage
-----
    python scripts/gen_chaos_compose.py [--count N]

Defaults to 100 servers. The first ``5`` are kept from the skeleton;
the helper appends from ``006`` upward.

Region allocation: 4 buckets (chaos-r1..r4) split evenly. Lets the
shortest-path Dijkstra exercise region-aware weights even inside the
chaos chain.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SKELETON_NAME = "docker-compose.chaos.yml"
OUTPUT_NAME = "docker-compose.chaos.full.yml"
KEEP_HARDCODED = 5  # servers 001-005 already in the skeleton
BUCKET_REGIONS = ["chaos-r1", "chaos-r2", "chaos-r3", "chaos-r4"]


def server_block(n: int) -> str:
    sid = f"server_{n:03d}"
    name = f"helen_chaos_{n:03d}"
    region = BUCKET_REGIONS[(n - 1) % len(BUCKET_REGIONS)]
    return (
        f"  server-{n:03d}:\n"
        f"    <<: *helen-common\n"
        f"    container_name: {name}\n"
        f"    environment:\n"
        f"      <<: *helen-env\n"
        f"      HELEN_SERVER_ID: {sid}\n"
        f"      HELEN_REGION: {region}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--count", type=int, default=100,
        help="total number of helen-server replicas (default 100)",
    )
    args = parser.parse_args()
    if args.count < KEEP_HARDCODED + 1:
        print(
            f"--count must be > {KEEP_HARDCODED} (the skeleton declares "
            f"{KEEP_HARDCODED} servers by hand)",
            file=sys.stderr,
        )
        return 2

    here = Path(__file__).resolve().parent.parent
    skeleton = here / SKELETON_NAME
    if not skeleton.exists():
        print(f"skeleton not found: {skeleton}", file=sys.stderr)
        return 2

    src = skeleton.read_text(encoding="utf-8")

    # Find the volumes: marker line and inject server blocks before it.
    insert_before = "networks:\n  helen-chaos:\n"
    idx = src.find(insert_before)
    if idx < 0:
        print("could not find networks: marker in skeleton", file=sys.stderr)
        return 2

    blocks: list[str] = []
    for n in range(KEEP_HARDCODED + 1, args.count + 1):
        blocks.append(server_block(n))

    out = src[:idx] + "\n".join(blocks) + "\n" + src[idx:]
    out_path = here / OUTPUT_NAME
    out_path.write_text(out, encoding="utf-8")
    print(
        f"wrote {out_path} with {args.count} servers "
        f"({KEEP_HARDCODED} from skeleton + {args.count - KEEP_HARDCODED} generated)"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
