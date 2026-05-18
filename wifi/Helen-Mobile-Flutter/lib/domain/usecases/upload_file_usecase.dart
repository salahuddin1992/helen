/// File upload use case — chooses single-shot vs resumable based on size.
library;

import 'dart:io';

import '../../core/config/constants.dart';
import '../../data/api/files_api.dart';

class UploadFileUseCase {
  UploadFileUseCase(this._files);
  final FilesApi _files;

  /// Threshold above which we switch to resumable. 5 MiB is small enough
  /// to avoid most network blips; the desktop client uses the same cutoff.
  static const int resumableThreshold = 5 * 1024 * 1024;

  Future<Map<String, dynamic>> call(
    File file, {
    String? channelId,
    void Function(int sent, int total)? onProgress,
  }) async {
    final int size = await file.length();
    if (size <= resumableThreshold) {
      return _files.upload(file, channelId: channelId, onProgress: onProgress);
    }
    return _files.uploadResumable(
      file,
      channelId: channelId,
      chunkSize: K.defaultChunkSize,
      onProgress: onProgress,
    );
  }
}
