# CommClient — Test Scenarios & Edge Cases

## Test Environment Setup
- 2+ Windows machines on same LAN (or VMs with bridged networking)
- Server running on Machine A
- Desktop clients on Machine A + B (+ C for group tests)

---

## 1. Authentication

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 1.1 | Happy login | Enter valid username/password | Redirect to /chats, socket connected, presence "online" |
| 1.2 | Wrong password | Enter wrong password | Error message, no redirect |
| 1.3 | Duplicate session | Login on Machine A, then B | Both connected, presence shows both SIDs |
| 1.4 | Token expiry | Wait >60 min (or reduce JWT_ACCESS_TOKEN_EXPIRE_MINUTES to 1) | Auto-refresh via refresh token, no visible interruption |
| 1.5 | Session restore | Login, close app, reopen | Auto-restores session, no re-login required |
| 1.6 | Logout cleanup | Logout on one machine | Engines destroyed, socket disconnected, presence updated |

## 2. Presence

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 2.1 | Online detection | User A logs in | B sees A as "online" in contacts |
| 2.2 | Offline detection | User A closes app | B sees A as "offline" within ~25s (ping timeout) |
| 2.3 | Multi-tab presence | A opens 2 windows, closes 1 | A stays "online" (partial disconnect) |
| 2.4 | In-call status | A calls B, B accepts | Both show "in_call" status to C |
| 2.5 | Status after hangup | A hangs up | Both revert to "online" |

## 3. Private Messaging

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 3.1 | Send text | A sends "hello" to B | B receives immediately, delivery receipt sent to A |
| 3.2 | Long message | A sends 9999-char message | Delivered correctly, no truncation |
| 3.3 | Message over limit | A sends >10000-char message | Server rejects with error |
| 3.4 | Offline delivery | B is offline, A sends message | When B reconnects, sync_request returns missed message |
| 3.5 | Read receipts | B opens channel with A's messages | A sees "read" status |
| 3.6 | Typing indicator | A starts typing | B sees typing indicator, auto-stops on blur |
| 3.7 | Edit message | A edits a sent message | B sees updated content + "edited" badge |
| 3.8 | Delete message | A deletes a message | B sees "[deleted]" placeholder |
| 3.9 | Reactions | A adds 👍 to B's message | Both see reaction count update |
| 3.10 | Message queue retry | A sends while server is down | Message queued, retries on reconnect, delivered after |
| 3.11 | Rate limit | A sends >10 msgs/sec via script | Server returns "Rate limited" error |

## 4. Group Messaging

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 4.1 | Group send | A sends to group [A,B,C] | B and C both receive |
| 4.2 | Delivery receipts | B and C are online | A gets delivery receipt for both |
| 4.3 | Partial offline | C is offline | A gets delivery for B only; C syncs on reconnect |
| 4.4 | Group typing | A types in group | B and C see typing indicator |

## 5. Private Audio/Video Calls

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 5.1 | Audio call | A calls B (audio) | Ring → Accept → ICE connects → audio flows |
| 5.2 | Video call | A calls B (video) | Ring → Accept → video + audio flows |
| 5.3 | Reject | A calls B, B rejects | A sees "rejected", both back to idle |
| 5.4 | Ring timeout | A calls B, B doesn't answer for 30s | Call auto-ends, both back to idle |
| 5.5 | Hangup by caller | A hangs up during active call | Both back to idle, presence reset |
| 5.6 | Hangup by callee | B hangs up during active call | Both back to idle |
| 5.7 | Mute toggle | A mutes during call | B stops hearing A, participant state updated |
| 5.8 | Video toggle | A disables video | B sees black frame, participant state updated |
| 5.9 | Busy rejection | A calls B while B is in call with C | B auto-rejects A |
| 5.10 | Connect timeout | Block ICE (firewall) | Call ends after 15s with timeout |
| 5.11 | Reconnect on disconnect | Briefly disconnect A's network | State → reconnecting → reconnected (if <30s) |
| 5.12 | Reconnect timeout | Disconnect A's network for >30s | State → reconnecting → ended |
| 5.13 | Quality adaptation | Throttle bandwidth to 100kbps | QualityController downgrades to "low" or "audio-only" |

## 6. Group Audio/Video Calls

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 6.1 | 3-way call | A starts group call, B+C join | Mesh: 2 PeerConnections per user, all hear/see each other |
| 6.2 | Late join | A+B in call, C joins after 10s | C added to mesh, all 3 connected |
| 6.3 | Participant leaves | B leaves group call | A+C stay connected, B removed from mesh |
| 6.4 | Last person | A+B in call, B leaves | A alone → call auto-ends |
| 6.5 | Max participants | Try to join with >8 users | 9th user gets "Call is full" error |
| 6.6 | Disconnect cleanup | B's network drops | Server detects disconnect → removes B → notifies A+C |

## 7. Private Screen Sharing

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 7.1 | Start share | A shares screen during P2P call | B sees A's screen as new video track |
| 7.2 | Stop share | A stops screen share | B's screen video track removed |
| 7.3 | OS stop button | A clicks native "Stop sharing" | onended fires → stopScreenShare called |
| 7.4 | Switch source | A switches from Display 1 to Window | replaceTrack, no renegotiation |
| 7.5 | Cancel permission | A cancels the source picker | Error handled, state stays active |

## 8. Group Screen Sharing

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 8.1 | Presenter request | A requests presenter in group call | Server grants, all notified |
| 8.2 | Presenter queue | A is presenting, B requests | B queued at position 1 |
| 8.3 | Auto-promotion | A stops presenting, B in queue | B auto-promoted, presenter_promoted event |
| 8.4 | Cancel request | B cancels queue request | B removed from queue, queue_update broadcast |
| 8.5 | Force stop | Call initiator force-stops A | A's screen share stopped, presenter_force_stopped event |

## 9. Reconnection & Sync

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 9.1 | Brief disconnect | Pull A's ethernet for 3s | Socket reconnects, sync_request fetches missed messages |
| 9.2 | Extended disconnect | A offline for 2 min | Reconnects, bulk sync, message queue flushes retries |
| 9.3 | Server restart | Restart server while clients connected | Clients see server:shutdown, reconnect after restart |
| 9.4 | Orphan call cleanup | Server restarts mid-call | Orphan cleanup loop removes stale calls after 60s |

## 10. Performance & Stress

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 10.1 | Message flood | Send 100 messages in 10s | All delivered, rate limiter kicks in if >10/s |
| 10.2 | Large file share | Send 90MB file | Upload succeeds, download succeeds |
| 10.3 | Long call | Keep call active for 1+ hours | No memory leak, quality stable |
| 10.4 | Multiple channels | 50 channels with messages | Channel list renders smoothly (virtualized) |
| 10.5 | Concurrent callers | 4 group call participants | Mesh stable, CPU < 80% |

## 11. Security

| # | Scenario | Steps | Expected |
|---|----------|-------|----------|
| 11.1 | Expired JWT | Connect with expired access token | Connection refused |
| 11.2 | Forged signal | Send call_signal to user not in call | Server rejects (unauthorized log) |
| 11.3 | Non-member message | Send message to channel not joined | Server returns "Not a member" |
| 11.4 | Message XSS | Send `<script>alert(1)</script>` | Rendered as text, no execution |
| 11.5 | Oversized payload | Send 11MB socket event | Rejected by max_http_buffer_size |

---

## Debug Commands (Browser Console)

```javascript
// Enable call debug logging
__commclient_call_debug.enable()

// Export call debug log as JSON
copy(__commclient_call_debug.exportJSON())

// Check socket connection
socketManager.isConnected()

// Check call state
useCallStore.getState()

// Check messaging state
useChatStore.getState()
```
