"""Launch the Helen-Rendezvous service. Binds the HTTP listener; the
relay TCP listeners start from the FastAPI startup hook in ``main.py``.

Env:
  HELEN_RENDEZVOUS_TOKEN      shared bootstrap token (required — no default)
  HELEN_RENDEZVOUS_HOST       HTTP bind host (default: 0.0.0.0)
  HELEN_RENDEZVOUS_PORT       HTTP bind port (default: 9090)
  HELEN_RELAY_BACKEND_PORT    TCP relay backend port (default: 9101)
  HELEN_RELAY_FRONTEND_PORT   TCP relay frontend port (default: 9102)
"""

from __future__ import annotations

import os
import sys

import uvicorn


def main() -> None:
    host = os.environ.get("HELEN_RENDEZVOUS_HOST", "0.0.0.0")
    port = int(os.environ.get("HELEN_RENDEZVOUS_PORT", "9090"))
    if not os.environ.get("HELEN_RENDEZVOUS_TOKEN"):
        tf = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".token")
        if not os.path.exists(tf):
            print(
                "ERROR: HELEN_RENDEZVOUS_TOKEN is not set and no .token file exists.\n"
                "       Pick a strong random string and either export it as an env\n"
                "       var or write it to Helen-Rendezvous/.token (one line).",
                file=sys.stderr,
            )
            sys.exit(2)
    # Pass the imported app object directly instead of "main:app" so
    # the PyInstaller-packed binary doesn't depend on `main` being
    # importable via sys.path lookup at runtime (the bundle's modules
    # live inside _MEIPASS, not on sys.path).
    from main import app  # noqa: E402  (lazy: env was validated above)
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
