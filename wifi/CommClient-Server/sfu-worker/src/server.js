/**
 * HTTP control API for the Python MediasoupBridge + frontend consumers.
 *
 * Contract matches app/services/topology_manager.py::MediasoupBridge and is
 * extended with the transport/produce/consume endpoints needed by the
 * MediasoupSFUAdapter on the desktop client.
 *
 *   POST   /routers                         -> create router for call_id
 *   DELETE /routers/:callId                 -> close router
 *   POST   /routers/:callId/transports      -> create WebRtcTransport for a peer
 *   POST   /routers/:callId/transports/:id/connect   -> dtls handshake
 *   POST   /routers/:callId/transports/:id/produce   -> peer starts producing
 *   POST   /routers/:callId/consume         -> another peer consumes a producer
 *   POST   /routers/:callId/consumers/:id/resume     -> unpause consumer
 *   POST   /routers/:callId/consumers/:id/pause      -> pause consumer
 *   POST   /routers/:callId/peers/:peerId/leave      -> cleanup on leave
 *   GET    /healthz
 *   GET    /stats
 *
 * Bearer-token auth via MEDIASOUP_CONTROL_TOKEN.
 */

'use strict';

const fastifyFactory = require('fastify');
const pino = require('pino');
const path = require('path');
const fs = require('fs');
const { spawn } = require('child_process');
const config = require('./config');
const { WorkerPool } = require('./WorkerPool');
const { RouterRegistry } = require('./RouterRegistry');

/**
 * Generate an RTP port pair (RTP + RTCP) inside the configured RTC port range.
 * We keep a module-level counter so successive allocations don't collide.
 */
let _portCursor = null;
function _allocRtpPortPair() {
  const min = config.workers.rtcMinPort;
  const max = config.workers.rtcMaxPort;
  if (_portCursor === null || _portCursor + 4 > max) _portCursor = min;
  const rtp = _portCursor;
  const rtcp = _portCursor + 1;
  _portCursor += 4; // keep headroom (rtp + rtcp + two reserved slots)
  return { rtp, rtcp };
}

/**
 * Recording output directory — configurable via env, defaults to
 * <cwd>/recordings. Created on first write.
 */
const RECORDINGS_DIR = path.resolve(
  process.env.MEDIASOUP_RECORDINGS_DIR || path.join(process.cwd(), 'recordings'),
);
function _ensureRecordingsDir() {
  try {
    fs.mkdirSync(RECORDINGS_DIR, { recursive: true });
  } catch {
    /* ignore — we'll surface the real error on write */
  }
}

async function main() {
  // pino-pretty is a devDependency. Try to use it for pretty dev logs;
  // gracefully fall back to JSON if it isn't installed (production
  // images, the auto-launched bundle from PyInstaller, etc.). Without
  // this guard the worker hard-crashes at boot when only the runtime
  // deps are installed via `npm install --omit=dev`.
  let prettyTransport;
  if (process.env.NODE_ENV !== 'production') {
    try {
      require.resolve('pino-pretty');
      prettyTransport = { target: 'pino-pretty', options: { colorize: true } };
    } catch (_e) {
      prettyTransport = undefined; // JSON output, no transport.
    }
  }
  const logger = pino({
    level: process.env.LOG_LEVEL || 'info',
    transport: prettyTransport,
  });

  const pool = new WorkerPool({ ...config.workers, logger });
  await pool.start();

  const registry = new RouterRegistry({
    workerPool: pool,
    routerOpts: config.router,
    webRtcTransportOpts: config.webRtcTransport,
    idleTimeoutSec: config.routerIdleTimeoutSec,
    logger,
  });
  registry.start();

  const app = fastifyFactory({ logger: false, disableRequestLogging: true });

  // ── Bearer auth ─────────────────────────────────────────────────────────
  app.addHook('onRequest', async (req, reply) => {
    if (req.routerPath === '/healthz') return;
    const expected = config.control.token;
    if (!expected) return; // token not configured -> allow (LAN-only loopback)
    const auth = req.headers['authorization'] || '';
    if (auth !== `Bearer ${expected}`) {
      return reply.code(401).send({ error: 'unauthorized' });
    }
  });

  // ── Health / stats ──────────────────────────────────────────────────────
  app.get('/healthz', async () => ({ ok: true, uptime: process.uptime() }));

  app.get('/stats', async () => ({
    routers: registry._rooms.size,
    total_routers_ever: pool.totalRouters(),
    rooms: Array.from(registry._rooms.values()).map((r) => ({
      call_id: r.callId,
      transports: r.transports.size,
      producers: r.producers.size,
      consumers: r.consumers.size,
      peers: r.peerProducers.size,
      age_sec: Math.floor((Date.now() - r.createdAt) / 1000),
    })),
  }));

  // ── Router lifecycle ────────────────────────────────────────────────────
  app.post('/routers', async (req, reply) => {
    const { call_id } = req.body || {};
    if (!call_id) return reply.code(400).send({ error: 'missing call_id' });
    const room = await registry.getOrCreate(call_id);
    return {
      call_id,
      url: `ws://${config.control.host}:${config.control.port}`, // informative only
      token: null,
      rtp_capabilities: room.router.rtpCapabilities,
      transport_options: {
        // The adapter calls /transports to actually create one — these are
        // metadata for the client to know what this server supports.
        iceServers: [],
      },
    };
  });

  app.delete('/routers/:callId', async (req, reply) => {
    const closed = await registry.closeRoom(req.params.callId);
    return { closed };
  });

  // ── Transport lifecycle ─────────────────────────────────────────────────
  app.post('/routers/:callId/transports', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });

    const { peer_id, direction } = req.body || {};
    if (!peer_id) return reply.code(400).send({ error: 'missing peer_id' });
    if (!['send', 'recv'].includes(direction))
      return reply.code(400).send({ error: 'direction must be send|recv' });

    const transport = await room.router.createWebRtcTransport({
      ...config.webRtcTransport,
      appData: { peerId: peer_id, direction },
    });

    try {
      await transport.setMaxIncomingBitrate(
        config.webRtcTransport.maxIncomingBitrate,
      );
    } catch {
      /* older mediasoup */
    }

    transport.on('dtlsstatechange', (state) => {
      if (state === 'closed' || state === 'failed') {
        try {
          transport.close();
        } catch {}
        room.transports.delete(transport.id);
      }
    });
    transport.observer.on('close', () => {
      room.transports.delete(transport.id);
    });

    room.transports.set(transport.id, transport);
    registry.touch(room.callId);

    return {
      id: transport.id,
      ice_parameters: transport.iceParameters,
      ice_candidates: transport.iceCandidates,
      dtls_parameters: transport.dtlsParameters,
      sctp_parameters: transport.sctpParameters,
      direction,
    };
  });

  app.post('/routers/:callId/transports/:transportId/connect', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const transport = room.transports.get(req.params.transportId);
    if (!transport) return reply.code(404).send({ error: 'transport not found' });

    const { dtls_parameters } = req.body || {};
    if (!dtls_parameters)
      return reply.code(400).send({ error: 'missing dtls_parameters' });

    await transport.connect({ dtlsParameters: dtls_parameters });
    registry.touch(room.callId);
    return { ok: true };
  });

  // ── Produce / Consume ───────────────────────────────────────────────────
  app.post('/routers/:callId/transports/:transportId/produce', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const transport = room.transports.get(req.params.transportId);
    if (!transport) return reply.code(404).send({ error: 'transport not found' });

    const { kind, rtp_parameters, app_data } = req.body || {};
    if (!['audio', 'video'].includes(kind))
      return reply.code(400).send({ error: 'kind must be audio|video' });
    if (!rtp_parameters)
      return reply.code(400).send({ error: 'missing rtp_parameters' });

    const peerId = transport.appData.peerId;
    const producer = await transport.produce({
      kind,
      rtpParameters: rtp_parameters,
      appData: { ...app_data, peerId },
    });

    producer.on('transportclose', () => {
      room.producers.delete(producer.id);
      const set = room.peerProducers.get(peerId);
      if (set) set.delete(producer.id);
    });

    room.producers.set(producer.id, {
      producer,
      peerId,
      transportId: transport.id,
    });
    if (!room.peerProducers.has(peerId)) {
      room.peerProducers.set(peerId, new Set());
    }
    room.peerProducers.get(peerId).add(producer.id);
    registry.touch(room.callId);

    return { id: producer.id, kind, peer_id: peerId };
  });

  app.post('/routers/:callId/consume', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });

    const { transport_id, producer_id, rtp_capabilities, peer_id } = req.body || {};
    if (!transport_id || !producer_id || !rtp_capabilities || !peer_id)
      return reply
        .code(400)
        .send({ error: 'missing transport_id|producer_id|rtp_capabilities|peer_id' });

    const transport = room.transports.get(transport_id);
    if (!transport) return reply.code(404).send({ error: 'transport not found' });
    const producerEntry = room.producers.get(producer_id);
    if (!producerEntry) return reply.code(404).send({ error: 'producer not found' });

    if (!room.router.canConsume({ producerId: producer_id, rtpCapabilities: rtp_capabilities }))
      return reply.code(422).send({ error: 'cannot consume producer' });

    const consumer = await transport.consume({
      producerId: producer_id,
      rtpCapabilities: rtp_capabilities,
      paused: true, // resume once client is ready
      appData: { peerId: peer_id, producerPeerId: producerEntry.peerId },
    });

    consumer.on('transportclose', () => room.consumers.delete(consumer.id));
    consumer.on('producerclose', () => room.consumers.delete(consumer.id));

    room.consumers.set(consumer.id, {
      consumer,
      peerId: peer_id,
      transportId: transport.id,
    });
    registry.touch(room.callId);

    return {
      id: consumer.id,
      producer_id,
      producer_peer_id: producerEntry.peerId,
      kind: consumer.kind,
      rtp_parameters: consumer.rtpParameters,
      type: consumer.type,
      producer_paused: consumer.producerPaused,
    };
  });

  app.post('/routers/:callId/consumers/:consumerId/resume', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const entry = room.consumers.get(req.params.consumerId);
    if (!entry) return reply.code(404).send({ error: 'consumer not found' });
    await entry.consumer.resume();
    return { ok: true };
  });

  app.post('/routers/:callId/consumers/:consumerId/pause', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const entry = room.consumers.get(req.params.consumerId);
    if (!entry) return reply.code(404).send({ error: 'consumer not found' });
    await entry.consumer.pause();
    return { ok: true };
  });

  // ── Consumer simulcast / SVC layer selection ───────────────────────────
  //
  // Client drives this from its downlink bandwidth estimator: pick a lower
  // spatial/temporal layer when the downlink degrades, raise it back when
  // bandwidth recovers. mediasoup honours this immediately on the next
  // keyframe, so the bitrate step is effectively instantaneous.
  app.post('/routers/:callId/consumers/:consumerId/preferred-layers', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const entry = room.consumers.get(req.params.consumerId);
    if (!entry) return reply.code(404).send({ error: 'consumer not found' });

    const { spatial_layer, temporal_layer } = req.body || {};
    if (typeof spatial_layer !== 'number')
      return reply.code(400).send({ error: 'spatial_layer (number) is required' });

    try {
      await entry.consumer.setPreferredLayers({
        spatialLayer: spatial_layer,
        temporalLayer:
          typeof temporal_layer === 'number' ? temporal_layer : undefined,
      });
      return { ok: true };
    } catch (err) {
      return reply.code(400).send({ error: String(err.message || err) });
    }
  });

  app.post('/routers/:callId/consumers/:consumerId/priority', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const entry = room.consumers.get(req.params.consumerId);
    if (!entry) return reply.code(404).send({ error: 'consumer not found' });

    const { priority } = req.body || {};
    if (typeof priority !== 'number' || priority < 1 || priority > 255)
      return reply.code(400).send({ error: 'priority must be 1..255' });
    try {
      await entry.consumer.setPriority(priority);
      return { ok: true };
    } catch (err) {
      return reply.code(400).send({ error: String(err.message || err) });
    }
  });

  // ── Producer pause / resume — wired from the client mute button ────────
  app.post('/routers/:callId/producers/:producerId/pause', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const entry = room.producers.get(req.params.producerId);
    if (!entry) return reply.code(404).send({ error: 'producer not found' });
    try {
      await entry.producer.pause();
      return { ok: true, paused: true };
    } catch (err) {
      return reply.code(400).send({ error: String(err.message || err) });
    }
  });

  app.post('/routers/:callId/producers/:producerId/resume', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const entry = room.producers.get(req.params.producerId);
    if (!entry) return reply.code(404).send({ error: 'producer not found' });
    try {
      await entry.producer.resume();
      return { ok: true, paused: false };
    } catch (err) {
      return reply.code(400).send({ error: String(err.message || err) });
    }
  });

  // ── Transport bitrate caps — REMB / transport-cc input side ────────────
  //
  // Per-transport incoming bitrate ceiling. Defaults in config, but clients
  // can lower this (e.g. user is on metered WiFi) via a call_sfu_set_max_bitrate
  // event. Output bitrate is negotiated by the browser's send-side estimator
  // and we expose setMaxOutgoingBitrate for completeness.
  app.post('/routers/:callId/transports/:transportId/max-incoming-bitrate', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const transport = room.transports.get(req.params.transportId);
    if (!transport) return reply.code(404).send({ error: 'transport not found' });
    const { bitrate } = req.body || {};
    if (typeof bitrate !== 'number' || bitrate <= 0)
      return reply.code(400).send({ error: 'bitrate must be > 0' });
    try {
      await transport.setMaxIncomingBitrate(bitrate);
      return { ok: true };
    } catch (err) {
      return reply.code(400).send({ error: String(err.message || err) });
    }
  });

  app.post('/routers/:callId/transports/:transportId/max-outgoing-bitrate', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const transport = room.transports.get(req.params.transportId);
    if (!transport) return reply.code(404).send({ error: 'transport not found' });
    const { bitrate } = req.body || {};
    if (typeof bitrate !== 'number' || bitrate <= 0)
      return reply.code(400).send({ error: 'bitrate must be > 0' });
    try {
      if (typeof transport.setMaxOutgoingBitrate === 'function') {
        await transport.setMaxOutgoingBitrate(bitrate);
      }
      return { ok: true };
    } catch (err) {
      return reply.code(400).send({ error: String(err.message || err) });
    }
  });

  // ── AudioLevelObserver — active-speaker detection ──────────────────────
  //
  // Creates (once per router) an AudioLevelObserver that emits 'volumes'
  // events whenever the loudest producer changes. The active-speaker
  // decisions are POSTed back to the Python server at a configurable
  // callback URL so the Python side can fan them out via Socket.IO. If
  // MEDIASOUP_EVENT_CALLBACK_URL is unset we still accept the endpoint but
  // silently skip the callback (observer still runs for side-effects).
  async function _ensureAudioObserver(room) {
    if (room.audioObserver && !room.audioObserver.closed) return room.audioObserver;
    room.audioObserver = await room.router.createAudioLevelObserver({
      maxEntries: 1,
      threshold: -70, // dBov
      interval: 500,
    });
    room.audioObserver.on('volumes', (volumes) => {
      if (!volumes || !volumes.length) return;
      const top = volumes[0];
      const producerEntry = room.producers.get(top.producer?.id);
      const payload = {
        type: 'active_speaker',
        call_id: room.callId,
        producer_id: top.producer?.id,
        peer_id: producerEntry?.peerId || null,
        volume: top.volume, // 0..-127 dBov
        ts: Date.now(),
      };
      _postEvent(payload).catch(() => {});
    });
    room.audioObserver.on('silence', () => {
      _postEvent({
        type: 'silence',
        call_id: room.callId,
        ts: Date.now(),
      }).catch(() => {});
    });
    return room.audioObserver;
  }

  app.post('/routers/:callId/audio-observer/ensure', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    try {
      await _ensureAudioObserver(room);
      return { ok: true };
    } catch (err) {
      return reply.code(500).send({ error: String(err.message || err) });
    }
  });

  app.post('/routers/:callId/audio-observer/add', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const { producer_id } = req.body || {};
    if (!producer_id) return reply.code(400).send({ error: 'missing producer_id' });
    const entry = room.producers.get(producer_id);
    if (!entry) return reply.code(404).send({ error: 'producer not found' });
    if (entry.producer.kind !== 'audio')
      return reply.code(400).send({ error: 'producer is not audio' });
    try {
      const obs = await _ensureAudioObserver(room);
      await obs.addProducer({ producerId: producer_id });
      return { ok: true };
    } catch (err) {
      return reply.code(400).send({ error: String(err.message || err) });
    }
  });

  app.post('/routers/:callId/audio-observer/remove', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const { producer_id } = req.body || {};
    if (!producer_id) return reply.code(400).send({ error: 'missing producer_id' });
    try {
      if (room.audioObserver && !room.audioObserver.closed) {
        await room.audioObserver.removeProducer({ producerId: producer_id });
      }
      return { ok: true };
    } catch (err) {
      return reply.code(400).send({ error: String(err.message || err) });
    }
  });

  // ── Recording via PlainRtpTransport + ffmpeg ──────────────────────────
  //
  // We create PlainRtpTransports for the audio and/or video producers and
  // launch an ffmpeg child process that ingests the resulting RTP streams
  // and muxes them to a single .webm file under RECORDINGS_DIR.
  //
  // The server.js stays LAN-only — ffmpeg binds to 127.0.0.1 only.
  app.post('/routers/:callId/recording', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });

    const { audio_producer_id, video_producer_id, recording_id } = req.body || {};
    if (!audio_producer_id && !video_producer_id)
      return reply.code(400).send({ error: 'provide audio_producer_id and/or video_producer_id' });

    _ensureRecordingsDir();
    const recId = recording_id || `rec_${Date.now()}_${Math.random().toString(36).slice(2, 10)}`;
    const outputPath = path.join(RECORDINGS_DIR, `${room.callId}__${recId}.webm`);

    const rec = {
      id: recId,
      callId: room.callId,
      audioProducerId: audio_producer_id || null,
      videoProducerId: video_producer_id || null,
      audioRtpTransport: null,
      videoRtpTransport: null,
      audioConsumer: null,
      videoConsumer: null,
      ffmpegProc: null,
      outputPath,
      startedAt: Date.now(),
    };

    try {
      // Build the SDP fed to ffmpeg. We describe every incoming RTP stream
      // so ffmpeg can demux + transcode.
      const sdpLines = [
        'v=0',
        'o=- 0 0 IN IP4 127.0.0.1',
        's=CommClient-Recording',
        'c=IN IP4 127.0.0.1',
        't=0 0',
      ];

      if (audio_producer_id) {
        const prod = room.producers.get(audio_producer_id);
        if (!prod) throw new Error('audio producer not found');
        const { rtp, rtcp } = _allocRtpPortPair();
        rec.audioRtpTransport = await room.router.createPlainTransport({
          listenIp: { ip: '127.0.0.1' },
          rtcpMux: false,
          comedia: false,
          port: rtp,
          rtcpPort: rtcp,
        });
        rec.audioConsumer = await rec.audioRtpTransport.consume({
          producerId: audio_producer_id,
          rtpCapabilities: room.router.rtpCapabilities,
          paused: true,
        });
        const pt = rec.audioConsumer.rtpParameters.codecs[0].payloadType;
        const ssrc = rec.audioConsumer.rtpParameters.encodings[0].ssrc;
        sdpLines.push(
          `m=audio ${rtp} RTP/AVP ${pt}`,
          'a=rtpmap:' + pt + ' opus/48000/2',
          'a=fmtp:' + pt + ' minptime=10;useinbandfec=1',
          'a=sendonly',
          'a=ssrc:' + ssrc + ' cname:commclient',
        );
      }

      if (video_producer_id) {
        const prod = room.producers.get(video_producer_id);
        if (!prod) throw new Error('video producer not found');
        const { rtp, rtcp } = _allocRtpPortPair();
        rec.videoRtpTransport = await room.router.createPlainTransport({
          listenIp: { ip: '127.0.0.1' },
          rtcpMux: false,
          comedia: false,
          port: rtp,
          rtcpPort: rtcp,
        });
        rec.videoConsumer = await rec.videoRtpTransport.consume({
          producerId: video_producer_id,
          rtpCapabilities: room.router.rtpCapabilities,
          paused: true,
        });
        const codec = rec.videoConsumer.rtpParameters.codecs[0];
        const pt = codec.payloadType;
        const ssrc = rec.videoConsumer.rtpParameters.encodings[0].ssrc;
        const mime = codec.mimeType.toLowerCase();
        const codecName = mime.includes('vp8') ? 'VP8'
          : mime.includes('h264') ? 'H264'
          : mime.includes('vp9') ? 'VP9' : 'VP8';
        sdpLines.push(
          `m=video ${rtp} RTP/AVP ${pt}`,
          `a=rtpmap:${pt} ${codecName}/90000`,
          'a=sendonly',
          `a=ssrc:${ssrc} cname:commclient`,
        );
      }

      const sdpPath = path.join(RECORDINGS_DIR, `${recId}.sdp`);
      fs.writeFileSync(sdpPath, sdpLines.join('\n') + '\n', 'utf8');

      // Launch ffmpeg. Output is webm (matroska + vp8/opus), copy streams
      // where possible so the worker stays CPU-light.
      rec.ffmpegProc = spawn(
        process.env.FFMPEG_PATH || 'ffmpeg',
        [
          '-nostdin',
          '-protocol_whitelist', 'file,rtp,udp',
          '-fflags', '+genpts',
          '-f', 'sdp',
          '-i', sdpPath,
          '-map', '0:a?',
          '-c:a', 'libopus',
          '-map', '0:v?',
          '-c:v', 'copy',
          '-f', 'webm',
          '-y',
          outputPath,
        ],
        { stdio: ['ignore', 'ignore', 'pipe'] },
      );

      rec.ffmpegProc.stderr.on('data', (buf) => {
        logger.debug({ recId, ffmpeg: String(buf).slice(-120) }, 'ffmpeg stderr');
      });
      rec.ffmpegProc.on('exit', (code, sig) => {
        logger.info({ recId, code, sig, outputPath }, 'ffmpeg exited');
        _postEvent({
          type: 'recording_stopped',
          call_id: room.callId,
          recording_id: recId,
          output_path: outputPath,
          exit_code: code,
          ts: Date.now(),
        }).catch(() => {});
      });

      // Wait a short moment before resuming — gives ffmpeg time to open the
      // UDP sockets.
      await new Promise((r) => setTimeout(r, 150));
      if (rec.audioConsumer) await rec.audioConsumer.resume();
      if (rec.videoConsumer) await rec.videoConsumer.resume();

      room.recordings.set(recId, rec);
      registry.touch(room.callId);

      _postEvent({
        type: 'recording_started',
        call_id: room.callId,
        recording_id: recId,
        output_path: outputPath,
        ts: Date.now(),
      }).catch(() => {});

      return {
        ok: true,
        recording_id: recId,
        output_path: outputPath,
      };
    } catch (err) {
      await registry._closeRecording(rec);
      return reply.code(500).send({ error: String(err.message || err) });
    }
  });

  app.delete('/routers/:callId/recording/:recordingId', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    const rec = room.recordings.get(req.params.recordingId);
    if (!rec) return reply.code(404).send({ error: 'recording not found' });
    try {
      await registry._closeRecording(rec);
      room.recordings.delete(req.params.recordingId);
      return { ok: true, output_path: rec.outputPath };
    } catch (err) {
      return reply.code(500).send({ error: String(err.message || err) });
    }
  });

  app.get('/routers/:callId/recordings', async (req, reply) => {
    const room = registry.get(req.params.callId);
    if (!room) return reply.code(404).send({ error: 'router not found' });
    return {
      recordings: Array.from(room.recordings.values()).map((rec) => ({
        recording_id: rec.id,
        audio_producer_id: rec.audioProducerId,
        video_producer_id: rec.videoProducerId,
        output_path: rec.outputPath,
        started_at: rec.startedAt,
      })),
    };
  });

  // ── Callback to Python server for pushed events ────────────────────────
  // Python reads `MEDIASOUP_EVENT_CALLBACK_URL` (defaults to
  // http://127.0.0.1:8000/internal/sfu/events) and verifies a shared token.
  async function _postEvent(payload) {
    const url = process.env.MEDIASOUP_EVENT_CALLBACK_URL
      || 'http://127.0.0.1:8000/internal/sfu/events';
    const token = process.env.MEDIASOUP_EVENT_CALLBACK_TOKEN || '';
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...(token ? { 'X-Sfu-Token': token } : {}),
        },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        logger.debug({ status: res.status, url }, 'event callback non-2xx');
      }
    } catch (err) {
      logger.debug({ err: String(err.message || err) }, 'event callback failed');
    }
  }

  app.post('/routers/:callId/peers/:peerId/leave', async (req, reply) => {
    await registry.removePeer(req.params.callId, req.params.peerId);
    return { ok: true };
  });

  // ── Bind + graceful shutdown ────────────────────────────────────────────
  await app.listen({ host: config.control.host, port: config.control.port });
  logger.info(
    { host: config.control.host, port: config.control.port },
    'SFU control API listening',
  );

  const stop = async (signal) => {
    logger.info({ signal }, 'shutting down');
    try {
      await app.close();
    } catch {}
    try {
      await registry.shutdown();
    } catch {}
    try {
      await pool.shutdown();
    } catch {}
    process.exit(0);
  };
  process.on('SIGINT', () => stop('SIGINT'));
  process.on('SIGTERM', () => stop('SIGTERM'));
}

main().catch((err) => {
  // eslint-disable-next-line no-console
  console.error('SFU worker fatal:', err);
  process.exit(1);
});
