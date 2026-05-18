"""
BandwidthShaper — per-peer token-bucket rate limiter + preset engine.

In-memory token buckets are sized from ``FederationShaperRule`` rows.
Two buckets per peer (ingress / egress) gate every byte that flows
through the federation transport.

Presets
-------
* ``equal``  — split total budget evenly across all peers.
* ``region`` — proportional to the number of peers per region.
* ``role``   — masters get 4x, followers 2x, observers 1x.
* ``custom`` — caller-supplied params (per-peer overrides).

Actual vs configured rates are tracked on a 1-second sliding window
exposed via ``actuals(peer_id)``.

Singleton: ``get_shaper()``.
"""
from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any, Deque, Optional

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import async_session_factory
from app.models.federation_peer import FederationPeerMeta
from app.models.federation_shaper_rule import (
    FederationShaperRule,
    VALID_SHAPER_PRESETS,
)
from app.models.federation_v2 import FederatedServer

logger = structlog.get_logger(__name__)


# ── token bucket ────────────────────────────────────────────


@dataclass
class _Bucket:
    capacity_bits: float
    refill_rate_bps: float
    tokens: float = 0.0
    last: float = field(default_factory=time.monotonic)

    def consume(self, bytes_: int) -> bool:
        bits = bytes_ * 8
        now = time.monotonic()
        elapsed = max(0.0, now - self.last)
        self.tokens = min(self.capacity_bits, self.tokens + elapsed * self.refill_rate_bps)
        self.last = now
        if self.tokens >= bits:
            self.tokens -= bits
            return True
        return False


@dataclass
class _Window:
    """1-second sliding window of (ts, bytes) pairs."""
    samples: Deque[tuple[float, int]] = field(default_factory=lambda: deque(maxlen=512))

    def record(self, bytes_: int) -> None:
        self.samples.append((time.monotonic(), bytes_))

    def kbps(self) -> float:
        if not self.samples:
            return 0.0
        now = time.monotonic()
        cutoff = now - 1.0
        recent = [(t, b) for (t, b) in self.samples if t >= cutoff]
        if not recent:
            return 0.0
        total_bytes = sum(b for _, b in recent)
        return total_bytes * 8 / 1000.0


# ── shaper service ──────────────────────────────────────────


class BandwidthShaper:
    def __init__(self) -> None:
        self._in: dict[str, _Bucket] = {}
        self._out: dict[str, _Bucket] = {}
        self._in_win: dict[str, _Window] = defaultdict(_Window)
        self._out_win: dict[str, _Window] = defaultdict(_Window)
        self._lock = asyncio.Lock()
        self._loaded = False

    # ── public ───────────────────────────────────────────────

    async def reload(self) -> None:
        """Rebuild in-memory buckets from DB."""
        async with self._lock:
            async with async_session_factory() as db:
                rows = (await db.execute(
                    select(FederationShaperRule).where(
                        FederationShaperRule.active.is_(True)
                    )
                )).scalars().all()
            for r in rows:
                self._install(r)
            self._loaded = True

    async def list_rules(self) -> list[dict[str, Any]]:
        async with async_session_factory() as db:
            rows = (await db.execute(
                select(FederationShaperRule)
                .where(FederationShaperRule.active.is_(True))
            )).scalars().all()
        return [self._rule_dict(r) for r in rows]

    async def get_rule(self, server_id: str) -> Optional[dict[str, Any]]:
        async with async_session_factory() as db:
            row = (await db.execute(
                select(FederationShaperRule).where(
                    FederationShaperRule.server_id == server_id,
                    FederationShaperRule.active.is_(True),
                )
            )).scalar_one_or_none()
        return self._rule_dict(row) if row else None

    async def set_rule(
        self,
        server_id: str,
        *,
        in_kbps: int,
        out_kbps: int,
        burst_kbps: int = 0,
        priority: int = 4,
        preset: str = "custom",
        params: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> dict[str, Any]:
        if preset not in VALID_SHAPER_PRESETS:
            raise ValueError(f"invalid_preset:{preset}")
        if not (0 <= priority <= 7):
            raise ValueError("invalid_priority")
        async with async_session_factory() as db:
            # Deactivate prior
            prior = (await db.execute(
                select(FederationShaperRule).where(
                    FederationShaperRule.server_id == server_id,
                    FederationShaperRule.active.is_(True),
                )
            )).scalars().all()
            for p in prior:
                p.active = False
            row = FederationShaperRule(
                server_id=server_id,
                in_kbps=max(0, in_kbps),
                out_kbps=max(0, out_kbps),
                burst_kbps=max(0, burst_kbps),
                priority=int(priority),
                preset=preset,
                params=params or {},
                active=True,
                created_by=actor,
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            # Update meta pointer (best-effort)
            meta = (await db.execute(
                select(FederationPeerMeta).where(
                    FederationPeerMeta.server_id == server_id
                )
            )).scalar_one_or_none()
            if meta is not None:
                meta.shaper_rule_id = row.id
                await db.commit()
            elif row is not None:
                meta = FederationPeerMeta(
                    server_id=server_id, shaper_rule_id=row.id,
                )
                db.add(meta)
                await db.commit()
        self._install(row)
        return self._rule_dict(row)

    async def apply_preset(
        self,
        preset: str,
        params: Optional[dict[str, Any]] = None,
        actor: str = "system",
    ) -> list[dict[str, Any]]:
        if preset not in VALID_SHAPER_PRESETS:
            raise ValueError(f"invalid_preset:{preset}")
        params = params or {}
        total_in = int(params.get("total_in_kbps", 100_000))
        total_out = int(params.get("total_out_kbps", 100_000))
        async with async_session_factory() as db:
            peers = (await db.execute(
                select(FederatedServer)
            )).scalars().all()
            metas = {
                m.server_id: m
                for m in (await db.execute(
                    select(FederationPeerMeta)
                )).scalars().all()
            }

        if not peers:
            return []
        out: list[dict[str, Any]] = []
        if preset == "equal":
            per_in = max(1, total_in // len(peers))
            per_out = max(1, total_out // len(peers))
            for p in peers:
                out.append(await self.set_rule(
                    p.server_id, in_kbps=per_in, out_kbps=per_out,
                    burst_kbps=per_out, priority=4,
                    preset="equal", params=params, actor=actor,
                ))
        elif preset == "region":
            buckets: dict[str, list[str]] = defaultdict(list)
            for p in peers:
                m = metas.get(p.server_id)
                buckets[(m.region if m else "default") or "default"].append(p.server_id)
            for region, sids in buckets.items():
                per_in = max(1, total_in // max(1, len(sids) * len(buckets)))
                per_out = max(1, total_out // max(1, len(sids) * len(buckets)))
                for sid in sids:
                    out.append(await self.set_rule(
                        sid, in_kbps=per_in, out_kbps=per_out,
                        burst_kbps=per_out, priority=4,
                        preset="region",
                        params={"region": region, **params},
                        actor=actor,
                    ))
        elif preset == "role":
            weights = {"master": 4, "follower": 2, "observer": 1, "candidate": 2}
            denom = 0
            for p in peers:
                m = metas.get(p.server_id)
                role = (m.role if m else "follower") or "follower"
                denom += weights.get(role, 1)
            denom = max(1, denom)
            for p in peers:
                m = metas.get(p.server_id)
                role = (m.role if m else "follower") or "follower"
                w = weights.get(role, 1)
                per_in = max(1, total_in * w // denom)
                per_out = max(1, total_out * w // denom)
                priority = 6 if role == "master" else 4 if role == "follower" else 2
                out.append(await self.set_rule(
                    p.server_id, in_kbps=per_in, out_kbps=per_out,
                    burst_kbps=per_out, priority=priority,
                    preset="role", params={"role": role, **params},
                    actor=actor,
                ))
        else:  # custom
            overrides: dict[str, dict[str, Any]] = (params.get("per_peer") or {})
            for p in peers:
                cfg = overrides.get(p.server_id) or {}
                out.append(await self.set_rule(
                    p.server_id,
                    in_kbps=int(cfg.get("in_kbps", total_in // max(1, len(peers)))),
                    out_kbps=int(cfg.get("out_kbps", total_out // max(1, len(peers)))),
                    burst_kbps=int(cfg.get("burst_kbps", 0)),
                    priority=int(cfg.get("priority", 4)),
                    preset="custom",
                    params=cfg,
                    actor=actor,
                ))
        return out

    def actuals(self, server_id: str) -> dict[str, Any]:
        return {
            "server_id": server_id,
            "in_kbps_actual":  self._in_win[server_id].kbps(),
            "out_kbps_actual": self._out_win[server_id].kbps(),
            "in_capacity_bits":  self._in.get(server_id).capacity_bits if server_id in self._in else 0,
            "out_capacity_bits": self._out.get(server_id).capacity_bits if server_id in self._out else 0,
        }

    def allow(self, server_id: str, direction: str, bytes_: int) -> bool:
        """Token-bucket gate. ``direction`` is ``in`` or ``out``."""
        bucket = self._in.get(server_id) if direction == "in" else self._out.get(server_id)
        if bucket is None:
            # No rule = unlimited
            return True
        ok = bucket.consume(bytes_)
        if ok:
            win = self._in_win if direction == "in" else self._out_win
            win[server_id].record(bytes_)
        return ok

    # ── internals ────────────────────────────────────────────

    def _install(self, rule: FederationShaperRule) -> None:
        in_bps = rule.in_kbps * 1000.0
        out_bps = rule.out_kbps * 1000.0
        burst_bits = max(rule.burst_kbps, rule.out_kbps, rule.in_kbps) * 1000.0
        # Capacity is at least 1s worth; if burst given, use that.
        self._in[rule.server_id] = _Bucket(
            capacity_bits=max(in_bps, burst_bits, 1.0),
            refill_rate_bps=max(in_bps, 1.0),
            tokens=max(in_bps, burst_bits, 1.0),
        )
        self._out[rule.server_id] = _Bucket(
            capacity_bits=max(out_bps, burst_bits, 1.0),
            refill_rate_bps=max(out_bps, 1.0),
            tokens=max(out_bps, burst_bits, 1.0),
        )

    def _rule_dict(self, r: FederationShaperRule) -> dict[str, Any]:
        return {
            "id":         r.id,
            "server_id":  r.server_id,
            "in_kbps":    r.in_kbps,
            "out_kbps":   r.out_kbps,
            "burst_kbps": r.burst_kbps,
            "priority":   r.priority,
            "preset":     r.preset,
            "active":     r.active,
            "params":     r.params or {},
            "note":       r.note,
            "created_by": r.created_by,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }


# ── singleton ───────────────────────────────────────────────


_shaper: Optional[BandwidthShaper] = None


def get_shaper() -> BandwidthShaper:
    global _shaper
    if _shaper is None:
        _shaper = BandwidthShaper()
    return _shaper
