/**
 * WorkerPool — manages N mediasoup workers and shards routers across them.
 *
 * mediasoup runs CPU-bound media processing in a C++ worker subprocess;
 * one worker saturates ~1 CPU core under load. For a LAN call server on a
 * desktop-class CPU with 4-8 cores, we run one worker per core (capped).
 *
 * Sharding: routers for the same call_id land on the same worker so that
 * Transport/Producer/Consumer for one call do not need cross-worker PipeTransports.
 * We use FNV-1a on the call_id for a stable, portable hash.
 */

'use strict';

const mediasoup = require('mediasoup');

class WorkerPool {
  /**
   * @param {object} opts
   * @param {number} opts.numWorkers
   * @param {number} opts.rtcMinPort
   * @param {number} opts.rtcMaxPort
   * @param {string} opts.logLevel
   * @param {string[]} opts.logTags
   * @param {import('pino').Logger} opts.logger
   */
  constructor(opts) {
    this.opts = opts;
    this.logger = opts.logger.child({ component: 'WorkerPool' });
    this._workers = [];
    this._rrIndex = 0; // fallback round-robin
    this._disposed = false;
  }

  async start() {
    const { numWorkers, rtcMinPort, rtcMaxPort, logLevel, logTags } = this.opts;
    // Partition the RTC port range across workers so they do not collide.
    const span = Math.max(
      100,
      Math.floor((rtcMaxPort - rtcMinPort + 1) / numWorkers),
    );

    for (let i = 0; i < numWorkers; i++) {
      const minP = rtcMinPort + i * span;
      const maxP = i === numWorkers - 1 ? rtcMaxPort : minP + span - 1;

      const worker = await mediasoup.createWorker({
        logLevel,
        logTags,
        rtcMinPort: minP,
        rtcMaxPort: maxP,
      });

      worker.on('died', (err) => {
        this.logger.fatal({ err, pid: worker.pid }, 'mediasoup worker died');
        // If a worker dies we leave it out of rotation; the supervisor
        // (systemd / Windows service / PM2) will restart the whole process.
        this._workers = this._workers.filter((w) => w.worker !== worker);
      });

      this._workers.push({ worker, routerCount: 0 });
      this.logger.info(
        { pid: worker.pid, rtcMinPort: minP, rtcMaxPort: maxP, idx: i },
        'worker started',
      );
    }
  }

  /** Pick the worker for a given call_id. Sticky by hash, fallback round-robin. */
  pickWorker(callId) {
    if (this._workers.length === 0) throw new Error('no workers available');
    let idx = this._rrIndex++ % this._workers.length;
    if (callId) idx = _fnv1a(callId) % this._workers.length;
    return this._workers[idx];
  }

  /** Total live routers across all workers. */
  totalRouters() {
    return this._workers.reduce((acc, w) => acc + w.routerCount, 0);
  }

  async shutdown() {
    if (this._disposed) return;
    this._disposed = true;
    await Promise.all(
      this._workers.map(({ worker }) =>
        Promise.resolve()
          .then(() => worker.close())
          .catch(() => {}),
      ),
    );
    this._workers = [];
  }
}

function _fnv1a(str) {
  let h = 0x811c9dc5;
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return h;
}

module.exports = { WorkerPool };
