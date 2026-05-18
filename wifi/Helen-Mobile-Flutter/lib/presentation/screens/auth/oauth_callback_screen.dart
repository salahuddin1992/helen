import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/storage/secure_storage.dart';
import '../../../data/api/api_client.dart';
import '../../../data/api/oauth_api.dart';
import '../../../providers/auth_provider.dart';
import '../../../router/routes.dart';

class OauthCallbackScreen extends ConsumerStatefulWidget {
  const OauthCallbackScreen({super.key, required this.uri});
  final Uri uri;

  @override
  ConsumerState<OauthCallbackScreen> createState() =>
      _OauthCallbackScreenState();
}

class _OauthCallbackScreenState extends ConsumerState<OauthCallbackScreen> {
  String _status = 'Completing sign-in…';

  @override
  void initState() {
    super.initState();
    Future<void>.microtask(_handle);
  }

  Future<void> _handle() async {
    final String? provider = widget.uri.queryParameters['provider'];
    final String? code = widget.uri.queryParameters['code'];
    final String? state = widget.uri.queryParameters['state'];
    if (provider == null || code == null || state == null) {
      setState(() => _status = 'Invalid callback URL');
      return;
    }
    try {
      final OauthApi api = OauthApi(ApiClient.I);
      final Map<String, dynamic> resp = await api.exchangeCode(
        provider: provider,
        code: code,
        state: state,
      );
      final Map<String, dynamic> tokens =
          resp['tokens'] as Map<String, dynamic>;
      final String a = tokens['access_token'] as String;
      final String r = tokens['refresh_token'] as String;
      ApiClient.I.setTokens(a, r);
      await SecureStorage.setTokens(a, r);
      // Refresh auth — triggers user fetch through controller restore path.
      // Simplest: just navigate to home; router will re-evaluate.
      ref.invalidate(authProvider);
      if (mounted) context.go(Routes.home);
    } on Object catch (e) {
      setState(() => _status = 'Sign-in failed: $e');
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              const CircularProgressIndicator(),
              const SizedBox(height: 16),
              Text(_status, textAlign: TextAlign.center),
            ],
          ),
        ),
      ),
    );
  }
}
