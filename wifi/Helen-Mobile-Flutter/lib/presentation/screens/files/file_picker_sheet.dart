import 'dart:io';

import 'package:file_picker/file_picker.dart';
import 'package:flutter/material.dart';
import 'package:image_picker/image_picker.dart';

import '../../../core/logger/app_logger.dart';
import '../../../data/api/api_client.dart';
import '../../../data/api/files_api.dart';
import '../../../domain/usecases/upload_file_usecase.dart';

class FilePickerSheet extends StatefulWidget {
  const FilePickerSheet({super.key, required this.channelId});
  final String channelId;

  @override
  State<FilePickerSheet> createState() => _FilePickerSheetState();
}

class _FilePickerSheetState extends State<FilePickerSheet> {
  double? _progress;

  Future<void> _pickAndUpload(File file) async {
    setState(() => _progress = 0);
    try {
      final UploadFileUseCase uc =
          UploadFileUseCase(FilesApi(ApiClient.I));
      await uc(
        file,
        channelId: widget.channelId,
        onProgress: (int sent, int total) {
          if (mounted) setState(() => _progress = sent / total);
        },
      );
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          const SnackBar(content: Text('Upload complete')),
        );
        Navigator.of(context).pop();
      }
    } on Object catch (e, st) {
      AppLogger.I.w('upload failed', e, st);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Upload failed: $e')),
        );
        setState(() => _progress = null);
      }
    }
  }

  Future<void> _pickImage(ImageSource s) async {
    final XFile? f = await ImagePicker().pickImage(source: s);
    if (f != null) await _pickAndUpload(File(f.path));
  }

  Future<void> _pickFile() async {
    final FilePickerResult? r = await FilePicker.platform.pickFiles();
    if (r != null && r.files.single.path != null) {
      await _pickAndUpload(File(r.files.single.path!));
    }
  }

  @override
  Widget build(BuildContext context) {
    return SafeArea(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          mainAxisSize: MainAxisSize.min,
          children: <Widget>[
            if (_progress != null) ...<Widget>[
              LinearProgressIndicator(value: _progress),
              const SizedBox(height: 12),
              Text('Uploading… ${(_progress! * 100).toStringAsFixed(0)}%'),
              const SizedBox(height: 12),
            ],
            ListTile(
              leading: const Icon(Icons.camera_alt_outlined),
              title: const Text('Take photo'),
              onTap: () => _pickImage(ImageSource.camera),
            ),
            ListTile(
              leading: const Icon(Icons.image_outlined),
              title: const Text('Choose image'),
              onTap: () => _pickImage(ImageSource.gallery),
            ),
            ListTile(
              leading: const Icon(Icons.attach_file_rounded),
              title: const Text('Pick file'),
              onTap: _pickFile,
            ),
          ],
        ),
      ),
    );
  }
}
