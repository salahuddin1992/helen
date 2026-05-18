/**
 * Mediasoup worker configuration.
 *
 * Codec list is opinionated for LAN-only use:
 *  - Opus (48k stereo) for audio
 *  - VP8 + H.264 baseline for video (wide hardware compat on Windows)
 *
 * RTCP feedback flags enable FEC, PLI, NACK so network hiccups on WiFi do
 * not cause frozen frames.
 */

'use strict';

const os = require('os');

function _parseIntEnv(name, def) {
  const v = process.env[name];
  if (!v) return def;
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : def;
}

function _listenIps() {
  // LAN-only deployment: announce the host's LAN IPv4 to browser clients so
  // their ICE candidates can actually reach this worker. When bound to 0.0.0.0
  // without an announcedIp, chromium marks the candidate unreachable.
  const announced =
    process.env.MEDIASOUP_ANNOUNCED_IP || _autoDetectLanIp() || '127.0.0.1';
  return [{ ip: '0.0.0.0', announcedIp: announced }];
}

function _autoDetectLanIp() {
  const nets = os.networkInterfaces();
  for (const name of Object.keys(nets)) {
    for (const net of nets[name]) {
      if (net.family === 'IPv4' && !net.internal) return net.address;
    }
  }
  return null;
}

module.exports = {
  // HTTP control API (consumed by Python MediasoupBridge)
  control: {
    host: process.env.MEDIASOUP_CONTROL_HOST || '127.0.0.1',
    port: _parseIntEnv('MEDIASOUP_CONTROL_PORT', 4443),
    token: process.env.MEDIASOUP_CONTROL_TOKEN || null,
  },

  // One worker per CPU, capped. More workers = more CPU parallelism but more
  // cross-worker router pipes when routers for the same call land on different
  // workers. We shard routers by call_id hash — see WorkerPool.
  workers: {
    numWorkers: _parseIntEnv(
      'MEDIASOUP_NUM_WORKERS',
      Math.min(os.cpus().length, 4),
    ),
    rtcMinPort: _parseIntEnv('MEDIASOUP_RTC_MIN_PORT', 40000),
    rtcMaxPort: _parseIntEnv('MEDIASOUP_RTC_MAX_PORT', 49999),
    logLevel: process.env.MEDIASOUP_LOG_LEVEL || 'warn',
    logTags: ['info', 'ice', 'dtls', 'rtp', 'srtp', 'rtcp', 'rtx', 'bwe'],
  },

  router: {
    mediaCodecs: [
      {
        kind: 'audio',
        mimeType: 'audio/opus',
        clockRate: 48000,
        channels: 2,
        parameters: { useinbandfec: 1, usedtx: 1 },
      },
      {
        kind: 'video',
        mimeType: 'video/VP8',
        clockRate: 90000,
        parameters: { 'x-google-start-bitrate': 800 },
        rtcpFeedback: [
          { type: 'nack' },
          { type: 'nack', parameter: 'pli' },
          { type: 'ccm', parameter: 'fir' },
          { type: 'goog-remb' },
          { type: 'transport-cc' },
        ],
      },
      {
        kind: 'video',
        mimeType: 'video/H264',
        clockRate: 90000,
        parameters: {
          'packetization-mode': 1,
          'profile-level-id': '42e01f',
          'level-asymmetry-allowed': 1,
          'x-google-start-bitrate': 800,
        },
        rtcpFeedback: [
          { type: 'nack' },
          { type: 'nack', parameter: 'pli' },
          { type: 'ccm', parameter: 'fir' },
          { type: 'goog-remb' },
          { type: 'transport-cc' },
        ],
      },
    ],
  },

  webRtcTransport: {
    listenIps: _listenIps(),
    initialAvailableOutgoingBitrate: 1_000_000,
    maxIncomingBitrate: 2_500_000,
    enableUdp: true,
    enableTcp: true,
    preferUdp: true,
    // iceConsentTimeout: ensures dead ICE pairs get dropped fast
    iceConsentTimeout: 30,
  },

  // Liveness: if no transport activity for this many seconds, drop router
  routerIdleTimeoutSec: _parseIntEnv('MEDIASOUP_ROUTER_IDLE_SEC', 600),
};
