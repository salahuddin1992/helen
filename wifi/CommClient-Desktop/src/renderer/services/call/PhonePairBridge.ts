/**
 * PhonePairBridge — receives WebRTC tracks from a paired phone (Safari on
 * iPhone/Android) and registers them as virtual devices in MediaDeviceManager.
 *
 * Signaling is relayed by the server via Socket.IO (`pair:signal`). Actual
 * media flows P2P over ICE, typically LAN-direct. No mediasoup involvement —
 * the desktop just receives the tracks, then plays or republishes them
 * through its normal call pipeline as if they were a local camera.
 */

import toast from 'react-hot-toast';
import { socketManager } from '../socket.manager';
import { VIRTUAL_DEVICE_PREFIX } from './MediaDeviceManager';
import { useVirtualSourcesStore } from '@/stores/virtualSources.store';
import { useSettingsStore } from '@/stores/settings.store';
import { useCallStore } from '@/stores/call.store.v2';
import { t } from '@/i18n';

/** Call statuses during which a mid-flight input swap should be propagated
 *  to peers. Outside of these, swapping only updates the device selection
 *  and waits for the next call start. */
const LIVE_CALL_STATUSES = new Set(['connecting', 'active', 'reconnecting']);

interface PhoneLink {
  phoneSid: string;
  pc: RTCPeerConnection;
  deviceId: string;           // virtual device id for the video track
  deviceIdAudio?: string;     // virtual device id for the audio track
  stream: MediaStream;
  label: string;
  transport: 'usb_tether' | 'wifi';
  announced: boolean;         // toasted "live" once
}

const ICE_SERVERS: RTCIceServer[] = [
  { urls: 'stun:stun.l.google.com:19302' },
];

export class PhonePairBridge {
  private _links: Map<string, PhoneLink> = new Map(); // phone_sid → link
  private _unsubs: Array<() => void> = [];
  private _started = false;

  start(): void {
    if (this._started) return;
    this._started = true;
    this._unsubs.push(
      socketManager.on('pair:phone_ready', (payload: any) => this._onPhoneReady(payload)),
    );
    this._unsubs.push(
      socketManager.on('pair:signal', (payload: any) => this._onSignal(payload)),
    );
    this._unsubs.push(
      socketManager.on('pair:phone_offline', (payload: any) => this._onPhoneOffline(payload)),
    );
  }

  stop(): void {
    this._started = false;
    for (const u of this._unsubs) { try { u(); } catch (_) {} }
    this._unsubs = [];
    for (const phoneSid of Array.from(this._links.keys())) {
      this._tearDown(phoneSid);
    }
  }

  /** Called when server notifies us a phone is online. We don't initiate —
   *  the phone sends the offer. The transport hint is cached so it can be
   *  attached to the link when the offer arrives. */
  private _pendingTransport: Map<string, 'usb_tether' | 'wifi'> = new Map();
  // Deferred tear-down timers per phoneSid — gives momentary
  // 'disconnected' state up to 10s to self-heal before we wipe the
  // link. Audit fix.
  private _disconnectTimers: Map<string, ReturnType<typeof setTimeout>> = new Map();
  private _onPhoneReady(payload: { phone_sid: string; label?: string; transport?: string }): void {
    if (!payload?.phone_sid) return;
    const transport: 'usb_tether' | 'wifi' =
      payload.transport === 'usb_tether' ? 'usb_tether' : 'wifi';
    this._pendingTransport.set(payload.phone_sid, transport);
  }

  private async _onSignal(payload: { from_sid: string; from_device?: string; signal: any }): Promise<void> {
    const { from_sid, signal } = payload || ({} as any);
    if (!from_sid || !signal) return;

    let link = this._links.get(from_sid);
    if (!link) {
      if (signal.type !== 'offer') return; // ignore stray ICE before offer
      link = this._createLink(from_sid);
    }

    try {
      if (signal.type === 'offer') {
        await link.pc.setRemoteDescription(new RTCSessionDescription({ type: 'offer', sdp: signal.sdp }));
        const answer = await link.pc.createAnswer();
        await link.pc.setLocalDescription(answer);
        try {
          await socketManager.emit('pair:signal', {
            target_sid: from_sid,
            signal: { type: 'answer', sdp: answer.sdp },
          });
        } catch (err: any) {
          // Server rejects the answer when another desktop already owns the
          // phone's pair session — tear down silently so the remaining
          // desktop keeps the stream to itself.
          if (err?.message === 'not_claimed') {
            this._tearDown(from_sid, { silent: true });
            return;
          }
          throw err;
        }
      } else if (signal.type === 'ice' && signal.candidate) {
        try { await link.pc.addIceCandidate(signal.candidate); } catch (e) {
          console.warn('[PhonePairBridge] addIceCandidate failed:', e);
        }
      }
    } catch (e) {
      console.warn('[PhonePairBridge] signal handling failed', e);
    }
  }

  private _createLink(phoneSid: string): PhoneLink {
    const pc = new RTCPeerConnection({ iceServers: ICE_SERVERS, bundlePolicy: 'max-bundle' });
    const stream = new MediaStream();
    const deviceId = `${VIRTUAL_DEVICE_PREFIX}phone:${phoneSid}`;
    const transport = this._pendingTransport.get(phoneSid) || 'wifi';
    this._pendingTransport.delete(phoneSid);
    const label = transport === 'usb_tether' ? 'Phone camera (USB)' : 'Phone camera';
    const link: PhoneLink = { phoneSid, pc, deviceId, stream, label, transport, announced: false };
    this._links.set(phoneSid, link);

    pc.onicecandidate = (e) => {
      if (!e.candidate) return;
      socketManager
        .emit('pair:signal', {
          target_sid: phoneSid,
          signal: { type: 'ice', candidate: e.candidate.toJSON() },
        })
        .catch((err: any) => {
          if (err?.message === 'not_claimed') {
            this._tearDown(phoneSid, { silent: true });
          }
        });
    };

    pc.ontrack = (e) => {
      // Append incoming tracks to our composite stream, then make sure the
      // virtual source reflects the current tracks. We register once per
      // kind — a single deviceId covers both audio + video for this phone.
      for (const t of e.streams[0]?.getTracks() || [e.track]) {
        if (!stream.getTracks().includes(t)) stream.addTrack(t);
      }
      this._registerOrRefresh(link);
    };

    pc.onconnectionstatechange = () => {
      // Audit fix: previous code only handled 'failed'/'closed'. A
      // brief WiFi blip moves the PC to 'disconnected' for several
      // seconds before either recovering or escalating to 'failed'.
      // If the underlying transport doesn't recover, we'd be stuck
      // in 'disconnected' forever — half-alive PC blocking the next
      // pair attempt. Schedule a deferred tear-down so genuine
      // momentary blips can self-heal but extended outages don't
      // strand the link.
      const s = pc.connectionState;
      if (s === 'failed' || s === 'closed') {
        this._tearDown(phoneSid);
      } else if (s === 'disconnected') {
        // Give ICE 10s to recover before we tear down. If the state
        // moves back to 'connected' or 'completed' the timer is
        // cancelled in the next state-change.
        if (this._disconnectTimers.has(phoneSid)) return;
        const timer = setTimeout(() => {
          this._disconnectTimers.delete(phoneSid);
          if (pc.connectionState === 'disconnected') {
            this._tearDown(phoneSid);
          }
        }, 10_000);
        this._disconnectTimers.set(phoneSid, timer);
      } else if (s === 'connected') {
        // RTCPeerConnectionState union (per spec) is: 'new' |
        // 'connecting' | 'connected' | 'disconnected' | 'failed' |
        // 'closed'. There's no 'completed' on connectionState (that's
        // on iceConnectionState). 'connected' is the recovery signal.
        const t = this._disconnectTimers.get(phoneSid);
        if (t) {
          clearTimeout(t);
          this._disconnectTimers.delete(phoneSid);
        }
      }
    };

    return link;
  }

  private _registerOrRefresh(link: PhoneLink): void {
    const hasVideo = link.stream.getVideoTracks().length > 0;
    const hasAudio = link.stream.getAudioTracks().length > 0;
    const add = useVirtualSourcesStore.getState().add;
    if (hasVideo) {
      add({ deviceId: link.deviceId, label: link.label, kind: 'videoinput', stream: link.stream, transport: link.transport });
    }
    if (hasAudio) {
      const audioId = `${link.deviceId}:audio`;
      link.deviceIdAudio = audioId;
      add({ deviceId: audioId, label: link.label + ' (mic)', kind: 'audioinput', stream: link.stream, transport: link.transport });
    }
    if (!link.announced && (hasVideo || hasAudio)) {
      link.announced = true;
      this._announceLive(link);
    }
  }

  /** Notify the user the phone is now streaming + offer one-tap switch, and
   *  if a call is live right now, swap the upstream track so remote
   *  participants (same server + federated) start seeing the phone camera
   *  without the user having to end and rejoin the call. */
  private _announceLive(link: PhoneLink): void {
    const liveMsg = link.transport === 'usb_tether'
      ? t('pair.toast_live_usb')
      : t('pair.toast_live');
    toast.success(liveMsg, { duration: 8000 });

    const settings = useSettingsStore.getState();
    const currentVideoInput = settings.settings.videoInputDevice || '';
    const hasVideo = link.stream.getVideoTracks().length > 0;

    // USB-preference rule: if a USB-tethered phone comes online while the
    // user is currently on a Wi-Fi phone camera, promote the USB feed —
    // USB avoids encoder contention with everything else sharing the
    // wireless link and gives far more consistent latency.
    const currentSelected = currentVideoInput
      ? this._linkForDeviceId(currentVideoInput)
      : null;
    const shouldPromote = hasVideo
      && link.transport === 'usb_tether'
      && currentSelected
      && currentSelected.transport === 'wifi'
      && currentSelected !== link;

    if (!currentVideoInput && hasVideo) {
      settings.update({ videoInputDevice: link.deviceId });
      toast(t('pair.toast_auto_selected'), { icon: '📷' });
    } else if (shouldPromote) {
      settings.update({ videoInputDevice: link.deviceId });
      toast(t('pair.toast_usb_preferred'), { icon: '⚡' });
    }

    // Mid-call swap: CallEngine.switchVideoInput/Audio routes through the
    // mediasoup producer's replaceTrack so every remote peer — including
    // ones on federated servers — receives the phone feed immediately.
    // Re-read settings: ``update`` above may have just changed the selected
    // device, and we want the current authoritative choice here.
    const freshSettings = useSettingsStore.getState().settings;
    const call = useCallStore.getState();
    if (LIVE_CALL_STATUSES.has(call.status)) {
      if (hasVideo && freshSettings.videoInputDevice === link.deviceId) {
        call.switchVideoInput(link.deviceId).catch((err) =>
          console.warn('[PhonePairBridge] mid-call video swap failed:', err),
        );
      }
      if (link.deviceIdAudio && freshSettings.audioInputDevice === link.deviceIdAudio) {
        call.switchAudioInput(link.deviceIdAudio).catch((err) =>
          console.warn('[PhonePairBridge] mid-call audio swap failed:', err),
        );
      }
    }
  }

  private _onPhoneOffline(payload: { phone_sid: string }): void {
    if (!payload?.phone_sid) return;
    this._tearDown(payload.phone_sid);
  }

  /** Close and remove a phone link. ``silent`` suppresses the UI toast and
   *  the device-default reset — used when a rival desktop has claimed this
   *  phone so the losing desktop disappears quietly. */
  private _tearDown(phoneSid: string, opts: { silent?: boolean } = {}): void {
    const link = this._links.get(phoneSid);
    if (!link) return;
    this._links.delete(phoneSid);
    this._pendingTransport.delete(phoneSid);
    const remove = useVirtualSourcesStore.getState().remove;
    try { remove(link.deviceId); } catch (_) {}
    if (link.deviceIdAudio) {
      try { remove(link.deviceIdAudio); } catch (_) {}
    }
    try { link.pc.close(); } catch (_) {}

    if (!opts.silent) {
      // If the user was using the phone as their camera, drop back to
      // default. If a call is live right now, also flip the producer's
      // track so remote peers don't freeze on the last phone frame.
      const settings = useSettingsStore.getState();
      const call = useCallStore.getState();
      const inCall = LIVE_CALL_STATUSES.has(call.status);

      if (settings.settings.videoInputDevice === link.deviceId) {
        settings.update({ videoInputDevice: '' });
        if (inCall) {
          call.switchVideoInput('').catch((err) =>
            console.warn('[PhonePairBridge] mid-call video fallback failed:', err),
          );
        }
      }
      if (link.deviceIdAudio && settings.settings.audioInputDevice === link.deviceIdAudio) {
        settings.update({ audioInputDevice: 'default' });
        if (inCall) {
          call.switchAudioInput('default').catch((err) =>
            console.warn('[PhonePairBridge] mid-call audio fallback failed:', err),
          );
        }
      }

      if (link.announced) {
        toast(t('pair.toast_offline'), { icon: '📴' });
      }
    }
  }

  /** Reverse-lookup the link that owns a given virtual deviceId (video OR
   *  audio variant). Used by the USB-preference promotion logic. */
  private _linkForDeviceId(deviceId: string): PhoneLink | null {
    for (const link of this._links.values()) {
      if (link.deviceId === deviceId || link.deviceIdAudio === deviceId) {
        return link;
      }
    }
    return null;
  }

  /** Expose current links so UI can reflect "phone live" state. */
  isPhoneLive(phoneSid?: string): boolean {
    if (phoneSid) return this._links.has(phoneSid);
    return this._links.size > 0;
  }

  getLinkStream(phoneSid: string): MediaStream | null {
    return this._links.get(phoneSid)?.stream || null;
  }

  disconnectAll(): void {
    for (const phoneSid of Array.from(this._links.keys())) {
      this._tearDown(phoneSid);
    }
  }
}

// Singleton — the phone bridge is global. It starts once the user is logged
// in and the socket is connected; see initPhonePairBridge().
let _singleton: PhonePairBridge | null = null;
export function getPhonePairBridge(): PhonePairBridge {
  if (!_singleton) _singleton = new PhonePairBridge();
  return _singleton;
}
