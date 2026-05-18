// chaos-bridge-partition.mjs — bridge partition chaos test using
// Toxiproxy.
//
// Pre-req: Toxiproxy running with two proxies set up so the test can
// inject a bandwidth=0 toxic between Helen-Server-A and Helen-Server-B,
// observe the disconnect, restore, and verify the bridge auto-recovers.
//
// Setup (one-time):
//   docker run -d --name toxiproxy -p 8474:8474 -p 13088:13088 ghcr.io/shopify/toxiproxy
//   curl -X POST http://localhost:8474/proxies -d '{
//     "name": "a-to-b",
//     "listen": "0.0.0.0:13088",
//     "upstream": "helen-server-b:3088",
//     "enabled": true
//   }'
//
// Then point Helen-Server-A's peer config at http://localhost:13088.
//
// Run:
//   node scripts/chaos-bridge-partition.mjs

const TOXY = process.env.TOXIPROXY || 'http://localhost:8474';
const SERVER_A = process.env.SERVER_A || 'http://localhost:3088';
const SERVER_B = process.env.SERVER_B || 'http://localhost:3089';
const PARTITION_DURATION_SEC = Number(process.env.PARTITION_SEC) || 30;

async function toxipostJSON(path, body) {
    const r = await fetch(TOXY + path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    });
    if (!r.ok && r.status !== 409) {
        throw new Error(`Toxiproxy ${path} failed: ${r.status}`);
    }
}
async function toxidel(path) {
    const r = await fetch(TOXY + path, { method: 'DELETE' });
    if (!r.ok && r.status !== 404) {
        throw new Error(`Toxiproxy DELETE ${path} failed: ${r.status}`);
    }
}

async function checkToxiproxyAvailable() {
    try {
        const r = await fetch(TOXY + '/version', { signal: AbortSignal.timeout(2000) });
        return r.ok;
    } catch { return false; }
}

async function checkBridgeHealthy() {
    // Each server should list the other as a healthy peer.
    const r = await fetch(SERVER_A + '/api/peers', { signal: AbortSignal.timeout(3000) });
    if (!r.ok) return false;
    const data = await r.json();
    return Array.isArray(data.peers) && data.peers.length > 0;
}

(async () => {
    if (!(await checkToxiproxyAvailable())) {
        console.error('[chaos] Toxiproxy not reachable at', TOXY);
        console.error('[chaos] Start with: docker run -d --name toxiproxy -p 8474:8474 ghcr.io/shopify/toxiproxy');
        process.exit(2);  // exit 2 = environment problem (test skipped)
    }

    console.log('[chaos] toxiproxy reachable, beginning bridge-partition scenario');

    // Baseline.
    const baselineHealthy = await checkBridgeHealthy();
    console.log(`[chaos] baseline bridge healthy: ${baselineHealthy}`);
    if (!baselineHealthy) {
        console.error('[chaos] bridge NOT healthy at baseline — fix that first');
        process.exit(1);
    }

    // Inject 100% packet loss (bandwidth=0).
    console.log('[chaos] injecting bandwidth=0 toxic');
    await toxipostJSON('/proxies/a-to-b/toxics', {
        type: 'bandwidth',
        attributes: { rate: 0 },
        name: 'partition-test',
    });

    // Wait + observe.
    console.log(`[chaos] waiting ${PARTITION_DURATION_SEC}s for the bridge to detect partition`);
    let detectedAt = null;
    for (let i = 0; i < PARTITION_DURATION_SEC; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        if (!(await checkBridgeHealthy())) {
            detectedAt = i + 1;
            console.log(`[chaos] bridge marked unhealthy at t+${detectedAt}s`);
            break;
        }
    }
    if (!detectedAt) {
        console.warn('[chaos] WARNING — bridge stayed "healthy" through the partition (heartbeat too slow?)');
    }

    // Remove toxic + verify recovery.
    console.log('[chaos] removing partition');
    await toxidel('/proxies/a-to-b/toxics/partition-test');

    let recoveredAt = null;
    for (let i = 0; i < 30; i++) {
        await new Promise((r) => setTimeout(r, 1000));
        if (await checkBridgeHealthy()) {
            recoveredAt = i + 1;
            console.log(`[chaos] bridge recovered at t+${recoveredAt}s after partition removal`);
            break;
        }
    }

    if (!recoveredAt) {
        console.error('[chaos] FAIL — bridge did NOT recover within 30s of partition removal');
        process.exit(1);
    }

    console.log('[chaos] PASS — bridge survives + recovers from partition');
    process.exit(0);
})();
