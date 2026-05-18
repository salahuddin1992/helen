import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/storage/secure_storage.dart';
import '../../../data/api/api_client.dart';
import '../../../data/api/pairing_api.dart';
import '../../../providers/auth_provider.dart';
import '../../../router/routes.dart';

class EnterCodeScreen extends ConsumerStatefulWidget {
  const EnterCodeScreen({super.key});

  @override
  ConsumerState<EnterCodeScreen> createState() => _EnterCodeScreenState();
}

class _EnterCodeScreenState extends ConsumerState<EnterCodeScreen> {
  final TextEditingController _code = TextEditingController();
  bool _busy = false;
  String? _error;

  @override
  void dispose() {
    _code.dispose();
    super.dispose();
  }

  Future<void> _submit() async {
    final String code = _code.text.trim().toUpperCase();
    if (code.length < 6) {
      setState(() => _error = 'Code must be at least 6 characters');
      return;
    }
    setState(() {
      _busy = true;
      _error = null;
    });
    try {
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
          _busy = false;
        });
      }
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Enter pairing code')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              const SizedBox(height: 16),
              Text(
                'Enter the code shown on the device you want to pair with',
                style: Theme.of(context).textTheme.bodyMedium,
                textAlign: TextAlign.center,
              ),
              const SizedBox(height: 32),
              TextField(
                controller: _code,
                inputFormatters: <TextInputFormatter>[
                  FilteringTextInputFormatter.allow(RegExp(r'[A-Z0-9]')),
                  LengthLimitingTextInputFormatter(12),
                ],
                textAlign: TextAlign.center,
                style: Theme.of(context)
                    .textTheme
                    .headlineMedium
                    ?.copyWith(letterSpacing: 8),
                textCapitalization: TextCapitalization.characters,
                decoration: const InputDecoration(hintText: 'ABC123'),
              ),
              if (_error != null) ...<Widget>[
                const SizedBox(height: 16),
                Text(
                  _error!,
                  style:
                      TextStyle(color: Theme.of(context).colorScheme.error),
                  textAlign: TextAlign.center,
                ),
              ],
              const SizedBox(height: 24),
              FilledButton(
                onPressed: _busy ? null : _submit,
                child: _busy
                    ? const SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2.5),
                      )
                    : const Text('Pair'),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
