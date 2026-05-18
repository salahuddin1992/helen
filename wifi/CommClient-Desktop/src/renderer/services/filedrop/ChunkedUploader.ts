/**
 * ChunkedUploader — Upload files to the server's single-file upload endpoint.
 * For files under 10MB: single POST upload.
 * For files over 10MB: attempt chunked upload, fallback to single POST if server returns 404.
 * Uses auth tokens from the api client module.
 */

import { getBaseUrl } from '../api.client';
import { useAuthStore } from '../../stores/auth.store';

interface UploadProgressCallback {
  onProgress: (uploaded: number, total: number, speed: number) => void;
  onComplete: (fileId: string) => void;
  onError: (error: string) => void;
}

const CHUNK_SIZE = 256 * 1024; // 256 KB
const MAX_RETRIES = 3;
const SINGLE_UPLOAD_THRESHOLD = 10 * 1024 * 1024; // 10 MB

export class ChunkedUploader {
  private abortControllers: Map<string, AbortController> = new Map();
  private uploadSpeeds: Map<string, number> = new Map();

  /**
   * Upload file — uses single-file POST for files under 10MB,
   * attempts chunked for larger files with single-POST fallback.
   */
  async uploadFile(
    file: File,
    uploadUrl: string,
    callbacks: UploadProgressCallback,
    channelId?: string
  ): Promise<string> {
    const uploadId = `upload-${Date.now()}-${Math.random()}`;
    const abortController = new AbortController();
    this.abortControllers.set(uploadId, abortController);

    try {
      let fileId: string;

      if (file.size <= SINGLE_UPLOAD_THRESHOLD) {
        fileId = await this.singleUpload(file, uploadUrl, abortController, callbacks, channelId);
      } else {
        // Attempt chunked upload, fallback to single if server doesn't support it
        try {
          fileId = await this.chunkedUpload(file, uploadUrl, abortController, callbacks, channelId);
        } catch (error) {
          // If chunked endpoint returned 404, fallback to single upload
          if (error instanceof ChunkedNotSupportedError) {
            fileId = await this.singleUpload(file, uploadUrl, abortController, callbacks, channelId);
          } else {
            throw error;
          }
        }
      }

      callbacks.onComplete(fileId);
      this.abortControllers.delete(uploadId);
      return fileId;
    } catch (error) {
      callbacks.onError(error instanceof Error ? error.message : 'Upload failed');
      this.abortControllers.delete(uploadId);
      throw error;
    }
  }

  /**
   * Cancel ongoing upload
   */
  cancelUpload(uploadId: string): void {
    const controller = this.abortControllers.get(uploadId);
    if (controller) {
      controller.abort();
      this.abortControllers.delete(uploadId);
    }
  }

  /**
   * Cancel all ongoing uploads (useful for transfer ID mismatches)
   */
  cancelAll(): void {
    for (const [id, controller] of this.abortControllers) {
      controller.abort();
    }
    this.abortControllers.clear();
  }

  /**
   * Get current upload speed
   */
  getSpeed(uploadId: string): number {
    return this.uploadSpeeds.get(uploadId) || 0;
  }

  /**
   * Get the internal upload ID for a transfer (first active upload)
   */
  getActiveUploadId(): string | undefined {
    const keys = Array.from(this.abortControllers.keys());
    return keys.length > 0 ? keys[0] : undefined;
  }

  private getAuthHeader(): Record<string, string> {
    const tokens = useAuthStore.getState().tokens;
    if (tokens?.access_token) {
      return { Authorization: `Bearer ${tokens.access_token}` };
    }
    return {};
  }

  private getUploadEndpoint(channelId?: string): string {
    const baseUrl = getBaseUrl();
    const qs = channelId ? `?channel_id=${channelId}` : '';
    return `${baseUrl}/api/files/upload${qs}`;
  }

  /**
   * Single-file upload: sends the entire file as multipart FormData
   */
  private async singleUpload(
    file: File,
    _uploadUrl: string,
    abortController: AbortController,
    callbacks: UploadProgressCallback,
    channelId?: string
  ): Promise<string> {
    const url = this.getUploadEndpoint(channelId);
    const formData = new FormData();
    formData.append('file', file);

    const startTime = Date.now();

    const response = await fetch(url, {
      method: 'POST',
      headers: this.getAuthHeader(),
      body: formData,
      signal: abortController.signal,
    });

    if (!response.ok) {
      const errorText = await response.text().catch(() => response.statusText);
      throw new Error(`Upload failed (${response.status}): ${errorText}`);
    }

    const elapsed = (Date.now() - startTime) / 1000 || 1;
    const speed = file.size / elapsed;
    callbacks.onProgress(file.size, file.size, speed);

    const data = await response.json();
    return data.id || data.fileId || data.file_id;
  }

  /**
   * Chunked upload: slices file and uploads sequentially.
   * Throws ChunkedNotSupportedError if server returns 404.
   */
  private async chunkedUpload(
    file: File,
    uploadUrl: string,
    abortController: AbortController,
    callbacks: UploadProgressCallback,
    channelId?: string
  ): Promise<string> {
    const chunks = this.createChunks(file);
    const totalChunks = chunks.length;
    const uploadId = `chunk-${Date.now()}`;

    let uploadedBytes = 0;
    let lastProgressTime = Date.now();
    let lastProgressBytes = 0;

    for (let i = 0; i < totalChunks; i++) {
      const chunk = chunks[i];
      let retryCount = 0;

      while (retryCount < MAX_RETRIES) {
        try {
          const formData = new FormData();
          formData.append('file', chunk);
          formData.append('uploadId', uploadId);
          formData.append('chunkIndex', i.toString());
          formData.append('totalChunks', totalChunks.toString());
          formData.append('fileName', file.name);
          if (channelId) formData.append('channel_id', channelId);

          const response = await fetch(uploadUrl, {
            method: 'POST',
            headers: this.getAuthHeader(),
            body: formData,
            signal: abortController.signal,
          });

          if (response.status === 404) {
            throw new ChunkedNotSupportedError();
          }

          if (!response.ok) {
            throw new Error(`Chunk upload failed: ${response.statusText}`);
          }

          uploadedBytes += chunk.size;

          const now = Date.now();
          const timeDelta = (now - lastProgressTime) / 1000;
          const bytesDelta = uploadedBytes - lastProgressBytes;
          const speed = bytesDelta / (timeDelta || 1);

          callbacks.onProgress(uploadedBytes, file.size, speed);

          lastProgressTime = now;
          lastProgressBytes = uploadedBytes;

          break;
        } catch (error) {
          if (error instanceof ChunkedNotSupportedError) throw error;
          retryCount++;
          if (retryCount >= MAX_RETRIES) {
            throw new Error(
              `Chunk ${i + 1}/${totalChunks} failed after ${MAX_RETRIES} retries`
            );
          }
          await this.delay(1000 * retryCount);
        }
      }
    }

    // Finalize — if server doesn't support finalize, fall back
    try {
      const response = await fetch(`${uploadUrl}/finalize`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          ...this.getAuthHeader(),
        },
        body: JSON.stringify({
          uploadId,
          fileName: file.name,
          fileSize: file.size,
          mimeType: file.type,
        }),
        signal: abortController.signal,
      });

      if (response.status === 404) {
        throw new ChunkedNotSupportedError();
      }

      if (!response.ok) {
        throw new Error(`Finalize failed: ${response.statusText}`);
      }

      const data = await response.json();
      return data.id || data.fileId || data.file_id;
    } catch (error) {
      if (error instanceof ChunkedNotSupportedError) throw error;
      throw error;
    }
  }

  private createChunks(file: File): Blob[] {
    const chunks: Blob[] = [];
    for (let i = 0; i < file.size; i += CHUNK_SIZE) {
      chunks.push(file.slice(i, i + CHUNK_SIZE));
    }
    return chunks;
  }

  private delay(ms: number): Promise<void> {
    return new Promise((resolve) => setTimeout(resolve, ms));
  }
}

class ChunkedNotSupportedError extends Error {
  constructor() {
    super('Server does not support chunked uploads');
    this.name = 'ChunkedNotSupportedError';
  }
}
