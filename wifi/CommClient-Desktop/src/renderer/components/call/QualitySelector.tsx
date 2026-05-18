/**
 * QualitySelector.tsx — in-call resolution / bitrate selector.
 *
 * Shows the same ladder the server advertised via /api/media-policy/me,
 * filtered by QualityController.getAllowedPresets(). Clicking a row
 * calls QualityController.forcePreset() which in turn clamps against
 * the server cap — the dropdown is a hint, not authority.
 *
 * The dropdown also exposes an "Auto" row that hands control back to
 * the adaptive quality loop (set by clearing the forced preset).
 */

import React, { useEffect, useRef, useState } from 'react';
import { ChevronDown, Check, Settings, Lock } from 'lucide-react';

import type { QualityController } from '../../services/call/QualityController';

interface QualitySelectorProps {
  controller: QualityController | null;
  /** Called after the controller switches preset so parent can update UI. */
  onChange?: (presetId: string) => void;
}

interface Row {
  id: string;
  label: string;
  subline: string;
}

function buildRows(controller: QualityController | null): {
  rows: Row[];
  policyNote: string | null;
} {
  if (!controller) return { rows: [], policyNote: null };
  const allowed = controller.getAllowedPresets();
  const cap = controller.getServerCap();

  const rows: Row[] = allowed.map(({ id, preset }) => {
    const sub = preset.idealWidth === 0
      ? 'Voice only, no video'
      : `${preset.idealWidth}×${preset.idealHeight} @ ${preset.maxFramerate}fps · ${preset.maxBitrateKbps} kbps`;
    return { id, label: preset.label, subline: sub };
  });

  // Always prepend "Auto" as the default.
  rows.unshift({
    id: '__auto__',
    label: 'Auto',
    subline: 'Adapts to network & CPU',
  });

  let policyNote: string | null = null;
  if (cap) {
    policyNote = `Admin cap: ${cap.maxWidth}×${cap.maxHeight} @ ${cap.maxFramerate}fps, ${cap.maxBitrateKbps} kbps`;
    if (!cap.allowClientOverride) {
      policyNote += ' (locked)';
    }
  }
  return { rows, policyNote };
}


export const QualitySelector: React.FC<QualitySelectorProps> = ({
  controller,
  onChange,
}) => {
  const [open, setOpen] = useState(false);
  const [currentId, setCurrentId] = useState<string>(
    controller?.currentPreset ?? '__auto__',
  );
  const [{ rows, policyNote }, setBuilt] = useState(() => buildRows(controller));
  const ref = useRef<HTMLDivElement>(null);

  // Refresh rows when the controller changes (e.g. new server cap).
  useEffect(() => {
    setBuilt(buildRows(controller));
  }, [controller]);

  // Close on outside click.
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onDoc);
    return () => document.removeEventListener('mousedown', onDoc);
  }, []);

  // Re-read rows every few seconds — catches server-pushed cap updates.
  useEffect(() => {
    if (!controller) return;
    const t = setInterval(() => {
      setBuilt(buildRows(controller));
      setCurrentId(controller.currentPreset);
    }, 5_000);
    return () => clearInterval(t);
  }, [controller]);

  const handlePick = async (id: string) => {
    if (!controller) return;
    setOpen(false);
    if (id === '__auto__') {
      // Hand control back to the adaptive loop by forcing the current
      // highest-allowed preset and letting _pollWithReporting take over.
      const highest = rows.find((r) => r.id !== '__auto__');
      if (highest) await controller.forcePreset(highest.id);
      setCurrentId('__auto__');
      onChange?.('__auto__');
      return;
    }
    await controller.forcePreset(id);
    setCurrentId(id);
    onChange?.(id);
  };

  const locked = !!controller?.getServerCap() && !controller.getServerCap()!.allowClientOverride;
  const currentLabel = (rows.find((r) => r.id === currentId) || rows[0])?.label ?? 'Auto';

  return (
    <div ref={ref} className="relative inline-block text-left">
      <button
        type="button"
        onClick={() => !locked && setOpen((v) => !v)}
        disabled={locked}
        className={`flex items-center gap-2 rounded-lg border border-zinc-700 bg-zinc-800/80 px-3 py-1.5 text-sm text-zinc-100 hover:bg-zinc-700/80 ${
          locked ? 'cursor-not-allowed opacity-70' : ''
        }`}
        title={locked ? 'Quality locked by admin policy' : 'Change video quality'}
      >
        <Settings className="h-4 w-4" />
        <span className="font-medium">{currentLabel}</span>
        {locked ? <Lock className="h-4 w-4 text-amber-400" /> : <ChevronDown className="h-4 w-4" />}
      </button>

      {open && (
        <div className="absolute right-0 z-50 mt-2 w-72 rounded-xl border border-zinc-700 bg-zinc-900 p-1 shadow-2xl">
          {policyNote && (
            <div className="border-b border-zinc-800 px-3 py-2 text-xs text-zinc-400">
              {policyNote}
            </div>
          )}
          <ul role="listbox" className="max-h-80 overflow-y-auto py-1">
            {rows.map((r) => {
              const isActive = r.id === currentId;
              return (
                <li key={r.id}>
                  <button
                    type="button"
                    onClick={() => handlePick(r.id)}
                    className={`flex w-full items-start gap-2 rounded-lg px-3 py-2 text-left text-sm ${
                      isActive
                        ? 'bg-indigo-600/30 text-indigo-100'
                        : 'text-zinc-200 hover:bg-zinc-800'
                    }`}
                  >
                    <span className="pt-0.5">
                      {isActive ? <Check className="h-4 w-4" /> : <span className="block h-4 w-4" />}
                    </span>
                    <span className="flex-1">
                      <span className="block font-medium">{r.label}</span>
                      <span className="block text-xs text-zinc-400">{r.subline}</span>
                    </span>
                  </button>
                </li>
              );
            })}
            {rows.length === 0 && (
              <li className="px-3 py-2 text-xs text-zinc-500">
                No quality levels available
              </li>
            )}
          </ul>
        </div>
      )}
    </div>
  );
};

export default QualitySelector;
