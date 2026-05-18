# Group call — minimal WebRTC + Socket.IO

A clean-room reference implementation of a same-room WebRTC mesh call.
Open the same `?room=<id>` URL in N tabs and they all see each other.

```
group-call-app/
├── package.json
├── server.js                — Express + Socket.IO signaling
└── public/
    ├── index.html
    ├── client.js            — peerConnections[socketId] mesh
    └── style.css
```

## Run

```bash
cd group-call-app
npm install
npm start
```

Then open in **two or more tabs**:

```
http://127.0.0.1:3099/?room=test
```

(Pass `?name=Alice` to set a display name, otherwise a random one is
assigned.)

## Why your previous attempt showed "every tab sees only itself"

Four classic bugs collapse onto the same symptom:

1. **Different rooms.** Each tab's `roomId` was randomized or missing,
   so every tab joined its own one-person room. **Fix:** read `?room=`
   from the URL — same query string on every tab → same room.

2. **No signaling server.** Without a shared message bus, an
   `RTCPeerConnection` has no way to deliver its SDP offer to anyone
   else. The client can build the call locally but never connects.
   **Fix:** Socket.IO `offer` / `answer` / `ice-candidate` relay
   between sockets in the same room.

3. **WebRTC glare.** When `tab B` joins a room with `tab A`, both
   sides try to be the offerer simultaneously. Both peer connections
   land in `have-local-offer` and reject each other's offer, so no
   media flows. **Fix:** convention — only the **joiner** offers; the
   existing members create the peer connection (so local tracks are
   attached and ICE starts) but wait for the joiner's offer.

4. **Media-vs-signaling race.** The most insidious one — and the one
   that survived all three earlier fixes. If the page emits
   `join-room` *before* `getUserMedia` resolves, every
   `RTCPeerConnection` is created with `localStream === null`, so no
   tracks are attached. The SDP offers carry no media, ICE still
   completes (`pc.connectionState='connected'`), but the remote
   `ontrack` never fires. Each tab keeps showing only its own tile
   even though the connection looks healthy. **Fix:** gate the
   `join-room` emit behind both `socket.connected` *and*
   `_mediaResolved`; whichever finishes second triggers the join.

This client follows that convention strictly:

```js
socket.on('joined-room', ({ existing }) => {
    // I just joined → I offer to everyone already in.
    for (const peer of existing) { /* createPeer + createOffer + send */ }
});
socket.on('user-joined', (peer) => {
    // Someone joined after me → prep pc, wait for their offer.
    createPeer(peer.socketId, peer);
});
```

## Console logs

The client prints every signaling step so you can verify the flow:

```
[joined room] roomId=test name=Sand-742 sid=zrx0F2
[user joined] Pine-310 (Vw7L9R)
[offer sent] → Pine-310 (Vw7L9R)
[offer received] ← Pine-310 (Vw7L9R)
[answer sent] → Pine-310 (Vw7L9R)
[answer received] ← Vw7L9R
[ice candidate received] ← Vw7L9R
[remote stream added] Pine-310 (Vw7L9R)
[pc:Vw7L9R] state=connected
[user left] Pine-310 (Vw7L9R)
```

## Grid sizing — `updateGridLayout(count)`

| count | columns | shape          |
|-------|---------|----------------|
| 1     | 1       | full screen    |
| 2     | 2       | side-by-side   |
| 3-4   | 2       | 2×2            |
| 5-9   | 3       | 3×N            |
| 10-16 | 4       | 4×N            |
| 17+   | ⌈√n⌉    | square-ish     |

`grid-auto-rows: 1fr` (in CSS) gives every row the same height, so
every tile gets equal area regardless of N.

## Local multi-tab testing on a single laptop

A single physical webcam can only be opened by one process at a time.
If you launch four Chrome windows on one laptop, only the first will
get the real camera; the others hit `NotReadableError` and silently
fall back to audio-only. The client logs this clearly and continues
the call (audio still flows).

To give every Chrome profile its own synthetic camera so all tabs get
video, launch Chrome with:

```
chrome.exe --use-fake-ui-for-media-stream ^
           --use-fake-device-for-media-stream ^
           --user-data-dir=%TEMP%\room-1 ^
           http://127.0.0.1:3099/?room=test
```

(Repeat with `--user-data-dir=%TEMP%\room-2` etc.)

In production — across actual devices — neither flag is needed.

## What the server does NOT do

- The server **never sees media**. It only relays SDP and ICE.
- No persistence, no auth, no encryption beyond the browser's WebRTC
  default DTLS-SRTP. Add a TURN server to `RTC_CONFIG.iceServers` for
  cross-NAT calls.

## Port

Defaults to `3099` (so it doesn't clash with Helen-Server on `3088`).
Override with `PORT=8080 npm start`.
