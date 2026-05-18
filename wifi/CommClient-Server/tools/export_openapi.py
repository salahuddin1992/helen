#!/usr/bin/env python3
"""
Export the Helen admin OpenAPI 3.1 spec to JSON + YAML, split by tag,
and generate a Markdown summary.

Usage
-----
    python tools/export_openapi.py [--output docs/openapi/] [--full]

Outputs
-------
``docs/openapi/helen_admin_v1.json``
    Combined OpenAPI 3.1 spec covering every admin router that
    successfully mounts.

``docs/openapi/helen_admin_v1.yaml``
    Same spec, YAML serialization.

``docs/openapi/admin_<tag>.json``
    Per-router subset (paths whose first tag is ``admin-<tag>``).

``docs/openapi/ENDPOINTS.md``
    Markdown report with endpoint counts per router, the full
    endpoint table, and a separate websocket section.

Design notes
------------
* The tool mounts each of the 11 admin routers onto a fresh
  ``FastAPI`` app rather than booting ``app.main:create_app``. The
  full boot path pulls in NATS / redis / native crypto deps that are
  not necessary just to extract a spec.
* If a router fails to import (sandbox), the failure is logged into
  the Markdown report so operators can see what was missed.
* OpenAPI version is forced to 3.1.0. FastAPI emits 3.1 by default
  in versions ≥ 0.99; we set it explicitly for older deployments.
"""
from __future__ import annotations

import argparse
import importlib
import json
import logging
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("export_openapi")


# ─────────────────────────────────────────────────────────────────────
# Router catalogue
# ─────────────────────────────────────────────────────────────────────

ADMIN_ROUTER_MODULES: tuple[tuple[str, str], ...] = (
    ("admin_monitoring",      "app.api.routes.admin_monitoring"),
    ("admin_topology",        "app.api.routes.admin_topology"),
    ("admin_siem",            "app.api.routes.admin_siem"),
    ("admin_tenancy_portal",  "app.api.routes.admin_tenancy_portal"),
    ("admin_dr_v2",           "app.api.routes.admin_dr_v2"),
    ("admin_plugins",         "app.api.routes.admin_plugins"),
    ("admin_federation",      "app.api.routes.admin_federation"),
    ("admin_qos",             "app.api.routes.admin_qos"),
    ("admin_compliance",      "app.api.routes.admin_compliance"),
    ("admin_onboarding",      "app.api.routes.admin_onboarding"),
    ("admin_router_control",  "app.api.routes.admin_router_control"),
)


# ─────────────────────────────────────────────────────────────────────
# App builder
# ─────────────────────────────────────────────────────────────────────


def build_app(full: bool = False):
    """Build a FastAPI app with every admin router mounted.

    Parameters
    ----------
    full
        If True, boot the heavyweight ``app.main:create_app``. If
        False (default), mount only the 11 admin routers on a fresh
        app — much faster and works in restricted sandboxes.
    """
    from fastapi import FastAPI

    if full:
        try:
            from app.main import create_app
            return create_app(), [m for _, m in ADMIN_ROUTER_MODULES], []
        except Exception as e:
            log.warning("create_app failed, falling back to lite mount: %s", e)

    app = FastAPI(
        title="Helen Admin API",
        description="Helen / CommClient-Server admin control plane API",
        version="1.0.0",
        openapi_version="3.1.0",
    )
    mounted: list[str] = []
    skipped: list[tuple[str, str]] = []
    for short, mod_path in ADMIN_ROUTER_MODULES:
        try:
            mod = importlib.import_module(mod_path)
            app.include_router(mod.router)
            mounted.append(short)
            log.info("mounted %s", short)
        except Exception as e:
            skipped.append((short, f"{e.__class__.__name__}: {e}"))
            log.warning("skipped %s — %s", short, e)
    return app, mounted, skipped


# ─────────────────────────────────────────────────────────────────────
# Spec extraction
# ─────────────────────────────────────────────────────────────────────


def split_by_tag(spec: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Split the combined spec by the first tag on each operation.

    Returns ``{tag: sub_spec}``. Each sub_spec is a stand-alone
    OpenAPI document that contains only the operations bearing that
    tag.
    """
    out: dict[str, dict[str, Any]] = {}
    for path, methods in spec.get("paths", {}).items():
        for method, op in methods.items():
            if method.startswith("x-") or not isinstance(op, dict):
                continue
            tags = op.get("tags") or ["untagged"]
            tag = tags[0]
            sub = out.setdefault(tag, {
                "openapi": spec.get("openapi", "3.1.0"),
                "info": {
                    **spec.get("info", {}),
                    "title": f"Helen Admin API — {tag}",
                },
                "paths": {},
                "components": spec.get("components", {}),
            })
            sub["paths"].setdefault(path, {})[method] = op
    return out


def collect_websocket_routes(app) -> list[dict[str, str]]:
    """Collect ``APIWebSocketRoute`` rows (OpenAPI doesn't model them)."""
    out: list[dict[str, str]] = []
    for r in app.routes:
        if r.__class__.__name__ == "APIWebSocketRoute":
            out.append({
                "path": getattr(r, "path", ""),
                "name": getattr(r, "name", ""),
            })
    return out


# ─────────────────────────────────────────────────────────────────────
# Output writers
# ─────────────────────────────────────────────────────────────────────


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)


def write_yaml(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        import yaml  # type: ignore
    except ImportError:
        log.warning("PyYAML not installed; writing fallback flat YAML")
        with open(path, "w", encoding="utf-8") as f:
            f.write("# PyYAML unavailable. JSON spec follows:\n")
            json.dump(obj, f, indent=2, ensure_ascii=False)
        return
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(obj, f, sort_keys=False, allow_unicode=True)


# ─────────────────────────────────────────────────────────────────────
# Markdown summary
# ─────────────────────────────────────────────────────────────────────


def render_markdown(spec: dict[str, Any], app, mounted: list[str],
                     skipped: list[tuple[str, str]]) -> str:
    """Render the ENDPOINTS.md report (Arabic-friendly, RTL-safe)."""
    paths = spec.get("paths", {})
    # Group by first tag
    by_tag: dict[str, list[tuple[str, str, dict]]] = defaultdict(list)
    for path, methods in paths.items():
        for method, op in methods.items():
            if not isinstance(op, dict) or method.startswith("x-"):
                continue
            tag = (op.get("tags") or ["untagged"])[0]
            by_tag[tag].append((method.upper(), path, op))

    ws = collect_websocket_routes(app)
    total_http = sum(len(v) for v in by_tag.values())
    total = total_http + len(ws)

    lines: list[str] = []
    lines.append("# Helen Admin API — Endpoints Index")
    lines.append("")
    lines.append("> توثيق شامل لجميع نقاط النهاية (Endpoints) في لوحات الإدارة الـ 11 لمنصة Helen / CommClient.")
    lines.append("> Generated by `tools/export_openapi.py`.")
    lines.append("")
    lines.append("## ملخّص (Summary)")
    lines.append("")
    lines.append(f"- **Total endpoints**: **{total}** ({total_http} HTTP + {len(ws)} WebSocket)")
    lines.append(f"- **Routers mounted**: {len(mounted)} / {len(ADMIN_ROUTER_MODULES)}")
    if skipped:
        lines.append(f"- **Routers skipped (sandbox / deps missing)**: {len(skipped)}")
    lines.append("")
    lines.append("### Endpoint count by router")
    lines.append("")
    lines.append("| Router (Tag) | Endpoints |")
    lines.append("|---|---:|")
    for tag in sorted(by_tag.keys()):
        lines.append(f"| `{tag}` | {len(by_tag[tag])} |")
    if ws:
        lines.append(f"| `websocket` | {len(ws)} |")
    lines.append("")

    if skipped:
        lines.append("### Skipped routers")
        lines.append("")
        for short, err in skipped:
            lines.append(f"- `{short}`: {err}")
        lines.append("")

    lines.append("## كامل قائمة الـ Endpoints")
    lines.append("")
    lines.append("| Method | Path | Summary | Auth | Tag |")
    lines.append("|---|---|---|---|---|")
    for tag in sorted(by_tag.keys()):
        for method, path, op in sorted(by_tag[tag], key=lambda x: (x[1], x[0])):
            summary = (op.get("summary") or op.get("description") or "").split("\n")[0][:80]
            auth = "admin" if "admin" in tag else "any"
            # Escape pipes in path/summary
            sp = path.replace("|", "\\|")
            ss = summary.replace("|", "\\|")
            lines.append(f"| `{method}` | `{sp}` | {ss} | {auth} | `{tag}` |")
    lines.append("")

    if ws:
        lines.append("## WebSocket Endpoints")
        lines.append("")
        lines.append("OpenAPI 3.1 لا يدعم WebSocket بشكل أصلي — هذه القائمة مولّدة من الـ routing table مباشرة.")
        lines.append("")
        lines.append("| Path | Name |")
        lines.append("|---|---|")
        for w in sorted(ws, key=lambda x: x["path"]):
            lines.append(f"| `{w['path']}` | `{w['name']}` |")
        lines.append("")

    return "\n".join(lines) + "\n"


# ─────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export the Helen admin OpenAPI 3.1 spec to JSON+YAML+Markdown."
    )
    parser.add_argument(
        "--output", "-o", default="docs/openapi",
        help="Output directory (default: docs/openapi)",
    )
    parser.add_argument(
        "--full", action="store_true",
        help="Boot full app via create_app() instead of the lite admin mount.",
    )
    parser.add_argument(
        "--no-split", action="store_true",
        help="Skip writing per-tag spec files.",
    )
    args = parser.parse_args(argv)

    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("building app (full=%s) …", args.full)
    app, mounted, skipped = build_app(full=args.full)
    log.info("mounted %d routers, skipped %d", len(mounted), len(skipped))

    spec = app.openapi()
    # Force OpenAPI 3.1
    spec["openapi"] = "3.1.0"
    spec.setdefault("info", {})
    spec["info"].setdefault("title", "Helen Admin API")
    spec["info"].setdefault("version", "1.0.0")

    # ── Combined spec ────────────────────────────────────────
    combined_json = out_dir / "helen_admin_v1.json"
    combined_yaml = out_dir / "helen_admin_v1.yaml"
    write_json(combined_json, spec)
    write_yaml(combined_yaml, spec)
    log.info("wrote %s", combined_json)
    log.info("wrote %s", combined_yaml)

    # ── Per-tag splits ──────────────────────────────────────
    if not args.no_split:
        for tag, sub in split_by_tag(spec).items():
            slug = tag.replace("-", "_")
            path = out_dir / f"{slug}.json"
            write_json(path, sub)
            log.info("wrote %s (%d paths)", path, len(sub["paths"]))

    # ── Markdown ─────────────────────────────────────────────
    md_path = out_dir / "ENDPOINTS.md"
    md = render_markdown(spec, app, mounted, skipped)
    md_path.write_text(md, encoding="utf-8")
    log.info("wrote %s (%d lines)", md_path, md.count("\n"))

    # Summary to stdout for CI parsing.
    total_http = sum(
        1 for path, methods in spec.get("paths", {}).items()
        for m in methods if not m.startswith("x-")
    )
    ws = collect_websocket_routes(app)
    print(f"OPENAPI_EXPORT_DONE total={total_http + len(ws)} http={total_http} ws={len(ws)} mounted={len(mounted)} skipped={len(skipped)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
