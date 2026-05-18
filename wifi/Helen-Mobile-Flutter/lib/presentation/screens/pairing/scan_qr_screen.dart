import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:mobile_scanner/mobile_scanner.dart';

import '../../../core/storage/secure_storage.dart';
import '../../../data/api/api_client.dart';
import '../../../data/api/pairing_api.dart';
import '../../../providers/auth_provider.dart';
import '../../../router/routes.dart';
import '../../../services/permissions.dart';

class ScanQrScreen extends ConsumerStatefulWidget {
  const ScanQrScreen({super.key});

  @override
  ConsumerState<ScanQrScreen> createState() => _ScanQrScreenState();
}

class _ScanQrScreenState extends ConsumerState<ScanQrScreen> {
  late final MobileScannerController _controller =
      MobileScannerController(detectionSpeed: DetectionSpeed.noDuplicates);
  bool _claiming = false;
  String? _error;

  @override
  void initState() {
    super.initState();
    Future<void>.microtask(() async {
      final bool ok = await AppPermissions.requestCamera();
      if (!ok && mounted) {
        setState(() => _error = 'Camera permission required');
      }
    });
  }

  @override
  void dispose() {
    unawaited(_controller.dispose());
    super.dispose();
  }

  Future<void> _onDetect(BarcodeCapture cap) async {
    if (_claiming) return;
    final String? raw = cap.barcodes.isNotEmpty ? cap.barcodes.first.rawValue : null;
    if (raw == null || raw.isEmpty) return;

    // Expected formats:
    //   helen://pair?code=ABC123&server=https://...
    //   ABC123 (plain code typed manually elsewhere)
    String code;
    String? overrideServer;
    try {
      final Uri u = Uri.parse(raw);
      if (u.scheme == 'helen' || u.scheme == 'https' || u.scheme == 'http') {
        code = u.queryParameters['code'] ?? '';
        overrideServer = u.queryParameters['server'];
      } else {
        code = raw.trim();
      }
    } on Object {
      code = raw.trim();
    }
    if (code.isEmpty) return;

    setState(() => _claiming = true);
    try {
      if (overrideServer != null && overrideServer.isNotEmpty) {
        await SecureStorage.setServerUrl(overrideServer);
        ApiClient.I.configure(baseUrl: overrideServer);
      }
      final PairingApi api = PairingApi(ApiClient.I);
      final Map<String, dynamic> resp = await api.complete(
        code: code,
        deviceName: 'Helen Mobile',
      );
      final Map<String, dynamic> tokens =
          resp['tokens'] as Map<String, dynamic>;
      final String a = tokens['access_token'] as String;
      final String r = tokens['refresh_token'] as String;
      ApiClient.I.setTokens(a, r);
      await SecureStorage.setTokens(a, r);
      ref.invalidate(authProvider);
      if (mounted) context.go(Routes.home);
    } on Object catch (e) {
      if (mounted) {
        setState(() {
          _error = 'Pairing failed: $e';
          _claiming = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Scan pairing code'),
        actions: <Widget>[
          IconButton(
            onPressed: () => context.push(Routes.pairCode),
            icon: const Icon(Icons.keyboard_alt_outlined),
            tooltip: 'Enter code manually',
          ),
        ],
      ),
      body: Stack(
        fit: StackFit.expand,
        children: <Widget>[
          MobileScanner(controller: _controller, onDetect: _onDetect),
          DecoratedBox(
            decoration: BoxDecoration(
              gradient: LinearGradient(
                begin: Alignment.topCenter,
                end: Alignment.bottomCenter,
                colors: <Color>[
                  Colors.black.withValues(alpha: 0.6),
                  Colors.transparent,
                  Colors.black.withValues(alpha: 0.6),
                ],
              ),
            ),
          ),
          Align(
            alignment: Alignment.bottomCenter,
            child: Padding(
              padding: const EdgeInsets.all(24),
              child: Column(
                mainAxisSize: MainAxisSize.min,
                children: <Widget>[
                  if (_error != null)
                    Container(
                      padding: const EdgeInsets.all(12),
                      decoration: BoxDecoration(
                        color: Theme.of(context).colorScheme.errorContainer,
                        borderRadius: BorderRadius.circular(12),
                      ),
                      child: Text(_error!),
                    ),
                  if (_claiming)
                    const Padding(
                      padding: EdgeInsets.only(top: 16),
                      child: CircularProgressIndicator(),
                    ),
                ],
              ),
            ),
          ),
        ],
      ),
    );
  }
}
