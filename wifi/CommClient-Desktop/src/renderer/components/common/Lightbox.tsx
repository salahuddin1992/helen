/**
 * Lightbox — minimal full-size image viewer mounted at the app root.
 * Other components (MessageBubble, Gallery, ProfilePhoto modal, etc.)
 * push a request via the `openLightbox()` event API; the singleton
 * listens and renders the full-size image until the user dismisses
 * with click / Esc.
 *
 * No external state library — uses a window-level CustomEvent so any
 * component can trigger it without importing the store. Keeps the
 * lightbox completely lazy: zero render cost when nothing is open.
 */
import React, { useCallback, useEffect, useState } from 'react';
import { X, ZoomIn, ZoomOut, Download as DownloadIcon } from 'lucide-react';

interface OpenDetail {
    src: string;
    alt?: string;
    downloadName?: string;
}

const EVT = 'commclient:lightbox-open';

/**
 * openLightbox — call from anywhere (e.g. an image's onClick) to show
 * a full-size view. Pass a `src` URL the renderer can load directly
 * (already-authenticated blob URL or a server endpoint that doesn't
 * require auth headers — the <img> tag can't carry a Bearer header).
 */
export function openLightbox(detail: OpenDetail): void {
    window.dispatchEvent(new CustomEvent<OpenDetail>(EVT, { detail }));
}

export const Lightbox: React.FC = () => {
    const [open, setOpen] = useState(false);
    const [detail, setDetail] = useState<OpenDetail | null>(null);
    const [zoom, setZoom] = useState(1);
    // Track image load failures separately so the user gets a real
    // error instead of an eternal blank black overlay. Without this,
    // a 404 / CORS reject / expired blob URL just leaves an empty
    // <img> with no fallback UI.
    const [imageError, setImageError] = useState<string | null>(null);

    useEffect(() => {
        const onOpen = (e: Event) => {
            const ce = e as CustomEvent<OpenDetail>;
            if (!ce.detail?.src) return;
            setDetail(ce.detail);
            setZoom(1);
            setImageError(null);
            setOpen(true);
        };
        window.addEventListener(EVT, onOpen);
        return () => window.removeEventListener(EVT, onOpen);
    }, []);

    const close = useCallback(() => {
        setOpen(false);
        setDetail(null);
        setZoom(1);
        setImageError(null);
    }, []);

    useEffect(() => {
        if (!open) return;
        const onKey = (e: KeyboardEvent) => {
            if (e.key === 'Escape') close();
            else if (e.key === '+' || e.key === '=') setZoom((z) => Math.min(z + 0.25, 4));
            else if (e.key === '-') setZoom((z) => Math.max(z - 0.25, 0.25));
            else if (e.key === '0') setZoom(1);
        };
        window.addEventListener('keydown', onKey);
        return () => window.removeEventListener('keydown', onKey);
    }, [open, close]);

    if (!open || !detail) return null;

    const wheel = (e: React.WheelEvent) => {
        e.stopPropagation();
        // ctrl+wheel zooms; bare wheel scrolls (so a tall image is pannable).
        if (e.ctrlKey) {
            const dz = e.deltaY > 0 ? -0.1 : 0.1;
            setZoom((z) => Math.max(0.25, Math.min(4, z + dz)));
        }
    };

    return (
        <div
            className="fixed inset-0 z-[200] bg-black/90 flex items-center justify-center select-none"
            onClick={close}
            onWheel={wheel}
        >
            <button
                onClick={(e) => { e.stopPropagation(); close(); }}
                className="absolute top-4 right-4 p-2 bg-black/50 text-white rounded-full hover:bg-black/70"
                title="Close (Esc)"
            >
                <X size={20} />
            </button>
            <div className="absolute top-4 left-1/2 -translate-x-1/2 flex gap-2 bg-black/50 rounded-full px-2 py-1">
                <button
                    onClick={(e) => { e.stopPropagation(); setZoom((z) => Math.max(z - 0.25, 0.25)); }}
                    className="p-2 text-white hover:bg-white/10 rounded-full"
                    title="Zoom out (-)"
                >
                    <ZoomOut size={16} />
                </button>
                <span className="text-xs text-white px-2 py-2 font-mono select-text">
                    {Math.round(zoom * 100)}%
                </span>
                <button
                    onClick={(e) => { e.stopPropagation(); setZoom((z) => Math.min(z + 0.25, 4)); }}
                    className="p-2 text-white hover:bg-white/10 rounded-full"
                    title="Zoom in (+)"
                >
                    <ZoomIn size={16} />
                </button>
                <a
                    href={detail.src}
                    download={detail.downloadName || true}
                    onClick={(e) => e.stopPropagation()}
                    className="p-2 text-white hover:bg-white/10 rounded-full inline-flex items-center"
                    title="Download"
                >
                    <DownloadIcon size={16} />
                </a>
            </div>
            {imageError ? (
                <div
                    className="flex flex-col items-center gap-3 p-6 bg-black/60 rounded-lg text-white max-w-md"
                    onClick={(e) => e.stopPropagation()}
                >
                    <X size={36} className="text-red-400" />
                    <div className="text-base font-semibold">تعذّر تحميل الصورة</div>
                    <div className="text-xs text-gray-400 break-all text-center">{imageError}</div>
                    <button
                        onClick={() => { setImageError(null); /* triggers re-render of <img> */ }}
                        className="mt-2 px-3 py-1.5 text-xs bg-blue-600 hover:bg-blue-500 rounded transition-colors"
                    >
                        إعادة المحاولة
                    </button>
                </div>
            ) : (
                <img
                    src={detail.src}
                    alt={detail.alt || 'Image'}
                    onClick={(e) => e.stopPropagation()}
                    onError={() => setImageError('تأكد أن الملف لم يُحذف وأن جلسة الدخول لم تنتهِ.')}
                    style={{ transform: `scale(${zoom})`, transition: 'transform 120ms ease-out' }}
                    className="max-w-[92vw] max-h-[88vh] object-contain"
                    draggable={false}
                />
            )}
        </div>
    );
};

export default Lightbox;
