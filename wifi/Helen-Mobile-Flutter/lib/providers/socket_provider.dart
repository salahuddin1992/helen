/// Socket lifecycle provider. Connects when authenticated, disposes on logout.
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/logger/app_logger.dart';
import '../core/storage/secure_storage.dart';
import '../data/socket/socket_client.dart';
import 'auth_provider.dart';

final Provider<SocketClient> socketProvider = Provider<SocketClient>((Ref ref) {
  final SocketClient sc = SocketClient.I;
  // React to auth state — open/close socket accordingly.
  ref.listen<AuthState>(authProvider, (AuthState? prev, AuthState next) {
    if (next is AuthAuthenticated) {
      SecureStorage.getAccessToken().then((String? t) {
        if (t != null && t.isNotEmpty) {
          sc.connect(accessToken: t).catchError((Object e, StackTrace st) {
            AppLogger.I.w('socket connect failed', e, st);
          });
        }
      });
    } else if (next is AuthUnauthenticated) {
      unawaited(sc.disconnect());
    }
  }, fireImmediately: true);
  ref.onDispose(() => sc.dispose());
  return sc;
});

void unawaited(Future<Object?> f) {
  f.then((_) {}, onError: (Object _, StackTrace __) {});
}
