"""Helen-Router launcher.

Usage:
  Development: python run.py
  Production:  Helen-Router.exe (PyInstaller frozen binary)
"""

from __future__ import annotations

import os
import sys

# Auto-create directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
for d in ("logs",):
    os.makedirs(os.path.join(BASE_DIR, d), exist_ok=True)

# Auto-create .env from .env.example
env_path = os.path.join(BASE_DIR, ".env")
env_example = os.path.join(BASE_DIR, ".env.example")
if not os.path.exists(env_path) and os.path.exists(env_example):
    import shutil
    shutil.copy2(env_example, env_path)


def main() -> None:
    # Load .env
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            for raw in f:
                raw = raw.strip()
                if not raw or raw.startswith("#"):
                    continue
                if "=" in raw:
                    k, v = raw.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())

    if not os.environ.get("HELEN_ROUTER_TOKEN"):
        print(
            "ERROR: HELEN_ROUTER_TOKEN is not set.\n"
            "       Generate one with:\n"
            "         python -c \"import secrets; "
            "print(secrets.token_hex(32))\"\n"
            "       and set it in .env.",
            file=sys.stderr,
        )
        sys.exit(2)

    host = os.environ.get("HELEN_ROUTER_HOST", "0.0.0.0")
    port = int(os.environ.get("HELEN_ROUTER_PORT", "8080"))

    import uvicorn
    print(f"Helen-Router starting on http://{host}:{port}")
    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        log_level=os.environ.get("HELEN_ROUTER_LOG_LEVEL", "info"),
        access_log=False,
    )


if __name__ == "__main__":
    main()
