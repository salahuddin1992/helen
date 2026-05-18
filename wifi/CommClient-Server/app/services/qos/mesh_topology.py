"""
Mesh / SFU topology resolver.

For group calls in mesh routing, every participant maintains a direct
RTCPeerConnection with every other participant. This module reconstructs
that graph from the active call state plus the latest QoS snapshot so the
admin UI can render the topology and highlight poor links.

For SFU/hybrid topologies we still emit a logical graph (every participant
↔ SFU node) so the visualization works uniformly.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any

from app.core.logging import get_logger
from app.services.qos.stats_collector import qos_stats_collector

logger = get_logger(__name__)


# Link is considered "poor" if either direction breaches one of:
POOR_LINK_LOSS_PCT = 5.0
POOR_LINK_RTT_MS = 300.0
POOR_LINK_MOS = 3.5


@dataclass
class TopologyNode:
    id: str
    role: str = "participant"           # initiator|participant|sfu
    mos_avg: float = 0.0
    streams: int = 0


@dataclass
class TopologyEdge:
    a: str
    b: str
    direction: str = "bidirectional"    # bidirectional|a_to_b|b_to_a
    rtt_ms: float = 0.0
    loss_pct: float = 0.0
    jitter_ms: float = 0.0
    mos: float = 0.0
    bandwidth_kbps: float = 0.0
    poor: bool = False
    reasons: list[str] = field(default_factory=list)


class QoSMeshTopology:
    """Build a per-call mesh graph from the rolling buffer."""

    def __init__(self) -> None:
        self._collector = qos_stats_collector

    # ── Public API ─────────────────────────────────────────────────────

    def build(self, call_id: str) -> dict[str, Any]:
        """
        Compute the topology for ``call_id``.

        Returns a dict with ``nodes``, ``edges``, ``routing`` and
        ``poor_links`` ready for the front-end graph renderer.
        """
        try:
            from app.services.call_service import call_service
            call = call_service.get_call(call_id)
        except Exception:                          # pragma: no cover
            call = None
        if not call:
            return {
                "call_id": call_id, "routing": "unknown",
                "nodes": [], "edges": [], "poor_links": [],
            }

        snap = self._collector.snapshot(call_id)
        latest = self._collector.latest_per_participant(call_id)

        routing = getattr(call, "routing", "mesh")
        participants = list(call.participants.keys())
        initiator = getattr(call, "initiator_id", participants[0] if participants else None)

        nodes = [
            TopologyNode(
                id=pid,
                role="initiator" if pid == initiator else "participant",
                mos_avg=latest.get(pid, {}).get("mos_avg", 0.0),
                streams=len((latest.get(pid) or {}).get("streams") or {}),
            )
            for pid in participants
        ]

        edges: list[TopologyEdge] = []
        if routing in ("p2p", "mesh"):
            edges = self._build_mesh_edges(participants, latest)
        else:
            # SFU/hybrid — fake an "SFU" node and star-graph it.
            sfu_id = f"sfu:{call_id[:8]}"
            nodes.append(TopologyNode(id=sfu_id, role="sfu",
                                      mos_avg=0.0, streams=0))
            edges = self._build_sfu_edges(sfu_id, participants, latest)

        poor = [asdict(e) for e in edges if e.poor]
        return {
            "call_id": call_id,
            "routing": routing,
            "generated_at": time.time(),
            "nodes": [asdict(n) for n in nodes],
            "edges": [asdict(e) for e in edges],
            "poor_links": poor,
            "raw_snapshot_keys": list(snap.get("streams", {}).keys())[:50],
        }

    # ── Edge builders ──────────────────────────────────────────────────

    def _build_mesh_edges(
        self, participants: list[str], latest: dict[str, dict],
    ) -> list[TopologyEdge]:
        edges: list[TopologyEdge] = []
        for i, a in enumerate(participants):
            for b in participants[i + 1:]:
                # Take the worst of the two halves so a single bad path
                # still surfaces as a poor link.
                ab = self._edge_metrics(latest.get(a, {}), peer=b, direction="outbound")
                ba = self._edge_metrics(latest.get(b, {}), peer=a, direction="outbound")

                rtt = max(ab["rtt_ms"], ba["rtt_ms"])
                loss = max(ab["loss_pct"], ba["loss_pct"])
                jitter = max(ab["jitter_ms"], ba["jitter_ms"])
                bw = max(ab["bitrate_kbps"], ba["bitrate_kbps"])
                # MOS is the *worse* (lower) of the two
                mos = min(ab["mos"], ba["mos"]) if ab["mos"] and ba["mos"] else (ab["mos"] or ba["mos"])

                poor, reasons = self._classify(loss, rtt, mos)
                edges.append(TopologyEdge(
                    a=a, b=b,
                    rtt_ms=rtt, loss_pct=loss, jitter_ms=jitter,
                    mos=mos, bandwidth_kbps=bw,
                    poor=poor, reasons=reasons,
                ))
        return edges

    def _build_sfu_edges(
        self, sfu_id: str, participants: list[str], latest: dict[str, dict],
    ) -> list[TopologyEdge]:
        edges: list[TopologyEdge] = []
        for pid in participants:
            m = self._edge_metrics(latest.get(pid, {}), peer=sfu_id, direction="outbound")
            poor, reasons = self._classify(m["loss_pct"], m["rtt_ms"], m["mos"])
            edges.append(TopologyEdge(
                a=pid, b=sfu_id,
                rtt_ms=m["rtt_ms"], loss_pct=m["loss_pct"], jitter_ms=m["jitter_ms"],
                mos=m["mos"], bandwidth_kbps=m["bitrate_kbps"],
                poor=poor, reasons=reasons,
            ))
        return edges

    @staticmethod
    def _edge_metrics(slot: dict, peer: str, direction: str) -> dict[str, float]:
        """
        Reduce a participant's per-stream latest snapshot to scalar
        link-level metrics. We don't have per-peer per-stream metrics in
        the v1 collector — instead we average over the participant's own
        streams, which is a reasonable approximation for mesh where every
        peer contributes equally to outbound congestion.
        """
        streams = (slot or {}).get("streams") or {}
        if not streams:
            return {"rtt_ms": 0.0, "loss_pct": 0.0, "jitter_ms": 0.0,
                    "mos": 0.0, "bitrate_kbps": 0.0}
        # Prefer the outbound stream where possible.
        chosen = [s for s in streams.values()
                  if s.get("direction") == direction] or list(streams.values())
        n = len(chosen)
        return {
            "rtt_ms":    sum(s["rtt_ms"]       for s in chosen) / n,
            "loss_pct":  sum(s["loss_pct"]     for s in chosen) / n,
            "jitter_ms": sum(s["jitter_ms"]    for s in chosen) / n,
            "mos":       sum(s["mos"]          for s in chosen) / n,
            "bitrate_kbps": sum(s["bitrate_kbps"] for s in chosen) / n,
        }

    @staticmethod
    def _classify(loss: float, rtt: float, mos: float) -> tuple[bool, list[str]]:
        reasons: list[str] = []
        if loss > POOR_LINK_LOSS_PCT:
            reasons.append(f"loss>{POOR_LINK_LOSS_PCT}%")
        if rtt > POOR_LINK_RTT_MS:
            reasons.append(f"rtt>{POOR_LINK_RTT_MS}ms")
        if mos and mos < POOR_LINK_MOS:
            reasons.append(f"mos<{POOR_LINK_MOS}")
        return (len(reasons) > 0, reasons)


qos_mesh_topology = QoSMeshTopology()
