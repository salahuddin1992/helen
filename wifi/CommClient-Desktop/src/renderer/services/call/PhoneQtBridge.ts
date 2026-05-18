/**
 * PhoneQtBridge — renderer-side consumer for the QuickTime-over-USB pipeline
 * that ``usbQuickTimeHelper.ts`` sets up in the main process. Given raw bulk
 * packets streaming off the phone's IN endpoint, this module:
 *
 *   1. Reassembles the QT wire framing (little-endian length prefix + 4-byte
 *      magic + payload) across IPC chunk boundaries.
 *   2. Drives the handshake state machine so the phone starts emitting A/V:
 *      answering ``ping``, ``syns``, and the common ``asyn`` commands by
 *      writing replies back through ``electronAPI.usbPhone.qt.send``.
 *   3. Extracts H.264 NALUs out of ``sbuf`` sample-buffer async messages,
 *      feeds them to a ``VideoDecoder`` (WebCodecs), and publishes the
 *      decoded VideoFrames via a ``MediaStreamTrackGenerator`` — wrapped in
 *      a ``MediaStream`` and registered in ``useVirtualSourcesStore`` as
 *      ``virtual:phone:qt:<udid>`` with ``transport='usb_tether'``.
 *
 * The QT protocol details we implement are the minimum viable subset
 * reverse-engineered by the ``quicktime_video_hack`` project — they're not
 * documented by Apple. When an unknown packet arrives we log and skip it
 * rather than tearing down the pipe, so an iOS update that adds new
 * messages won't kill the stream entirely.
 *
 * Integration surface:
 *   ``start()``  — subscribe to main-process IPC
 *   ``stop()``   — unsubscribe, tear down every decoder + virtual source
 */

import toast from 'react-hot-toast';
import { VIRTUAL_DEVICE_PREFIX } from './MediaDeviceManager';
import { useVirtualSourcesStore } from '@/stores/virtualSources.store';
import { t } from '@/i18n';

// ── QT wire framing ────────────────────────────────────

/** Every QT packet is prefixed with a little-endian u32 length *of the
 *  whole packet including this prefix*. Sub-2GiB checksum; any packet
 *  claiming more is almost certainly a desync. */
const QT_MIN_PACKET = 8;        // 4-byte length + 4-byte magic minimum
const QT_MAX_PACKET = 16 * 1024 * 1024;

/** Four-character code → u32 as written on the wire (little-endian). */
function fourCC(s: string): number {
  if (s.length !== 4) throw new Error('fourCC needs 4 chars');
  return (s.charCodeAt(0))
    | (s.charCodeAt(1) << 8)
    | (s.charCodeAt(2) << 16)
    | (s.charCodeAt(3) << 24);
}

const MAGIC_PING = fourCC('ping');
const MAGIC_SYNC = fourCC('sync');     // synchronous command, expects reply
const MAGIC_ASYN = fourCC('asyn');     // async notification / sample buffer
const MAGIC_RPLY = fourCC('rply');     // reply packet we send back for sync

// Common asyn/sync sub-command codes (four-char-code fields inside the body).
const CMD_FEED = fourCC('feed');       // "feed me" — ping-alike keepalive
const CMD_EAT  = fourCC('eat!');       // send more samples
const CMD_SBUF = fourCC('sbuf');       // sample buffer (video frame or audio)
const CMD_HPD1 = fourCC('hpd1');       // hotplug discovery: video
const CMD_HPA1 = fourCC('hpa1');       // hotplug discovery: audio
const CMD_NEED = fourCC('need');       // need sample
const CMD_SPRP = fourCC('sprp');       // set property
const CMD_TJMP = fourCC('tjmp');       // time jump
const CMD_CWPA = fourCC('cwpa');       // create work pool audio
const CMD_CVRP = fourCC('cvrp');       // create video receive port
const CMD_AFMT = fourCC('afmt');       // audio format description
const CMD_RELS = fourCC('rels');       // release

// ── H.264 NALU helpers ─────────────────────────────────

/** Parse an avcC "extradata" block to pull out SPS + PPS so we can prepend
 *  them to the first IDR NAL — required for VideoDecoder.configure().
 *  Returns null when the buffer is not a valid avcC. */
function parseAvcC(avcC: Uint8Array): { sps: Uint8Array[]; pps: Uint8Array[]; naluLengthSize: number } | null {
  if (avcC.length < 7 || avcC[0] !== 0x01) return null;
  const naluLengthSize = (avcC[4] & 0x03) + 1;
  let p = 5;
  const numSps = avcC[p++] & 0x1f;
  const sps: Uint8Array[] = [];
  for (let i = 0; i < numSps; i++) {
    if (p + 2 > avcC.length) return null;
    const len = (avcC[p] << 8) | avcC[p + 1];
    p += 2;
    if (p + len > avcC.length) return null;
    sps.push(avcC.subarray(p, p + len));
    p += len;
  }
  if (p >= avcC.length) return null;
  const numPps = avcC[p++];
  const pps: Uint8Array[] = [];
  for (let i = 0; i < numPps; i++) {
    if (p + 2 > avcC.length) return null;
    const len = (avcC[p] << 8) | avcC[p + 1];
    p += 2;
    if (p + len > avcC.length) return null;
    pps.push(avcC.subarray(p, p + len));
    p += len;
  }
  return { sps, pps, naluLengthSize };
}

/** Convert avcC NALUs (length-prefixed) into Annex-B (start-code prefixed)
 *  bytes. VideoDecoder accepts either; Annex-B makes debugging easier. */
function avccToAnnexB(src: Uint8Array, naluLengthSize: number): Uint8Array {
  const startCode = new Uint8Array([0, 0, 0, 1]);
  const parts: Uint8Array[] = [];
  let p = 0;
  while (p + naluLengthSize <= src.length) {
    let len = 0;
    for (let i = 0; i < naluLengthSize; i++) len = (len << 8) | src[p + i];
    p += naluLengthSize;
    if (len === 0 || p + len > src.length) break;
    parts.push(startCode, src.subarray(p, p + len));
    p += len;
  }
  const total = parts.reduce((a, b) => a + b.length, 0);
  const out = new Uint8Array(total);
  let off = 0;
  for (const part of parts) { out.set(part, off); off += part.length; }
  return out;
}

// ── Per-device state ──────────────────────────────────

interface DeviceState {
  udid: string;
  /** Rolling buffer of bytes from the IN endpoint that haven't been framed. */
  inbox: Uint8Array;
  /** Extradata + keyframe state for the H.264 decoder. */
  avcC: ReturnType<typeof parseAvcC> | null;
  decoder: any | null;       // VideoDecoder — typed any because TS lib might miss it
  track: any | null;         // MediaStreamTrackGenerator — same reason
  writer: WritableStreamDefaultWriter<any> | null;
  stream: MediaStream | null;
  /** Monotonic counter for VideoFrame timestamps (microseconds). */
  tsMicros: number;
  /** True once the decoder has been configured with SPS/PPS. */
  configured: boolean;
  /** Logged once so we don't spam the console per-packet. */
  warnedUnknown: Set<number>;
}

const _devices: Map<string, DeviceState> = new Map();
let _unsubFrame: (() => void) | null = null;
let _unsubStatus: (() => void) | null = null;
let _started = false;

function freshState(udid: string): DeviceState {
  return {
    udid,
    inbox: new Uint8Array(0),
    avcC: null,
    decoder: null,
    track: null,
    writer: null,
    stream: null,
    tsMicros: 0,
    configured: false,
    warnedUnknown: new Set(),
  };
}

// ── Packet demuxer ────────────────────────────────────

function appendBytes(a: Uint8Array, b: Uint8Array): Uint8Array {
  const out = new Uint8Array(a.length + b.length);
  out.set(a, 0);
  out.set(b, a.length);
  return out;
}

function readU32LE(buf: Uint8Array, off: number): number {
  return (buf[off]) | (buf[off + 1] << 8) | (buf[off + 2] << 16) | ((buf[off + 3] << 24) >>> 0);
}

/** Drain every complete packet from the rolling buffer and dispatch it to
 *  the state-machine handler. Returns the leftover bytes that belong to the
 *  next (still-incomplete) packet. */
function drainPackets(state: DeviceState): void {
  let buf = state.inbox;
  let cursor = 0;
  while (buf.length - cursor >= 4) {
    const len = readU32LE(buf, cursor);
    if (len < QT_MIN_PACKET || len > QT_MAX_PACKET) {
      // Desync — drop everything and hope the next packet realigns. A real
      // phone never sends a zero-length packet, so this is a safe bail.
      console.warn('[PhoneQtBridge] desync — dropping inbox', { udid: state.udid, len });
      state.inbox = new Uint8Array(0);
      return;
    }
    if (buf.length - cursor < len) break;
    const pkt = buf.subarray(cursor, cursor + len);
    cursor += len;
    try {
      handlePacket(state, pkt);
    } catch (err) {
      console.warn('[PhoneQtBridge] packet handler threw:', (err as Error).message);
    }
  }
  state.inbox = cursor === 0 ? buf : buf.subarray(cursor);
}

/** Dispatch a single fully-framed QT packet to the right handler. */
function handlePacket(state: DeviceState, pkt: Uint8Array): void {
  // [0..4)=length, [4..8)=magic, [8..) body
  const magic = readU32LE(pkt, 4);
  switch (magic) {
    case MAGIC_PING: return handlePing(state, pkt);
    case MAGIC_SYNC: return handleSync(state, pkt);
    case MAGIC_ASYN: return handleAsync(state, pkt);
    case MAGIC_RPLY: return; // we don't issue sync ourselves, so ignore replies
    default:
      if (!state.warnedUnknown.has(magic)) {
        state.warnedUnknown.add(magic);
        console.warn('[PhoneQtBridge] unknown packet magic', magic.toString(16));
      }
  }
}

// ── Handshake: ping / sync / async ────────────────────

/** Reply to a ``ping`` with an identical ``ping`` — that's the protocol. */
function handlePing(state: DeviceState, pkt: Uint8Array): void {
  const reply = new Uint8Array(16);
  const dv = new DataView(reply.buffer);
  dv.setUint32(0, 16, true);      // length
  dv.setUint32(4, MAGIC_PING, true);
  dv.setUint32(8, 1, true);       // version/protocol
  dv.setUint32(12, 0, true);
  sendToPhone(state.udid, reply);
  // Echo is harmless even if the phone ignored it — the reference impl does
  // the same thing. ``pkt`` is unused here but we keep the signature stable
  // so future versions with variable-length ping payloads can inspect it.
  void pkt;
}

/** Sync commands carry a correlation id after the magic. We mirror it in
 *  the ``rply`` so the phone can match request/response. Actual command
 *  semantics (clock creation, format negotiation) are stubbed — we reply
 *  with success/empty and trust the phone to continue. */
function handleSync(state: DeviceState, pkt: Uint8Array): void {
  if (pkt.length < 20) return;
  // Layout: [0..4)=len, [4..8)="sync", [8..12)=cmd, [12..20)=correlation
  const correlation = pkt.subarray(12, 20);
  const reply = new Uint8Array(20);
  const dv = new DataView(reply.buffer);
  dv.setUint32(0, 20, true);
  dv.setUint32(4, MAGIC_RPLY, true);
  reply.set(correlation, 8);
  dv.setUint32(16, 0, true);    // status=OK
  sendToPhone(state.udid, reply);
}

/** Async messages include the real payload we care about — specifically
 *  ``sbuf`` which carries H.264 sample buffers. Everything else we just
 *  acknowledge implicitly by continuing to read. */
function handleAsync(state: DeviceState, pkt: Uint8Array): void {
  if (pkt.length < 20) return;
  // [0..4)=len, [4..8)="asyn", [8..16)=clock-ref, [16..20)=cmd
  const cmd = readU32LE(pkt, 16);
  switch (cmd) {
    case CMD_FEED:
    case CMD_EAT:
    case CMD_NEED:
    case CMD_TJMP:
    case CMD_SPRP:
    case CMD_HPD1:
    case CMD_HPA1:
    case CMD_CWPA:
    case CMD_CVRP:
    case CMD_AFMT:
    case CMD_RELS:
      // Informational / control — no direct action on our side. The phone
      // will keep sending sbuf once hotplug discovery reports a live
      // consumer, which it infers from us continuing to read the pipe.
      return;
    case CMD_SBUF:
      return handleSampleBuffer(state, pkt.subarray(20));
    default:
      if (!state.warnedUnknown.has(cmd)) {
        state.warnedUnknown.add(cmd);
        console.warn('[PhoneQtBridge] unknown asyn cmd', cmd.toString(16));
      }
  }
}

// ── Sample buffer → H.264 → VideoFrame ────────────────

/** Parse the ``sbuf`` payload and push an EncodedVideoChunk into the
 *  decoder. ``sbuf`` is a serialized CMSampleBuffer: a sequence of
 *  (u32 length, 4-char tag, payload) fields. We care about ``nalu`` or
 *  ``sdat`` (the H.264 bytes) and ``idx ``/``pts `` for timestamp, plus
 *  ``fdsc`` (format description) when it arrives with an avcC. */
function handleSampleBuffer(state: DeviceState, body: Uint8Array): void {
  let p = 0;
  let samplePayload: Uint8Array | null = null;
  let formatAvcC: Uint8Array | null = null;
  let keyframe = false;
  while (p + 8 <= body.length) {
    const fieldLen = readU32LE(body, p);
    if (fieldLen < 8 || p + fieldLen > body.length) break;
    const tag = readU32LE(body, p + 4);
    const payload = body.subarray(p + 8, p + fieldLen);
    p += fieldLen;
    // 'nalu' / 'sdat' hold the encoded sample bytes (avcC framing).
    if (tag === fourCC('nalu') || tag === fourCC('sdat')) {
      samplePayload = payload;
      keyframe = keyframe || detectKeyframe(payload);
    } else if (tag === fourCC('fdsc')) {
      // Format description — nested; scan for an 'avcC' atom.
      formatAvcC = extractAvcCFromFdsc(payload);
    } else if (tag === fourCC('idx ') || tag === fourCC('pts ')) {
      // 8-byte timestamp in host nanoseconds. Convert to microseconds for
      // VideoFrame; we fall back to a monotonic counter if it's missing.
      if (payload.length >= 8) {
        const lo = readU32LE(payload, 0);
        const hi = readU32LE(payload, 4);
        const nanos = hi * 0x100000000 + lo;
        state.tsMicros = Math.floor(nanos / 1000);
      }
    }
  }

  if (formatAvcC) {
    const parsed = parseAvcC(formatAvcC);
    if (parsed) {
      state.avcC = parsed;
      reconfigureDecoder(state);
    }
  }

  if (!samplePayload || !state.avcC) return;

  const VideoDecoderCtor = (globalThis as any).VideoDecoder;
  if (!VideoDecoderCtor || !state.decoder) return;

  const EncodedVideoChunkCtor = (globalThis as any).EncodedVideoChunk;
  if (!EncodedVideoChunkCtor) return;

  try {
    // Timestamps must be monotonically non-decreasing. If the phone's clock
    // regressed (rare — it does happen on reboot boundaries) bump ours.
    state.tsMicros = state.tsMicros || Math.floor(performance.now() * 1000);
    const annexB = avccToAnnexB(samplePayload, state.avcC.naluLengthSize);
    const chunk = new EncodedVideoChunkCtor({
      type: keyframe ? 'key' : 'delta',
      timestamp: state.tsMicros,
      data: annexB,
    });
    state.decoder.decode(chunk);
    state.tsMicros += 33_333; // assume ~30fps when the phone doesn't hand us a pts
  } catch (err) {
    console.warn('[PhoneQtBridge] decode failed:', (err as Error).message);
  }
}

/** NAL-unit type 5 is an IDR slice — the only safe restart point for the
 *  decoder. We check the *first* NAL in the buffer because avcC packages
 *  one access unit per sample. */
function detectKeyframe(avccSample: Uint8Array): boolean {
  if (avccSample.length < 5) return false;
  // Skip the 4-byte length prefix (assume length-size=4, the Apple default).
  const nalHeader = avccSample[4];
  const nalType = nalHeader & 0x1f;
  return nalType === 5;
}

/** Walk a format-description block looking for an avcC atom. The
 *  quicktime_video_hack reference calls it a ``CMVideoFormatDescription``
 *  and stores avcC at a variable offset — we scan for the 4-byte "avcC"
 *  tag and take the next field. */
function extractAvcCFromFdsc(fdsc: Uint8Array): Uint8Array | null {
  const avcCTag = fourCC('avcC');
  for (let p = 0; p + 8 <= fdsc.length; p++) {
    if (readU32LE(fdsc, p + 4) === avcCTag) {
      const len = readU32LE(fdsc, p);
      if (len < 8 || p + len > fdsc.length) return null;
      return fdsc.subarray(p + 8, p + len);
    }
  }
  return null;
}

// ── WebCodecs pipeline wiring ─────────────────────────

function reconfigureDecoder(state: DeviceState): void {
  const VideoDecoderCtor = (globalThis as any).VideoDecoder;
  const MSTGCtor = (globalThis as any).MediaStreamTrackGenerator;
  if (!VideoDecoderCtor || !MSTGCtor) {
    console.warn('[PhoneQtBridge] WebCodecs / MediaStreamTrackGenerator unavailable');
    return;
  }

  // First-time setup: create the track generator + MediaStream + register.
  if (!state.track) {
    try {
      state.track = new MSTGCtor({ kind: 'video' });
      state.writer = state.track.writable.getWriter();
      state.stream = new MediaStream([state.track as any]);
      registerVirtualSource(state);
    } catch (err) {
      console.warn('[PhoneQtBridge] track-generator init failed:', (err as Error).message);
      return;
    }
  }

  // Tear down any previous decoder — SPS/PPS may have changed.
  if (state.decoder) {
    try { state.decoder.close(); } catch { /* ignore */ }
    state.decoder = null;
    state.configured = false;
  }

  const decoder = new VideoDecoderCtor({
    output: (frame: any) => {
      try {
        state.writer?.write(frame);
      } catch (err) {
        // Writer closed — track generator has been torn down. Drop the frame.
        try { frame.close(); } catch { /* ignore */ }
        void err;
      }
    },
    error: (err: any) => {
      console.warn('[PhoneQtBridge] decoder error:', err?.message || err);
    },
  });

  try {
    decoder.configure({
      codec: avccCodecString(state.avcC!),
      description: buildAvcCExtradata(state.avcC!),
      optimizeForLatency: true,
    });
    state.decoder = decoder;
    state.configured = true;
  } catch (err) {
    console.warn('[PhoneQtBridge] decoder.configure failed:', (err as Error).message);
    try { decoder.close(); } catch { /* ignore */ }
  }
}

/** Rebuild the avcC extradata block from parsed SPS/PPS arrays — the
 *  VideoDecoder needs the serialized form, not our parsed representation. */
function buildAvcCExtradata(avcC: NonNullable<DeviceState['avcC']>): Uint8Array {
  const sps = avcC.sps[0];
  if (!sps) return new Uint8Array(0);
  let len = 7;
  for (const s of avcC.sps) len += 2 + s.length;
  len += 1;
  for (const p of avcC.pps) len += 2 + p.length;
  const out = new Uint8Array(len);
  out[0] = 0x01;
  out[1] = sps[1];
  out[2] = sps[2];
  out[3] = sps[3];
  out[4] = 0xfc | (avcC.naluLengthSize - 1);
  out[5] = 0xe0 | avcC.sps.length;
  let off = 6;
  for (const s of avcC.sps) {
    out[off++] = (s.length >> 8) & 0xff;
    out[off++] = s.length & 0xff;
    out.set(s, off); off += s.length;
  }
  out[off++] = avcC.pps.length;
  for (const p of avcC.pps) {
    out[off++] = (p.length >> 8) & 0xff;
    out[off++] = p.length & 0xff;
    out.set(p, off); off += p.length;
  }
  return out;
}

/** Derive the Media Capabilities codec string from the SPS profile bytes —
 *  e.g. ``avc1.64001f`` for High@3.1. */
function avccCodecString(avcC: NonNullable<DeviceState['avcC']>): string {
  const sps = avcC.sps[0];
  if (!sps || sps.length < 4) return 'avc1.42E01E'; // Baseline 3.0 fallback
  const profile = sps[1].toString(16).padStart(2, '0');
  const constraints = sps[2].toString(16).padStart(2, '0');
  const level = sps[3].toString(16).padStart(2, '0');
  return `avc1.${profile}${constraints}${level}`;
}

function registerVirtualSource(state: DeviceState): void {
  if (!state.stream) return;
  const add = useVirtualSourcesStore.getState().add;
  const deviceId = `${VIRTUAL_DEVICE_PREFIX}phone:qt:${state.udid}`;
  add({
    deviceId,
    label: 'iPhone (USB direct)',
    kind: 'videoinput',
    stream: state.stream,
    transport: 'usb_tether',
  });
}

function unregisterVirtualSource(udid: string): void {
  const remove = useVirtualSourcesStore.getState().remove;
  try { remove(`${VIRTUAL_DEVICE_PREFIX}phone:qt:${udid}`); } catch { /* ignore */ }
}

// ── Send path ─────────────────────────────────────────

function sendToPhone(udid: string, bytes: Uint8Array): void {
  const api = (globalThis as any).electronAPI?.usbPhone?.qt?.send;
  if (!api) return;
  api(udid, bytes).catch((err: any) => {
    console.warn('[PhoneQtBridge] send failed:', err?.message || err);
  });
}

// ── Public surface ────────────────────────────────────

function tearDownDevice(state: DeviceState): void {
  try { state.decoder?.close(); } catch { /* ignore */ }
  try { state.writer?.close(); } catch { /* ignore */ }
  try { state.track?.stop(); } catch { /* ignore */ }
  unregisterVirtualSource(state.udid);
  _devices.delete(state.udid);
}

export function startPhoneQtBridge(): void {
  if (_started) return;
  const api = (globalThis as any).electronAPI?.usbPhone?.qt;
  if (!api?.onFrame) {
    // Preload didn't expose QT — either we're running outside Electron
    // (Vite dev, Storybook) or the user is on an older build. Bail quietly;
    // the tether path still works.
    return;
  }
  _started = true;

  _unsubFrame = api.onFrame((payload: { udid: string; data: Uint8Array }) => {
    if (!payload?.udid || !payload?.data) return;
    let state = _devices.get(payload.udid);
    if (!state) {
      state = freshState(payload.udid);
      _devices.set(payload.udid, state);
    }
    state.inbox = appendBytes(state.inbox, payload.data);
    drainPackets(state);
  });

  // Drop decoder + virtual source when the main process says the stream is
  // gone (unplug, kernel reset). The status broadcast lists currently-active
  // devices — any UDID *not* in that list has vanished.
  _unsubStatus = api.onStatus((status: { devices: Array<{ udid: string; streaming: boolean }> }) => {
    const live = new Set((status?.devices || []).map((d) => d.udid));
    for (const [udid, state] of Array.from(_devices.entries())) {
      if (!live.has(udid)) {
        try { tearDownDevice(state); } catch { /* ignore */ }
        toast(t('pair.toast_offline'), { icon: '📴' });
      }
    }
  });
}

export function stopPhoneQtBridge(): void {
  if (!_started) return;
  _started = false;
  try { _unsubFrame?.(); } catch { /* ignore */ }
  try { _unsubStatus?.(); } catch { /* ignore */ }
  _unsubFrame = null;
  _unsubStatus = null;
  for (const state of Array.from(_devices.values())) {
    tearDownDevice(state);
  }
}
