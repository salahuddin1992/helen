/**
 * FileMessageBubble — generic file attachment with download +
 * "open with system default" flow.
 *
 * What the user sees:
 *   1. Idle      — extension icon + filename + size + Download button.
 *   2. Downloading — progress percentage, spinner.
 *   3. Done      — "Open" + "Show in folder" buttons. Open invokes
 *      ``shell.openPath`` so the OS picks the right app (PDF reader,
 *      Word, archive manager, …).
 *
 * For audio files we route into the existing VoicePlayer-like
 * inline player; for video files the parent dispatcher uses
 * VideoMessageBubble. This component is the catch-all for
 * everything else (PDF, DOCX, ZIP, EXE, etc).
 */

import React, { useEffect, useState } from 'react';
import { Download, Folder, ExternalLink, Loader2 } from 'lucide-react';
import toast from 'react-hot-toast';
import {
  downloadFileToDisk,
  openWithDefaultApp,
  revealInFolder,
} from '@/services/chat-downloads';
import {
  formatBytes,
  fileIconForExtension,
  getExtension,
} from './videoExt';
import { getBaseUrl } from '@/services/api.client';

interface Props {
  fileId: string;
  filename: string;
  isOwn?: boolean;
  initialSizeBytes?: number | null;
}

export const FileMessageBubble: React.FC<Props> = ({
  fileId,
  filename,
  isOwn = false,
  initialSizeBytes = null,
}) => {
  const [bytesTotal, setBytesTotal] = useState<number | null>(
    initialSizeBytes,
  );
  const [downloading, setDownloading] = useState(false);
  const [progress, setProgress] = useState(0);
  const [savedPath, setSavedPath] = useState<string | null>(null);

  // HEAD probe for size (decorative — failure is silent).
  useEffect(() => {
    if (bytesTotal != null) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await fetch(
          `${getBaseUrl()}/api/files/${fileId}`,
          { method: 'HEAD' },
        );
        const len = res.headers.get('content-length');
        if (!cancelled && len) {
          setBytesTotal(parseInt(len, 10));
        }
      } catch { /* ignore */ }
    })();
    return () => {
      cancelled = true;
    };
  }, [fileId, bytesTotal]);

  const ext = getExtension(filename);
  const icon = fileIconForExtension(ext);

  const handleDownload = async () => {
    if (downloading) return;
    setDownloading(true);
    setProgress(0);
    const r = await downloadFileToDisk(fileId, filename, (p) => {
      setProgress(p.bytes_received);
      if (p.bytes_total != null) setBytesTotal(p.bytes_total);
    });
    setDownloading(false);
    if (r.error) {
      toast.error(`فشل التحميل: ${r.error}`);
      return;
    }
    if (r.path) {
      setSavedPath(r.path);
      toast.success('تم التحميل');
    } else {
      toast.success('بدأ التحميل');
    }
  };

  const handleOpen = async () => {
    if (!savedPath) return;
    const ok = await openWithDefaultApp(savedPath);
    if (!ok) toast.error('تعذّر فتح الملف بالتطبيق الافتراضي');
  };

  const handleDownloadAndOpen = async () => {
    if (downloading) return;
    setDownloading(true);
    const r = await downloadFileToDisk(fileId, filename, (p) => {
      setProgress(p.bytes_received);
      if (p.bytes_total != null) setBytesTotal(p.bytes_total);
    });
    setDownloading(false);
    if (r.error || !r.path) {
      toast.error(r.error || 'فشل التحميل');
      return;
    }
    setSavedPath(r.path);
    const ok = await openWithDefaultApp(r.path);
    if (!ok) toast.error('تعذّر فتح الملف');
  };

  const percent =
    bytesTotal && bytesTotal > 0
      ? Math.min(100, Math.round((progress / bytesTotal) * 100))
      : null;

  return (
    <div
      className={
        'flex items-center gap-3 p-3 rounded-lg border ' +
        (isOwn
          ? 'border-blue-700/50 bg-blue-900/10'
          : 'border-surface-700 bg-surface-800/40')
      }
      style={{ maxWidth: '380px' }}
    >
      {/* Big extension chip */}
      <div className="flex-none w-12 h-12 rounded-lg bg-surface-700
                      flex flex-col items-center justify-center
                      text-xl">
        <span aria-hidden>{icon}</span>
        {ext && (
          <span className="text-[9px] uppercase text-gray-300
                            mt-0.5 tracking-wide">
            {ext}
          </span>
        )}
      </div>

      {/* Filename + size + actions */}
      <div className="min-w-0 flex-1">
        <div className="text-sm font-medium text-gray-100 truncate"
             title={filename}>
          {filename || `file-${fileId.slice(0, 6)}`}
        </div>
        <div className="text-[11px] text-gray-400 mt-0.5">
          {formatBytes(bytesTotal)}
          {downloading && percent != null
            ? ` · ${percent}%`
            : ''}
          {savedPath && !downloading
            ? ' · حُفظ'
            : ''}
        </div>

        <div className="mt-1.5 flex items-center gap-1.5">
          {!savedPath && (
            <>
              <button
                onClick={handleDownloadAndOpen}
                disabled={downloading}
                className="flex items-center gap-1 px-2 py-1
                           bg-blue-700 hover:bg-blue-600
                           disabled:opacity-50 text-white rounded
                           text-[11px]"
              >
                {downloading
                  ? <Loader2 size={11} className="animate-spin" />
                  : <ExternalLink size={11} />}
                <span>تحميل وفتح</span>
              </button>
              <button
                onClick={handleDownload}
                disabled={downloading}
                className="flex items-center gap-1 px-2 py-1
                           bg-surface-700 hover:bg-surface-600
                           disabled:opacity-50 text-white rounded
                           text-[11px]"
              >
                <Download size={11} />
                <span>تحميل فقط</span>
              </button>
            </>
          )}
          {savedPath && (
            <>
              <button
                onClick={handleOpen}
                className="flex items-center gap-1 px-2 py-1
                           bg-blue-700 hover:bg-blue-600 text-white
                           rounded text-[11px]"
              >
                <ExternalLink size={11} />
                <span>فتح</span>
              </button>
              <button
                onClick={() => revealInFolder(savedPath)}
                className="flex items-center gap-1 px-2 py-1
                           bg-surface-700 hover:bg-surface-600
                           text-white rounded text-[11px]"
                title="عرض في المجلد"
              >
                <Folder size={11} />
              </button>
            </>
          )}
        </div>
      </div>
    </div>
  );
};
