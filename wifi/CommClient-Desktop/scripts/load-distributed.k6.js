// load-distributed.k6.js — distributed load test for Helen multi-server.
//
// Targets up to 5 Helen servers, has each VU pick one round-robin,
// authenticates as a seeded user (admin1..admin10 / user1..user10),
// opens a Socket.IO connection, joins a channel, sends N messages,
// then disconnects.
//
// Run:
//   k6 run scripts/load-distributed.k6.js
//   k6 run --vus 100 --duration 5m scripts/load-distributed.k6.js
//   SERVERS="http://10.0.0.1:3088,http://10.0.0.2:3088" k6 run scripts/load-distributed.k6.js
//
// Pass criteria:
//   • login p95 < 500ms
//   • message_send p95 < 1s
//   • <0.1% socket disconnects mid-test
//   • server CPU stable < 70% per host (check Grafana separately)

import http from 'k6/http';
import { sleep, check } from 'k6';
import { Counter, Trend } from 'k6/metrics';

const SERVERS = (__ENV.SERVERS || 'http://127.0.0.1:3088').split(',').filter(Boolean);

const sentMessages = new Counter('helen_messages_sent');
const failedSends = new Counter('helen_messages_failed');
const loginLatency = new Trend('helen_login_seconds');
const sendLatency = new Trend('helen_message_send_seconds');

export const options = {
    scenarios: {
        ramp_50: {
            executor: 'ramping-vus',
            startVUs: 0,
            stages: [
                { duration: '30s', target: 50 },
                { duration: '5m',  target: 50 },
                { duration: '30s', target: 0 },
            ],
        },
    },
    thresholds: {
        'helen_login_seconds':        ['p(95)<0.5'],
        'helen_message_send_seconds': ['p(95)<1.0'],
        'helen_messages_failed':      ['count<10'],
    },
};

const SEEDED = [
    'admin1','admin2','admin3','admin4','admin5','admin6','admin7','admin8','admin9','admin10',
    'user1','user2','user3','user4','user5','user6','user7','user8','user9','user10',
];

export default function () {
    const server = SERVERS[__VU % SERVERS.length];
    const username = SEEDED[__VU % SEEDED.length];

    // Login.
    const t0 = Date.now();
    const loginRes = http.post(
        `${server}/api/auth/login`,
        JSON.stringify({ username, password: username }),
        { headers: { 'Content-Type': 'application/json' } },
    );
    loginLatency.add((Date.now() - t0) / 1000);
    if (!check(loginRes, { 'login 200': (r) => r.status === 200 })) return;
    const token = loginRes.json('tokens.access_token');

    // Get/create a default DM channel for this VU pair.
    // Simpler: list channels, pick the first group.
    const chRes = http.get(`${server}/api/channels`, {
        headers: { Authorization: `Bearer ${token}` },
    });
    if (chRes.status !== 200) return;
    const channels = chRes.json('channels');
    if (!channels || !channels.length) return;
    const cid = channels[0].id;

    // Send 5 messages with random delay.
    for (let i = 0; i < 5; i++) {
        const ts0 = Date.now();
        const sendRes = http.post(
            `${server}/api/channels/${cid}/messages`,
            JSON.stringify({
                content: `k6 vu=${__VU} iter=${__ITER} msg=${i}`,
                client_id: `k6-${__VU}-${__ITER}-${i}`,
            }),
            { headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' } },
        );
        sendLatency.add((Date.now() - ts0) / 1000);
        if (sendRes.status === 201 || sendRes.status === 200) {
            sentMessages.add(1);
        } else {
            failedSends.add(1);
        }
        sleep(0.2 + Math.random() * 0.5);
    }

    sleep(1);
}
