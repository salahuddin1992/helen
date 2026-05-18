import React, { useEffect, useMemo, useRef, useState } from 'react';
import QRCode from 'qrcode';
import { Phone, Copy, RefreshCw, CheckCircle, PhoneOff, Smartphone } from 'lucide-react';
import { Modal } from '../common/Modal';
import { api, getBaseUrl } from '@/services/api.client';
import { socketManager } from '@/services/socket.manager';
import { useVirtualSourcesStore } from '@/stores/virtualSources.store';
import { getPhonePairBridge } from '@/services/call/PhonePairBridge';
import { VIRTUAL_DEVICE_PREFIX } from '@/services/call/MediaDeviceManager';
import { t } from '@/i18n';
import toast from 'react-hot-toast';

interface UsbPhoneStatus {
  connected: boolean;
  hostAddress: string | null;
  phoneAddress: string | null;
  interfaceName: string | null;
  mac: string | null;
  since: number;
}

interface QtDevice {
  udid: string;
  product: string;
  vendorId: number;
  productId: number;
  streaming: boolean;
}

interface QtStatus {
  supported: boolean;
  error: string | null;
  devices: QtDevice[];
  lastScan: number;
}

// Replace the hostname in a base URL while preserving scheme + port. Used
// when the iPhone is USB-tethered: the phone can't reach the desktop's
// Wi-Fi IP, but it *can* reach the tether subnet's .2 interface, so the
// QR needs to encode that address instead.
function withHost(base: string, host: string): string {
  try {
    const u = new URL(base);
    u.hostname = host;
    return u.origin;
  } catch {
    return base;
  }
}

interface Props {
  isOpen: boolean;
  onClose: () => void;
}

type Phase = 'idle' | 'loading' | 'ready' | 'paired' | 'live' | 'expired' | 'error';

const PHONE_VIDEO_PREFIX = `${VIRTUAL_DEVICE_PREFIX}phone:`;

export const PairPhoneDialog: React.FC<Props> = ({ isOpen, onClose }) => {
  const [phase, setPhase] = useState<Phase>('idle');
  const [pairPath, setPairPath] = useState<string>(''); // e.g. "/pair?t=..."
  const [qrDataUrl, setQrDataUrl] = useState<string>('');
  const [secondsLeft, setSecondsLeft] = useState<number>(0);
  const [errMsg, setErrMsg] = useState<string>('');
  const [usb, setUsb] = useState<UsbPhoneStatus | null>(null);
  const [qt, setQt] = useState<QtStatus | null>(null);
  const [qtActivating, setQtActivating] = useState<string | null>(null);
  const tickRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const videoRef = useRef<HTMLVideoElement | null>(null);

  // USB subscription lives only while the dialog is open — there's no reason
  // to keep a listener pinned when the user isn't pairing.
  useEffect(() => {
    if (!isOpen) return;
    const usbApi = (window as any).electronAPI?.usbPhone;
    if (!usbApi) return;
    let unsub: (() => void) | null = null;
    usbApi.getStatus?.().then((s: UsbPhoneStatus) => setUsb(s)).catch(() => {});
    try {
      unsub = usbApi.onStatus?.((s: UsbPhoneStatus) => setUsb(s)) || null;
    } catch { /* ignore */ }
    return () => { if (unsub) try { unsub(); } catch { /* ignore */ } };
  }, [isOpen]);

  // QuickTime helper subscription — surfaces directly-attached iPhones the
  // native USB backend can see, so the user can trigger QT streaming if
  // available. Fails silently on installs without the native module.
  useEffect(() => {
    if (!isOpen) return;
    const qtApi = (window as any).electronAPI?.usbPhone?.qt;
    if (!qtApi) return;
    let unsub: (() => void) | null = null;
    qtApi.getStatus?.().then((s: QtStatus) => setQt(s)).catch(() => {});
    try {
      unsub = qtApi.onStatus?.((s: QtStatus) => setQt(s)) || null;
    } catch { /* ignore */ }
    return () => { if (unsub) try { unsub(); } catch { /* ignore */ } };
  }, [isOpen]);

  const activateQt = async (udid: string) => {
    const qtApi = (window as any).electronAPI?.usbPhone?.qt;
    if (!qtApi?.activate) return;
    setQtActivating(udid);
    try {
      const res = await qtApi.activate(udid);
      if (res?.ok) {
        toast.success(t('pair.qt_activated'));
      } else {
        toast.error(res?.error ? `${t('pair.qt_activate_failed')}: ${res.error}` : t('pair.qt_activate_failed'));
      }
    } catch (err: any) {
      toast.error(`${t('pair.qt_activate_failed')}: ${err?.message || String(err)}`);
    } finally {
      setQtActivating(null);
    }
  };

  // Effective pair URL — swap to USB host when an iPhone tether is active.
  const pairUrl = useMemo(() => {
    if (!pairPath) return '';
    const base = usb?.connected && usb.hostAddress
      ? withHost(getBaseUrl(), usb.hostAddress)
      : getBaseUrl();
    return `${base}${pairPath}`;
  }, [pairPath, usb]);

  // Regenerate the QR image whenever the effective URL changes (token
  // refresh OR USB plug/unplug).
  useEffect(() => {
    if (!pairUrl) { setQrDataUrl(''); return; }
    let cancelled = false;
    QRCode.toDataURL(pairUrl, { width: 260, margin: 1, errorCorrectionLevel: 'M' })
      .then((qr) => { if (!cancelled) setQrDataUrl(qr); })
      .catch(() => { if (!cancelled) setQrDataUrl(''); });
    return () => { cancelled = true; };
  }, [pairUrl]);

  const sources = useVirtualSourcesStore((s) => s.sources);
  const phoneVideo = useMemo(
    () =>
      Object.values(sources).find(
        (s) => s.kind === 'videoinput' && s.deviceId.startsWith(PHONE_VIDEO_PREFIX),
      ),
    [sources],
  );

  const stopTicker = () => {
    if (tickRef.current) {
      clearInterval(tickRef.current);
      tickRef.current = null;
    }
  };

  const requestToken = async () => {
    setPhase('loading');
    setErrMsg('');
    setQrDataUrl('');
    setPairPath('');
    stopTicker();
    try {
      const { pair_url_path, expires_in } = await api.requestPairToken();
      setPairPath(pair_url_path);
      setSecondsLeft(expires_in);
      setPhase('ready');
      tickRef.current = setInterval(() => {
        setSecondsLeft((s) => {
          if (s <= 1) {
            stopTicker();
            setPhase('expired');
            return 0;
          }
          return s - 1;
        });
      }, 1000);
    } catch (e: any) {
      setErrMsg(e?.message || String(e));
      setPhase('error');
    }
  };

  const copyUrl = async () => {
    try {
      await navigator.clipboard.writeText(pairUrl);
      toast.success(t('pair.link_copied'));
    } catch {
      toast.error(t('common.error'));
    }
  };

  const disconnect = () => {
    try { getPhonePairBridge().disconnectAll(); } catch (_) { /* ignore */ }
    onClose();
  };

  useEffect(() => {
    if (!isOpen) {
      stopTicker();
      setPhase('idle');
      setPairPath('');
      setQrDataUrl('');
      setSecondsLeft(0);
      return;
    }
    // If a phone is already streaming when the dialog opens, jump straight to
    // the live phase instead of re-requesting a pair token.
    if (getPhonePairBridge().isPhoneLive()) {
      setPhase('live');
      return;
    }
    requestToken();
  }, [isOpen]);

  useEffect(() => {
    if (!isOpen) return;
    const unsubscribe = socketManager.on('pair:completed', (data: any) => {
      stopTicker();
      setPhase('paired');
      toast.success(t('pair.success'));
      void data;
    });
    return unsubscribe;
  }, [isOpen]);

  // Promote to "live" as soon as tracks arrive through the bridge. Also drop
  // back to idle if the phone goes offline while the dialog is open.
  useEffect(() => {
    if (!isOpen) return;
    if (phoneVideo && phase !== 'live') {
      setPhase('live');
      stopTicker();
    } else if (!phoneVideo && phase === 'live') {
      setPhase('idle');
    }
  }, [phoneVideo, phase, isOpen]);

  // Bind incoming MediaStream to the <video> element when in live phase.
  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    if (phase === 'live' && phoneVideo) {
      if (v.srcObject !== phoneVideo.stream) {
        v.srcObject = phoneVideo.stream;
      }
      v.play().catch(() => { /* autoplay blocked — the poster covers it */ });
    } else {
      v.srcObject = null;
    }
  }, [phase, phoneVideo]);

  const showInfoBlock = phase === 'ready' || phase === 'paired' || phase === 'expired' || phase === 'error' || phase === 'loading';

  return (
    <Modal isOpen={isOpen} onClose={onClose} title={t('pair.title')}>
      <div className="flex flex-col items-center gap-4">
        {phase !== 'live' && (
          <p className="text-sm text-gray-400 text-center leading-relaxed">
            {t('pair.description')}
          </p>
        )}

        {showInfoBlock && (
          <div className="w-[260px] h-[260px] bg-white rounded-lg flex items-center justify-center overflow-hidden">
            {phase === 'loading' && (
              <div className="w-6 h-6 border-2 border-gray-400 border-t-transparent rounded-full animate-spin" />
            )}
            {phase === 'ready' && qrDataUrl && (
              <img src={qrDataUrl} alt="Pair QR" className="w-full h-full" />
            )}
            {phase === 'paired' && (
              <CheckCircle className="text-green-500" size={80} />
            )}
            {phase === 'expired' && (
              <div className="text-gray-700 text-sm text-center px-4">{t('pair.expired')}</div>
            )}
            {phase === 'error' && (
              <div className="text-red-600 text-xs text-center px-4">{errMsg}</div>
            )}
          </div>
        )}

        {phase === 'live' && (
          <>
            <div className="text-sm font-medium text-gray-200">{t('pair.preview_title')}</div>
            <div className="w-full aspect-video bg-black rounded-lg overflow-hidden relative">
              <video
                ref={videoRef}
                autoPlay
                playsInline
                muted
                className="w-full h-full object-contain"
              />
              <div className="absolute top-2 left-2 flex items-center gap-1 bg-red-600/90 text-white text-[10px] font-bold uppercase tracking-wider px-2 py-0.5 rounded">
                <span className="w-1.5 h-1.5 bg-white rounded-full animate-pulse" />
                LIVE
              </div>
            </div>
            <p className="text-xs text-gray-500 text-center leading-relaxed">
              {t('pair.preview_hint')}
            </p>
          </>
        )}

        {phase === 'ready' && (
          <>
            <div className="text-xs text-gray-500">
              {t('pair.expires_in')} {secondsLeft}s
            </div>
            {usb?.connected && usb.hostAddress && (
              <div className="w-full flex items-center gap-2 bg-emerald-900/40 border border-emerald-700/40 rounded-md px-3 py-2">
                <Smartphone size={16} className="text-emerald-400 shrink-0" />
                <div className="flex-1 text-[11px] text-emerald-200 leading-snug">
                  {t('pair.usb_detected')}
                </div>
              </div>
            )}
            {qt?.supported && qt.devices.length > 0 && (
              <div className="w-full flex flex-col gap-1 bg-indigo-900/40 border border-indigo-700/40 rounded-md px-3 py-2">
                <div className="text-[11px] text-indigo-200 font-medium">
                  {t('pair.qt_devices_title')}
                </div>
                {qt.devices.map((dev) => (
                  <div key={dev.udid} className="flex items-center gap-2 py-0.5">
                    <code className="flex-1 text-[10px] text-indigo-300 truncate">
                      {dev.product} · {dev.udid.slice(0, 16)}
                    </code>
                    <button
                      onClick={() => activateQt(dev.udid)}
                      disabled={qtActivating === dev.udid || dev.streaming}
                      className="px-2 py-0.5 text-[10px] font-medium bg-indigo-600 hover:bg-indigo-500 text-white rounded disabled:opacity-40"
                    >
                      {dev.streaming
                        ? t('pair.qt_streaming')
                        : qtActivating === dev.udid
                          ? '…'
                          : t('pair.qt_activate')}
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="w-full flex items-center gap-2 bg-surface-800 rounded-md px-3 py-2">
              <Phone size={16} className="text-gray-400 shrink-0" />
              <code className="flex-1 text-xs text-gray-300 truncate">{pairUrl}</code>
              <button
                onClick={copyUrl}
                className="text-gray-400 hover:text-white shrink-0"
                aria-label={t('pair.copy_link')}
                title={t('pair.copy_link')}
              >
                <Copy size={14} />
              </button>
            </div>
            <p className="text-[11px] text-gray-500 text-center leading-snug px-2">
              {t('pair.usb_hint')}
            </p>
          </>
        )}

        {phase === 'paired' && (
          <div className="text-sm text-green-400 text-center">{t('pair.success')}</div>
        )}

        <div className="flex gap-2 w-full">
          {(phase === 'expired' || phase === 'error') && (
            <button
              onClick={requestToken}
              className="flex-1 flex items-center justify-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-500 text-white rounded-md text-sm font-medium"
            >
              <RefreshCw size={14} />
              {t('pair.refresh')}
            </button>
          )}
          {phase === 'live' && (
            <button
              onClick={disconnect}
              className="flex-1 flex items-center justify-center gap-2 px-4 py-2 bg-red-600 hover:bg-red-500 text-white rounded-md text-sm font-medium"
            >
              <PhoneOff size={14} />
              {t('pair.disconnect')}
            </button>
          )}
          <button
            onClick={onClose}
            className="flex-1 px-4 py-2 bg-surface-800 hover:bg-surface-700 text-gray-200 rounded-md text-sm font-medium"
          >
            {phase === 'paired' || phase === 'live' ? t('common.done') : t('common.cancel')}
          </button>
        </div>
      </div>
    </Modal>
  );
};
