/// File upload / download — single-shot multipart + resumable chunked.
///
/// Server endpoints (Module P):
///   POST /api/files/upload                       — small files
///   POST /api/files/resumable/init               — start session, returns upload_id
///   PUT  /api/files/resumable/chunk?upload_id=…&index=… — upload one chunk
///   POST /api/files/resumable/complete           — finalize (server stitches + verifies)
///   GET  /api/files/{id}                         — download
library;

import 'dart:async';
import 'dart:io';
import 'dart:typed_data';

import 'package:dio/dio.dart';

import '../../core/config/constants.dart';
import '../../core/errors/app_exception.dart';
import '../../core/logger/app_logger.dart';
import 'api_client.dart';

typedef ProgressCb = void Function(int sent, int total);

class FilesApi {
  FilesApi(this._client);
  final ApiClient _client;

  /// Single-shot upload — use for files < 5 MiB.
  Future<Map<String, dynamic>> upload(
    File file, {
    String? channelId,
    ProgressCb? onProgress,
  }) async {
    return guardApi(() async {
      final FormData form = FormData.fromMap(<String, dynamic>{
        'file': await MultipartFile.fromFile(file.path,
            filename: file.uri.pathSegments.last),
      });
      final Response<Map<String, dynamic>> r =
          await _client.dio.post<Map<String, dynamic>>(
        K.pFilesUpload,
        data: form,
        queryParameters: <String, dynamic>{
          if (channelId != null) 'channel_id': channelId,
        },
        onSendProgress: (int sent, int total) => onProgress?.call(sent, total),
      );
      if (r.statusCode! >= 300) {
        throw ServerException('Upload failed', statusCode: r.statusCode);
      }
      return r.data!;
    });
  }

  /// Resumable upload — for files ≥ 5 MiB. Survives flaky LAN; if the
  /// process dies mid-upload you can resume from the last reported chunk.
  Future<Map<String, dynamic>> uploadResumable(
    File file, {
    String? channelId,
    int chunkSize = K.defaultChunkSize,
    ProgressCb? onProgress,
  }) async {
    final int total = await file.length();
    if (total > K.maxUploadSize) {
      throw ValidationException('File too large (max 2 GiB)');
    }

    return guardApi(() async {
      // 1. Init
      final Map<String, dynamic> init = await _client.post<Map<String, dynamic>>(
        K.pFilesResumableInit,
        body: <String, dynamic>{
          'filename': file.uri.pathSegments.last,
          'size': total,
          'chunk_size': chunkSize,
          if (channelId != null) 'channel_id': channelId,
        },
      );
      final String uploadId = init['upload_id'] as String;
      AppLogger.I
          .i('resumable: init id=$uploadId total=$total chunk=$chunkSize');

      // 2. Upload chunks sequentially. (Parallelism saves time but the desktop
      //    client also uses serial chunks here for back-pressure friendliness;
      //    can be lifted to 4 concurrent later.)
      final RandomAccessFile raf = await file.open();
      int sent = 0;
      int index = 0;
      try {
        while (sent < total) {
          final int remaining = total - sent;
          final int take = remaining < chunkSize ? remaining : chunkSize;
          final Uint8List buf = await raf.read(take);

          await _client.dio.put<dynamic>(
            K.pFilesResumableChunk,
            data: Stream<List<int>>.value(buf),
            queryParameters: <String, dynamic>{
              'upload_id': uploadId,
              'index': index,
            },
            options: Options(
              headers: <String, dynamic>{
                Headers.contentLengthHeader: buf.length,
                'Content-Type': 'application/octet-stream',
              },
            ),
          );

          sent += buf.length;
          index += 1;
          onProgress?.call(sent, total);
        }
      } finally {
        await raf.close();
      }

      // 3. Complete
      final Map<String, dynamic> done = await _client.post<Map<String, dynamic>>(
        K.pFilesResumableComplete,
        body: <String, String>{'upload_id': uploadId},
      );
      AppLogger.I.i('resumable: complete id=$uploadId');
      return done;
    });
  }

  /// Build a download URL the caller can pass to `image_picker` / `Image.network`
  /// (after attaching the auth header out-of-band, e.g. via Dio.download).
  String downloadUrl(String fileId) => '/api/files/$fileId';

  Future<File> downloadTo(String fileId, String savePath,
      {ProgressCb? onProgress, CancelToken? cancelToken}) async {
    return guardApi(() async {
      await _client.dio.download(
        downloadUrl(fileId),
        savePath,
        onReceiveProgress: (int got, int total) {
          if (total > 0) onProgress?.call(got, total);
        },
        cancelToken: cancelToken,
      );
      return File(savePath);
    });
  }
}
