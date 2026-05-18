"""
Automatic control plane for Helen-Server.

Sensor → Aggregator → Policy → Executor → Audit pipeline.

Runs as an asyncio background task on a 2s tick. Reads host metrics
(CPU, RSS, NIC) via psutil, reads app metrics (room count, message
throughput) from existing services, and reads operator caps from
data/server_roles.json.

Emits decisions (state transitions, emergency mode entries, admission
refusals) to an append-only NDJSON audit log plus an in-RAM ring
buffer exposed via the /api/admin/control-plane endpoints.

This is single-node only. Multi-node coordination (leader election via
DB lease, per-node metric aggregation) is a later addition — the
surface here is designed to add it without rewrites.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import psutil
import structlog

logger = structlog.get_logger(__name__)

# ── Paths ──────────────────────────────────────────────────────
_DATA_DIR = Path(__file__).resolve().parents[2] / "data"
_ROLES_FILE = _DATA_DIR / "server_roles.json"
_AUDIT_FILE = _DATA_DIR / "control_plane_audit.ndjson"
_STATE_FILE = _DATA_DIR / "control_plane_state.json"


# ── Threshold profiles ─────────────────────────────────────────
PROFILES = {
    "conservative": {
        "cpu_up":          60, "cpu_down":      48,
        "rss_warn":        70, "rss_emergency": 85,
        "loss_audio":       6, "loss_chat":    12,
        "rtt_degrade":     200,
        "sfu_cpu_cap":     75,
        "nic_sat":         65,
        "participants_sfu": 3,
        "dwell_upshift":   30, "dwell_downshift":  3,
    },
    "balanced": {
        "cpu_up":          70, "cpu_down":      56,
        "rss_warn":        75, "rss_emergency": 90,
        "loss_audio":      10, "loss_chat":    18,
        "rtt_degrade":     300,
        "sfu_cpu_cap":     85,
        "nic_sat":         80,
        "participants_sfu": 3,
        "dwell_upshift":   15, "dwell_downshift":  5,
    },
    "aggressive": {
        "cpu_up":          80, "cpu_down":      64,
        "rss_warn":        80, "rss_emergency": 95,
        "loss_audio":      14, "loss_chat":    24,
        "rtt_degrade":     450,
        "sfu_cpu_cap":     92,
        "nic_sat":         90,
        "participants_sfu": 4,
        "dwell_upshift":    5, "dwell_downshift": 10,
    },
}

# Cooldowns (seconds) — enforced by Executor.
COOLDOWNS = {
    "room.migrate":        60,
    "room.mode_change":    30,
    "emergency.toggle":    120,
    "admission.toggle":     15,
    "recording.toggle":     45,
    "simulcast.drop":       10,
}


# ── Dataclasses ────────────────────────────────────────────────
@dataclass
class HostSample:
    ts: float
    cpu_pct: float
    rss_pct: float
    nic_rx_bps: float
    nic_tx_bps: float
    disk_q: float  # 0..1 proxy for disk pressure
    process_count: int


@dataclass
class AppSample:
    ts: float
    active_rooms: int
    active_participants: int
    active_sockets: int
    msg_rate: float        # msgs / second (last 2s)
    msg_queue_lag_ms: float
    file_queue_depth: int
    file_queue_wait_s: float
    db_write_p95_ms: float


@dataclass
class NetSample:
    ts: float
    loss_p95_pct: float    # across active media sessions
    rtt_p95_ms: float
    jitter_p95_ms: float


@dataclass
class Decision:
    ts: float
    seq: int
    kind: str              # "policy.decision"
    scope: str             # "global" | "room"
    room_id: Optional[str]
    from_state: Optional[str]
    to_state: str
    trigger: str
    inputs: dict[str, Any]
    profile: str
    override_active: bool = False
    suppressed: bool = False
    suppressed_reason: Optional[str] = None
    cooldown_until: Optional[float] = None


@dataclass
class GlobalState:
    phase: str = "normal"   # normal | degraded | emergency | frozen
    admission_open: bool = True
    recording_paused: bool = False
    file_throttle: float = 1.0  # 0..1 fraction of NIC budget
    last_change: float = 0.0
    last_trigger: str = ""


@dataclass
class RoomInfo:
    """Per-room tracked state.

    `desired_mode` is what Policy wants; `applied_mode` is what Executor
    actually set (after cooldown checks). `override` is operator-forced.
    """
    room_id: str
    kind: str = "chat"                 # chat | voice | video
    participants: int = 0
    started_at: float = field(default_factory=time.time)
    last_update: float = field(default_factory=time.time)
    desired_mode: str = "p2p"          # p2p | sfu | relay | audio-only | chat-only
    applied_mode: str = "p2p"
    override: Optional[dict] = None    # {mode, ttl_until, by, reason}
    critical: bool = False
    loss_p95: float = 0.0
    rtt_p95: float = 0.0


@dataclass
class ControlState:
    global_state: GlobalState = field(default_factory=GlobalState)
    rooms: dict[str, RoomInfo] = field(default_factory=dict)
    last_action_at: dict[str, float] = field(default_factory=dict)  # action_key → ts
    profile: str = "balanced"
    admission_refusals: int = 0


# ── Aggregator: EWMA + p95 over rolling window ─────────────────
class _Window:
    """Fixed-size window of samples for percentile computation."""

    def __init__(self, maxlen: int = 30):
        self._buf: deque[float] = deque(maxlen=maxlen)

    def push(self, v: float) -> None:
        self._buf.append(float(v))

    def p95(self) -> float:
        if not self._buf:
            return 0.0
        s = sorted(self._buf)
        idx = int(0.95 * (len(s) - 1))
        return s[idx]

    def avg(self) -> float:
        if not self._buf:
            return 0.0
        return sum(self._buf) / len(self._buf)


class Aggregator:
    """Smooths the raw sensor stream.

    - fast signals (loss, RTT, jitter): EWMA α=0.3
    - slow signals (CPU, RSS): 30s p95 window (at 2s tick = 15 samples)
    - trigger timers: per-signal "above-threshold for N seconds" counters
    """

    def __init__(self) -> None:
        self._cpu_win = _Window(15)
        self._rss_win = _Window(15)
        self._nic_rx_win = _Window(15)
        self._nic_tx_win = _Window(15)
        self._loss_ewma = 0.0
        self._rtt_ewma = 0.0
        self._jitter_ewma = 0.0
        self._above_since: dict[str, float] = {}

    def ingest(self, host: HostSample, app: AppSample, net: NetSample) -> None:
        self._cpu_win.push(host.cpu_pct)
        self._rss_win.push(host.rss_pct)
        self._nic_rx_win.push(host.nic_rx_bps)
        self._nic_tx_win.push(host.nic_tx_bps)
        alpha = 0.3
        self._loss_ewma = alpha * net.loss_p95_pct + (1 - alpha) * self._loss_ewma
        self._rtt_ewma = alpha * net.rtt_p95_ms + (1 - alpha) * self._rtt_ewma
        self._jitter_ewma = alpha * net.jitter_p95_ms + (1 - alpha) * self._jitter_ewma

    def sustained(self, key: str, cond: bool, now: float) -> float:
        """Return seconds this condition has held continuously (0 when false)."""
        if cond:
            if key not in self._above_since:
                self._above_since[key] = now
            return now - self._above_since[key]
        self._above_since.pop(key, None)
        return 0.0

    def snapshot(self) -> dict[str, float]:
        return {
            "cpu_p95":   self._cpu_win.p95(),
            "cpu_avg":   self._cpu_win.avg(),
            "rss_p95":   self._rss_win.p95(),
            "nic_rx":    self._nic_rx_win.avg(),
            "nic_tx":    self._nic_tx_win.avg(),
            "loss_ewma": self._loss_ewma,
            "rtt_ewma":  self._rtt_ewma,
            "jitter_ewma": self._jitter_ewma,
        }


# ── Sensors: host + app + net ──────────────────────────────────
class HostSensor:
    """Host-level resource readings via psutil."""

    def __init__(self) -> None:
        self._last_nic: tuple[float, int, int] = (time.time(), 0, 0)
        try:
            c = psutil.net_io_counters()
            self._last_nic = (time.time(), c.bytes_recv, c.bytes_sent)
        except Exception:
            pass

    def sample(self) -> HostSample:
        now = time.time()
        try:
            cpu = psutil.cpu_percent(interval=None)
        except Exception:
            cpu = 0.0
        try:
            rss_pct = psutil.virtual_memory().percent
        except Exception:
            rss_pct = 0.0
        rx_bps = tx_bps = 0.0
        try:
            c = psutil.net_io_counters()
            dt = max(0.001, now - self._last_nic[0])
            rx_bps = (c.bytes_recv - self._last_nic[1]) / dt
            tx_bps = (c.bytes_sent - self._last_nic[2]) / dt
            self._last_nic = (now, c.bytes_recv, c.bytes_sent)
        except Exception:
            pass
        disk_q = 0.0
        try:
            # psutil on Windows doesn't always expose queue depth; fall back
            # to utilization percentage from io_counters. Best-effort only.
            # For now, 0.0 — slot is present for later wiring.
            pass
        except Exception:
            pass
        try:
            pc = len(psutil.pids())
        except Exception:
            pc = 0
        return HostSample(
            ts=now, cpu_pct=cpu, rss_pct=rss_pct,
            nic_rx_bps=rx_bps, nic_tx_bps=tx_bps,
            disk_q=disk_q, process_count=pc,
        )


class AppSensor:
    """App-level readings pulled from existing services when available.

    Every getter is wrapped in a try/except so the control plane never
    crashes when an upstream service is temporarily unreachable.
    """

    def __init__(self) -> None:
        self._last_msg_count: tuple[float, int] = (time.time(), 0)

    def sample(self) -> AppSample:
        now = time.time()
        rooms = 0
        participants = 0
        sockets = 0
        try:
            from app.socket import sio
            # Rough: number of distinct rooms and connection count.
            sockets = len(getattr(sio.manager, "rooms", {}).get("/", {}) or {}) \
                      if hasattr(sio, "manager") else 0
        except Exception:
            pass
        msg_rate = 0.0
        msg_queue_lag = 0.0
        try:
            from app.services import stats_service
            stats = stats_service.get_runtime_counters()
            total_msgs = stats.get("total_messages_sent", 0)
            dt = max(0.001, now - self._last_msg_count[0])
            msg_rate = max(0.0, (total_msgs - self._last_msg_count[1]) / dt)
            self._last_msg_count = (now, total_msgs)
            msg_queue_lag = stats.get("socket_emit_lag_ms", 0.0)
        except Exception:
            pass
        file_queue_depth = 0
        file_queue_wait = 0.0
        try:
            from app.services import resumable_upload_service as rus
            file_queue_depth = getattr(rus, "pending_count", lambda: 0)()
        except Exception:
            pass
        db_p95 = 0.0
        try:
            from app.services import stats_service
            db_p95 = stats_service.db_write_p95_ms()
        except Exception:
            pass
        return AppSample(
            ts=now,
            active_rooms=rooms,
            active_participants=participants,
            active_sockets=sockets,
            msg_rate=msg_rate,
            msg_queue_lag_ms=msg_queue_lag,
            file_queue_depth=file_queue_depth,
            file_queue_wait_s=file_queue_wait,
            db_write_p95_ms=db_p95,
        )


class NetSensor:
    """Network quality readings.

    Pulls two sources when available:
      1. Per-room RTCP / ICE stats (populated externally via
         ControlPlane.update_room on call legs) — aggregated to p95.
      2. Socket.IO engine ping RTT — a coarse proxy for control-plane
         latency; signals general network health even when no media flows.
    When neither source is wired, returns zeros (policy treats as healthy).
    """

    def __init__(self) -> None:
        self._rtt_samples: deque[float] = deque(maxlen=50)

    def sample(self, rooms: Optional[dict[str, "RoomInfo"]] = None) -> NetSample:
        # Aggregate per-room loss/RTT samples into p95.
        loss_vals, rtt_vals = [], []
        if rooms:
            for r in rooms.values():
                if r.loss_p95:
                    loss_vals.append(r.loss_p95)
                if r.rtt_p95:
                    rtt_vals.append(r.rtt_p95)
        # Socket.IO engine RTT fallback.
        try:
            from app.socket import sio
            mgr = getattr(sio, "manager", None)
            if mgr is not None:
                engine = getattr(sio, "eio", None)
                if engine is not None and hasattr(engine, "sockets"):
                    for _, sock in list(engine.sockets.items())[:50]:
                        prt = getattr(sock, "last_ping", 0)
                        last_pong = getattr(sock, "last_pong", 0)
                        if prt and last_pong and last_pong > prt:
                            rtt_ms = (last_pong - prt) * 1000.0
                            if 0 < rtt_ms < 5000:
                                rtt_vals.append(rtt_ms)
        except Exception:
            pass
        def _p95(vs: list[float]) -> float:
            if not vs: return 0.0
            s = sorted(vs); i = int(0.95 * (len(s) - 1))
            return s[i]
        return NetSample(
            ts=time.time(),
            loss_p95_pct=_p95(loss_vals),
            rtt_p95_ms=_p95(rtt_vals),
            jitter_p95_ms=0.0,  # requires RTCP; leave 0 until wired
        )


# ── Policy engine: rules + hysteresis ──────────────────────────
class Policy:
    def __init__(self, profile: str = "balanced") -> None:
        self.profile = profile

    def thresholds(self) -> dict:
        return PROFILES.get(self.profile, PROFILES["balanced"])

    def decide_rooms(
        self,
        state: ControlState,
        snap: dict[str, float],
        operator_caps: dict,
    ) -> list[Decision]:
        """Per-room state decisions.

        Applied after global decide; considers global phase as a ceiling.
        Operator overrides dominate unless the global phase is emergency.
        """
        t = self.thresholds()
        now = time.time()
        phase = state.global_state.phase
        mode_cap = (operator_caps or {}).get("policy_mode", {}).get("value", "auto")

        out: list[Decision] = []
        for rid, r in state.rooms.items():
            current = r.applied_mode

            # 1) Operator per-room override — highest priority except emergency.
            if r.override and r.override.get("ttl_until", 0) > now:
                desired = r.override["mode"]
                trig = "operator.room_override"
            # 2) Emergency/frozen phase forces audio-only or chat-only for all rooms.
            elif phase == "frozen":
                desired = "chat-only"
                trig = "global.frozen"
            elif phase == "emergency":
                desired = "audio-only" if r.kind in ("voice", "video") else "chat-only"
                trig = "global.emergency"
            # 3) Global policy_mode cap — operator global lock.
            elif mode_cap == "chat_only":
                desired = "chat-only"; trig = "global.policy_mode.chat_only"
            elif mode_cap == "audio_only":
                desired = "audio-only" if r.kind in ("voice", "video") else "chat-only"
                trig = "global.policy_mode.audio_only"
            elif mode_cap == "no_sfu_p2p_only":
                desired = "p2p"; trig = "global.policy_mode.p2p_only"
            else:
                # 4) Automatic mode selection based on participants + load.
                if r.kind == "chat":
                    desired = "chat-only"; trig = "kind.chat"
                elif phase == "degraded" and not r.critical:
                    desired = "audio-only"; trig = "degraded_non_critical"
                elif r.participants >= t["participants_sfu"]:
                    desired = "sfu"; trig = "participants>=sfu_threshold"
                else:
                    desired = "p2p"; trig = "participants<sfu_threshold"
                # Loss-based downgrades.
                if r.loss_p95 >= t["loss_chat"]:
                    desired = "chat-only"; trig = "loss>chat_threshold"
                elif r.loss_p95 >= t["loss_audio"]:
                    desired = "audio-only"; trig = "loss>audio_threshold"

            if desired != current:
                out.append(Decision(
                    ts=now, seq=0, kind="policy.decision", scope="room",
                    room_id=rid, from_state=current, to_state=desired,
                    trigger=trig,
                    inputs={
                        "participants": r.participants,
                        "kind":         r.kind,
                        "loss_p95":     round(r.loss_p95, 2),
                        "phase":        phase,
                        "critical":     r.critical,
                        "override":     bool(r.override),
                    },
                    profile=self.profile,
                    override_active=bool(r.override) or (mode_cap != "auto"),
                ))
        return out

    def decide_global(
        self,
        state: ControlState,
        snap: dict[str, float],
        app: AppSample,
        agg: Aggregator,
        operator_caps: dict,
    ) -> Optional[Decision]:
        """Decide global phase transitions.

        Inputs come smoothed from `snap`. Hysteresis enforced here:
        phase can only change if the sustained time exceeds dwell.
        """
        t = self.thresholds()
        now = time.time()
        cpu = snap["cpu_p95"]
        rss = snap["rss_p95"]
        loss = snap["loss_ewma"]
        db_p95 = app.db_write_p95_ms

        current = state.global_state.phase
        desired = current

        # Hard trigger: emergency
        sev_cpu  = agg.sustained("g.cpu_emerg",  cpu > t["rss_emergency"], now)
        sev_rss  = agg.sustained("g.rss_emerg",  rss > t["rss_emergency"], now)
        sev_db   = agg.sustained("g.db_emerg",   db_p95 > 500, now)

        # Soft trigger: degraded
        warn_cpu = agg.sustained("g.cpu_warn", cpu > t["cpu_up"], now)
        warn_rss = agg.sustained("g.rss_warn", rss > t["rss_warn"], now)
        warn_loss = agg.sustained("g.loss_warn", loss > 3.0, now)

        # Recovery (below downshift thresholds)
        rec_cpu  = agg.sustained("g.cpu_rec",  cpu < t["cpu_down"], now)
        rec_rss  = agg.sustained("g.rss_rec",  rss < t["cpu_down"], now)

        trig = ""
        if (sev_cpu > 30 or sev_rss > 30 or sev_db > 60):
            desired = "frozen" if (cpu > 95 or rss > 95) else "emergency"
            trig = "cpu>95" if sev_cpu > 30 else ("rss>95" if sev_rss > 30 else "db_p95>500")
        elif (warn_cpu > t["dwell_upshift"] or warn_rss > t["dwell_upshift"]
              or warn_loss > t["dwell_upshift"]):
            desired = "degraded"
            trig = (f"cpu>{t['cpu_up']}" if warn_cpu else
                    f"rss>{t['rss_warn']}" if warn_rss else "loss>3%")
        elif rec_cpu > 120 and rec_rss > 120 and current != "normal":
            desired = "normal"
            trig = "metrics_green_120s"

        # Honor operator override: if policy_mode forces lesser state, cap.
        mode_cap = (operator_caps or {}).get("policy_mode", {}).get("value", "auto")
        if mode_cap == "chat_only":
            pass  # chat-only is handled per-room; global phase unaffected
        # (video_ok etc. don't loosen emergency — safety first)

        if desired != current:
            return Decision(
                ts=now, seq=0, kind="policy.decision", scope="global",
                room_id=None, from_state=current, to_state=desired,
                trigger=trig or "combined", inputs={
                    "cpu_p95": round(cpu, 1), "rss_p95": round(rss, 1),
                    "loss_ewma": round(loss, 2), "db_p95": round(db_p95, 1),
                    "sustained": {
                        "cpu_emerg": round(sev_cpu, 1),
                        "rss_emerg": round(sev_rss, 1),
                        "db_emerg":  round(sev_db, 1),
                        "cpu_warn":  round(warn_cpu, 1),
                        "rss_warn":  round(warn_rss, 1),
                    },
                },
                profile=self.profile,
                override_active=(mode_cap != "auto"),
            )
        return None


# ── Executor: apply decisions with cooldown ────────────────────
class Executor:
    def __init__(self, state: ControlState) -> None:
        self.state = state

    def apply(self, d: Decision) -> Decision:
        """Apply a decision respecting cooldowns. Sets suppressed flags.

        Cooldown keys: global actions are plain (e.g. "emergency.toggle")
        so they throttle the whole system. Room actions are suffixed with
        the room_id so each room has its own cooldown clock.
        """
        now = time.time()
        cooldown_class = self._cooldown_for(d)
        cooldown_key = cooldown_class
        if cooldown_class and d.scope == "room" and d.room_id:
            cooldown_key = f"{cooldown_class}:{d.room_id}"
        if cooldown_class:
            last = self.state.last_action_at.get(cooldown_key, 0)
            if now - last < COOLDOWNS.get(cooldown_class, 0):
                d.suppressed = True
                d.suppressed_reason = f"cooldown:{cooldown_class}"
                d.cooldown_until = last + COOLDOWNS.get(cooldown_class, 0)
                return d
            self.state.last_action_at[cooldown_key] = now

        # Apply the state change.
        if d.scope == "global":
            self.state.global_state.phase = d.to_state
            self.state.global_state.last_change = now
            self.state.global_state.last_trigger = d.trigger
            # Derived flags.
            self.state.global_state.admission_open = \
                d.to_state in ("normal", "degraded")
            self.state.global_state.recording_paused = \
                d.to_state in ("emergency", "frozen")
        elif d.scope == "room" and d.room_id:
            r = self.state.rooms.get(d.room_id)
            if r:
                r.applied_mode = d.to_state
                r.desired_mode = d.to_state
                r.last_update = now
        return d

    @staticmethod
    def _cooldown_for(d: Decision) -> Optional[str]:
        if d.scope == "global":
            return "emergency.toggle"
        if d.scope == "room":
            return "room.mode_change"
        return None


# ── Audit trail ────────────────────────────────────────────────
class Audit:
    def __init__(self, ring_size: int = 500) -> None:
        self.ring: deque[Decision] = deque(maxlen=ring_size)
        self._seq = 0

    def record(self, d: Decision) -> None:
        self._seq += 1
        d.seq = self._seq
        self.ring.append(d)
        try:
            _AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
            with _AUDIT_FILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(_decision_to_json(d)) + "\n")
        except Exception as e:
            logger.warning("control_plane_audit_write_failed", error=str(e))

    def recent(self, limit: int = 50) -> list[dict]:
        items = list(self.ring)[-limit:]
        items.reverse()
        return [_decision_to_json(d) for d in items]


def _decision_to_json(d: Decision) -> dict:
    return {
        "ts": _iso(d.ts), "seq": d.seq, "kind": d.kind, "scope": d.scope,
        "room_id": d.room_id, "from_state": d.from_state,
        "to_state": d.to_state, "trigger": d.trigger,
        "inputs": d.inputs, "profile": d.profile,
        "override_active": d.override_active,
        "suppressed": d.suppressed,
        "suppressed_reason": d.suppressed_reason,
        "cooldown_until": _iso(d.cooldown_until) if d.cooldown_until else None,
    }


def _iso(ts: float) -> str:
    import datetime as _dt
    return _dt.datetime.utcfromtimestamp(ts).isoformat(timespec="milliseconds") + "Z"


# ── Orchestrator: the async tick loop ──────────────────────────
class ControlPlane:
    """Singleton tick loop binding sensors → aggregator → policy → executor → audit.

    Start with `await ControlPlane.instance().start()`. Stop via `.stop()`.
    """

    _singleton: "ControlPlane | None" = None

    def __init__(self, tick_sec: float = 2.0) -> None:
        self.tick = tick_sec
        self.host = HostSensor()
        self.app = AppSensor()
        self.net = NetSensor()
        self.agg = Aggregator()
        self.state = ControlState()
        self.policy = Policy(self.state.profile)
        self.executor = Executor(self.state)
        self.audit = Audit()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._last_snap: dict[str, float] = {}
        self._last_app: Optional[AppSample] = None

    @classmethod
    def instance(cls) -> "ControlPlane":
        if cls._singleton is None:
            cls._singleton = ControlPlane()
        return cls._singleton

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run(), name="helen-control-plane")
        logger.info("control_plane_started", tick_sec=self.tick,
                    profile=self.state.profile)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        logger.info("control_plane_stopped")

    async def _run(self) -> None:
        # Warmup cpu_percent so the first reading isn't 0.0.
        try: psutil.cpu_percent(interval=None)
        except Exception: pass
        await asyncio.sleep(0.2)
        while self._running:
            try:
                await self._tick()
            except Exception as e:
                logger.error("control_plane_tick_failed", error=str(e))
            await asyncio.sleep(self.tick)

    async def _tick(self) -> None:
        host = self.host.sample()
        app  = self.app.sample()
        net  = self.net.sample(self.state.rooms)
        self.agg.ingest(host, app, net)
        # Push self-load to every known peer once every ~5s (every 3rd tick).
        # Direct await — safer than fire-and-forget when we need
        # deterministic behavior under restart + shutdown.
        self._gossip_counter = getattr(self, "_gossip_counter", 0) + 1
        if self._gossip_counter % 3 == 0:
            try:
                await asyncio.wait_for(_gossip_self_to_peers(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("gossip_cycle_timeout")
            except Exception as _e:
                logger.warning("gossip_cycle_error", error=str(_e))
        snap = self.agg.snapshot()
        self._last_snap = snap
        self._last_app = app

        caps = _read_operator_caps()
        # Decide global phase first (per-room decisions depend on it).
        prev_phase = self.state.global_state.phase
        proposal = self.policy.decide_global(self.state, snap, app, self.agg, caps)
        if proposal:
            applied = self.executor.apply(proposal)
            self.audit.record(applied)
            # Fire side-effects if phase actually changed (not suppressed).
            if not applied.suppressed and applied.to_state != prev_phase:
                await self._broadcast_phase_change(applied)

        # Decide per-room modes.
        room_decisions = self.policy.decide_rooms(self.state, snap, caps)
        for d in room_decisions:
            applied = self.executor.apply(d)
            self.audit.record(applied)

        # Evict stale rooms (no update in 5 min → room ended).
        now = time.time()
        stale = [rid for rid, r in self.state.rooms.items()
                 if now - r.last_update > 300]
        for rid in stale:
            self.state.rooms.pop(rid, None)

    async def _broadcast_phase_change(self, d: "Decision") -> None:
        """Emit `system.phase_change` to all sockets when global phase shifts.

        Non-fatal: control plane must not depend on socket being up.
        """
        try:
            from app.socket import sio
            await sio.emit("system.phase_change", {
                "from":    d.from_state,
                "to":      d.to_state,
                "trigger": d.trigger,
                "ts":      _iso(d.ts),
                "recording_paused": self.state.global_state.recording_paused,
                "admission_open":   self.state.global_state.admission_open,
            })
        except Exception as e:
            logger.warning("phase_broadcast_failed", error=str(e))

    # ── Public read surface ─────────────────────────────────────
    def status(self) -> dict:
        s = self.state
        a = self._last_snap or {}
        app = self._last_app
        return {
            "running": self._running,
            "profile": s.profile,
            "global": {
                "phase":            s.global_state.phase,
                "admission_open":   s.global_state.admission_open,
                "recording_paused": s.global_state.recording_paused,
                "last_trigger":     s.global_state.last_trigger,
                "last_change":      _iso(s.global_state.last_change)
                                    if s.global_state.last_change else None,
            },
            "inputs": {
                "cpu_p95":   round(a.get("cpu_p95", 0), 1),
                "cpu_avg":   round(a.get("cpu_avg", 0), 1),
                "rss_p95":   round(a.get("rss_p95", 0), 1),
                "nic_rx_mbps": round(a.get("nic_rx", 0) / 125_000, 2),
                "nic_tx_mbps": round(a.get("nic_tx", 0) / 125_000, 2),
                "loss_ewma":   round(a.get("loss_ewma", 0), 2),
                "rtt_ewma_ms": round(a.get("rtt_ewma", 0), 1),
                "msg_rate":    round(app.msg_rate if app else 0, 2),
                "active_sockets": app.active_sockets if app else 0,
                "db_p95_ms":   round(app.db_write_p95_ms if app else 0, 1),
            },
            "thresholds": self.policy.thresholds(),
            "rooms": self.room_snapshot(),
            "admission_refusals": s.admission_refusals,
        }

    def set_profile(self, name: str) -> None:
        if name not in PROFILES:
            raise ValueError(f"Unknown profile: {name}")
        self.state.profile = name
        self.policy = Policy(name)

    # ── Room lifecycle (called by call/room services) ───────────
    def register_room(self, room_id: str, kind: str = "chat",
                      participants: int = 0) -> None:
        now = time.time()
        r = self.state.rooms.get(room_id)
        if r:
            r.participants = participants
            r.last_update = now
            r.kind = kind or r.kind
        else:
            self.state.rooms[room_id] = RoomInfo(
                room_id=room_id, kind=kind, participants=participants,
                started_at=now, last_update=now,
                desired_mode="p2p", applied_mode="p2p",
            )

    def update_room(self, room_id: str, **fields) -> None:
        r = self.state.rooms.get(room_id)
        if not r:
            self.register_room(room_id,
                               kind=fields.get("kind", "chat"),
                               participants=fields.get("participants", 0))
            r = self.state.rooms[room_id]
        for k, v in fields.items():
            if hasattr(r, k) and v is not None:
                setattr(r, k, v)
        r.last_update = time.time()

    def unregister_room(self, room_id: str) -> None:
        self.state.rooms.pop(room_id, None)

    def room_snapshot(self) -> list[dict]:
        out = []
        for r in self.state.rooms.values():
            out.append({
                "room_id":      r.room_id,
                "kind":         r.kind,
                "participants": r.participants,
                "started_at":   _iso(r.started_at),
                "last_update":  _iso(r.last_update),
                "applied_mode": r.applied_mode,
                "desired_mode": r.desired_mode,
                "override":     r.override,
                "critical":     r.critical,
                "loss_p95":     r.loss_p95,
                "rtt_p95":      r.rtt_p95,
            })
        out.sort(key=lambda x: x["started_at"], reverse=True)
        return out

    def force_room_mode(self, room_id: str, mode: str,
                        ttl_sec: int = 900, by: str = "operator",
                        reason: str = "") -> bool:
        r = self.state.rooms.get(room_id)
        if not r:
            return False
        r.override = {
            "mode": mode,
            "ttl_until": time.time() + max(1, int(ttl_sec)),
            "by": by,
            "reason": reason[:200],
        }
        return True

    def clear_room_override(self, room_id: str) -> bool:
        r = self.state.rooms.get(room_id)
        if not r or not r.override:
            return False
        r.override = None
        return True

    def set_room_critical(self, room_id: str, critical: bool) -> bool:
        r = self.state.rooms.get(room_id)
        if not r:
            return False
        r.critical = bool(critical)
        return True

    # ── Admission gate (called by request handlers) ─────────────
    def is_admission_allowed(self, kind: str = "room") -> tuple[bool, str]:
        """Return (allowed, reason).

        Called by channel/call creation endpoints before accepting load.
        `frozen` phase refuses everything; `emergency` refuses new calls
        but allows chat. `degraded` allows all but Policy will downgrade
        their media on entry.
        """
        g = self.state.global_state
        phase = g.phase
        if phase == "frozen":
            self.state.admission_refusals += 1
            return False, "server_frozen"
        if phase == "emergency" and kind in ("call", "room.voice", "room.video"):
            self.state.admission_refusals += 1
            return False, "server_emergency_media_blocked"
        if not g.admission_open:
            self.state.admission_refusals += 1
            return False, "admission_closed"
        return True, ""

    def force_exit_emergency(self) -> bool:
        """Operator manual exit from emergency/frozen phase."""
        if self.state.global_state.phase in ("emergency", "frozen"):
            self.state.global_state.phase = "degraded"
            self.state.global_state.last_trigger = "operator.force_exit"
            self.state.global_state.last_change = time.time()
            self.state.global_state.admission_open = True
            # Record audit entry.
            self.audit.record(Decision(
                ts=time.time(), seq=0, kind="policy.decision", scope="global",
                room_id=None, from_state="emergency", to_state="degraded",
                trigger="operator.force_exit", inputs={}, profile=self.state.profile,
                override_active=True,
            ))
            return True
        return False


def _read_operator_caps() -> dict:
    try:
        if _ROLES_FILE.is_file():
            return json.loads(_ROLES_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


async def _gossip_self_to_peers() -> None:
    """Fire-and-forget: POST self load metrics to every registered peer.

    Failures are logged at debug, never raised — a peer that's slow
    or down must not stall the control-plane tick.
    """
    try:
        from app.services.node_registry import get_registry
        reg = get_registry()
        reg.refresh_self_load()          # ← pull fresh values from control plane
        self_id = reg.self_node_id
        peers = [n for n in reg.nodes(include_dead=False) if not n.self_node]
        logger.info("gossip_peers_count", peers=len(peers))
        if not peers:
            return
        # Import httpx lazily so headless deployments without it don't crash.
        try:
            import httpx
        except ImportError:
            logger.warning("gossip_httpx_missing")
            return
        # Build self snapshot once.
        import asyncio
        with_self = next((n for n in reg.nodes() if n.self_node), None)
        if not with_self:
            return
        # Include known_peers so receiver learns about nodes it hasn't
        # seen directly. Bounded to 50 to keep payloads small.
        known = [{"node_id": n.node_id, "host": n.host, "port": n.port}
                 for n in reg.nodes(include_dead=False) if not n.self_node][:50]
        payload = {
            "node_id": self_id,
            "load": {
                "cpu_pct":        with_self.load.cpu_pct,
                "rss_pct":        with_self.load.rss_pct,
                "nic_rx_mbps":    with_self.load.nic_rx_mbps,
                "nic_tx_mbps":    with_self.load.nic_tx_mbps,
                "active_sockets": with_self.load.active_sockets,
                "active_rooms":   with_self.load.active_rooms,
                "active_calls":   with_self.load.active_calls,
                "phase":          with_self.load.phase,
            },
            "known_peers": known,
            # Include self capability so receiver can auto-register us
            # if they don't know about us yet (mesh join on first contact).
            "capability": {
                "cpu_cores": with_self.capability.cpu_cores,
                "ram_gb":    with_self.capability.ram_gb,
                "nic_gbps":  with_self.capability.nic_gbps,
                "disk_ssd":  with_self.capability.disk_ssd,
                "platform":  with_self.capability.platform,
                "version":   with_self.capability.version,
                "host":      with_self.host,
                "port":      with_self.port,
            },
        }
        # Gossip fan-out: pick K random peers instead of all. Keeps message
        # count O(N) per round regardless of cluster size.
        try:
            from app.services.cluster_mesh import get_mesh
            targets = get_mesh().pick_gossip_targets(peers)
        except Exception:
            targets = peers

        async with httpx.AsyncClient(timeout=2.0) as client:
            async def _one(peer):
                try:
                    await client.post(
                        f"http://{peer.host}:{peer.port}/api/admin/placement/gossip",
                        json=payload,
                    )
                except Exception as e:
                    logger.debug("gossip_peer_failed",
                                 peer=peer.node_id, error=str(e))
            await asyncio.gather(*[_one(p) for p in targets],
                                 return_exceptions=True)
    except Exception as e:
        logger.debug("gossip_cycle_failed", error=str(e))
