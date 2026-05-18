/**
 * BackgroundImagePicker — file input + preset palette for the
 * custom virtual-background feature.
 *
 * Users can:
 *   - Pick a local image file (decoded into a data URL).
 *   - Choose from 6 built-in solid-color backgrounds (no asset
 *     downloads — generated client-side on demand).
 *
 * The chosen image is loaded into VideoEffectPipeline; selecting
 * any image also flips ``videoEffect`` to 'image' so the user
 * sees the change immediately.
 */

import React, { useRef, useState } from 'react';
import { useCallStore } from '@/stores/call.store.v2';

const PRESET_GRADIENTS: Array<{ name: string; from: string; to: string }> = [
  { name: 'بحر',   from: '#1e3a8a', to: '#67e8f9' },
  { name: 'غروب', from: '#7c2d12', to: '#fbbf24' },
  { name: 'غابة',  from: '#14532d', to: '#86efac' },
  { name: 'وردي',  from: '#831843', to: '#f9a8d4' },
  { name: 'بنفسجي', from: '#581c87', to: '#c4b5fd' },
  { name: 'فحمي',  from: '#0f172a', to: '#475569' },
];

const generateGradientDataUrl = (from: string, to: string): string => {
  const c = document.createElement('canvas');
  c.width = 1280;
  c.height = 720;
  const ctx = c.getContext('2d');
  if (!ctx) return '';
  const grad = ctx.createLinearGradient(0, 0, c.width, c.height);
  grad.addColorStop(0, from);
  grad.addColorStop(1, to);
  ctx.fillStyle = grad;
  ctx.fillRect(0, 0, c.width, c.height);
  return c.toDataURL('image/jpeg', 0.85);
};

const BackgroundImagePicker: React.FC = () => {
  const setBg = useCallStore((s) => s.setVideoBackgroundImage);
  const setEffect = useCallStore((s) => s.setVideoEffect);
  const videoEffect = useCallStore((s) => s.videoEffect);
  const inputRef = useRef<HTMLInputElement>(null);
  const [open, setOpen] = useState(false);

  const handleFile = async (file: File) => {
    if (!file.type.startsWith('image/')) return;
    const reader = new FileReader();
    reader.onload = async () => {
      const src = String(reader.result);
      await setBg(src);
      await setEffect('image');
      setOpen(false);
    };
    reader.readAsDataURL(file);
  };

  const handlePreset = async (from: string, to: string) => {
    const url = generateGradientDataUrl(from, to);
    await setBg(url);
    await setEffect('image');
    setOpen(false);
  };

  const clear = async () => {
    await setBg('');
    await setEffect('none');
    setOpen(false);
  };

  return (
    <div className="flex flex-col items-center gap-2 relative">
      <button
        onClick={() => setOpen((v) => !v)}
        className={`w-14 h-14 rounded-full flex items-center justify-center transition-all duration-200
                    ${open || videoEffect === 'image'
                      ? 'bg-blue-600 text-white'
                      : 'bg-surface-700 text-text-300 hover:bg-surface-600'}`}
        title="خلفية مخصصة"
      >
        {/* Picture frame icon, inline. */}
        <svg width="22" height="22" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" strokeWidth="2"
             strokeLinecap="round" strokeLinejoin="round">
          <rect x="3" y="3" width="18" height="18" rx="2" />
          <circle cx="9" cy="9" r="2" />
          <path d="m21 15-3.5-3.5a1.4 1.4 0 0 0-2 0L5 22" />
        </svg>
      </button>
      <span className="text-xs text-text-400 font-medium">خلفية</span>

      {open && (
        <div className="absolute bottom-20 left-1/2 -translate-x-1/2
                        w-72 bg-surface-900/95 border border-surface-700
                        rounded-lg shadow-2xl backdrop-blur p-3 z-50">
          <div className="text-xs text-text-300 font-medium mb-2">
            اختر خلفية
          </div>
          <div className="grid grid-cols-3 gap-2 mb-3">
            {PRESET_GRADIENTS.map((g) => (
              <button
                key={g.name}
                onClick={() => handlePreset(g.from, g.to)}
                className="aspect-video rounded-md overflow-hidden
                           hover:ring-2 hover:ring-blue-400 transition relative"
                style={{ background: `linear-gradient(135deg, ${g.from}, ${g.to})` }}
                title={g.name}
              >
                <span className="absolute inset-0 flex items-end p-1
                                 text-[10px] text-white/90 font-medium
                                 bg-gradient-to-t from-black/40 to-transparent">
                  {g.name}
                </span>
              </button>
            ))}
          </div>
          <input
            ref={inputRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) void handleFile(f);
            }}
          />
          <div className="flex gap-1">
            <button
              onClick={() => inputRef.current?.click()}
              className="flex-1 px-3 py-1.5 rounded bg-surface-800
                         hover:bg-surface-700 text-text-100 text-xs"
            >
              رفع صورة...
            </button>
            <button
              onClick={clear}
              className="px-3 py-1.5 rounded bg-surface-800
                         hover:bg-surface-700 text-text-300 text-xs"
            >
              إلغاء الخلفية
            </button>
          </div>
        </div>
      )}
    </div>
  );
};

export default BackgroundImagePicker;
