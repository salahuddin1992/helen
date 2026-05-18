/// Environment configuration.
///
/// Values are sourced (in order of precedence):
///   1. `.env` file (loaded by [dotenv] in main.dart)
///   2. `--dart-define` compile-time defines
///   3. Hard-coded fallbacks below
///
/// Never put real secrets here. The mobile client never holds a service
/// account or admin key — only user-issued JWTs from /api/auth/login.
library;

import 'package:flutter/foundation.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';

class Env {
  Env._({
    required this.apiBaseUrl,
    required this.socketUrl,
    required this.oauthRedirect,
    required this.pairingRedirect,
    required this.logLevel,
    required this.connectTimeoutMs,
    required this.receiveTimeoutMs,
    required this.retryAttempts,
    required this.appName,
  });

  static late Env _instance;
  static Env get I => _instance;

  final String apiBaseUrl;
  final String socketUrl;
  final String oauthRedirect;
  final String pairingRedirect;
  final String logLevel;
  final int connectTimeoutMs;
  final int receiveTimeoutMs;
  final int retryAttempts;
  final String appName;

  static String _read(String key, String fallback) {
    // dotenv first
    if (dotenv.isInitialized && dotenv.env.containsKey(key)) {
      final String? v = dotenv.env[key];
      if (v != null && v.isNotEmpty) return v;
    }
    // dart-define second (only resolvable at compile time for known keys)
    final String fromDefine = _readDefine(key);
    if (fromDefine.isNotEmpty) return fromDefine;
    return fallback;
  }

  static String _readDefine(String key) {
    // const lookup table — extend as new HELEN_* defines are added
    switch (key) {
      case 'HELEN_SERVER_URL':
        return const String.fromEnvironment('HELEN_SERVER_URL');
      case 'HELEN_SOCKET_URL':
        return const String.fromEnvironment('HELEN_SOCKET_URL');
      case 'HELEN_OAUTH_REDIRECT':
        return const String.fromEnvironment('HELEN_OAUTH_REDIRECT');
      case 'HELEN_PAIRING_REDIRECT':
        return const String.fromEnvironment('HELEN_PAIRING_REDIRECT');
      case 'HELEN_LOG_LEVEL':
        return const String.fromEnvironment('HELEN_LOG_LEVEL');
      default:
        return '';
    }
  }

  static int _readInt(String key, int fallback) {
    final String raw = _read(key, '');
    return int.tryParse(raw) ?? fallback;
  }

  static void bootstrap() {
    final String api = _read('HELEN_SERVER_URL', 'http://127.0.0.1:3000');
    final String ws = _read('HELEN_SOCKET_URL', api);
    _instance = Env._(
      apiBaseUrl: _stripTrailingSlash(api),
      socketUrl: _stripTrailingSlash(ws),
      oauthRedirect: _read('HELEN_OAUTH_REDIRECT', 'helen://oauth/callback'),
      pairingRedirect: _read('HELEN_PAIRING_REDIRECT', 'helen://pair/callback'),
      logLevel: _read('HELEN_LOG_LEVEL', kDebugMode ? 'debug' : 'info'),
      connectTimeoutMs: _readInt('HELEN_CONNECT_TIMEOUT_MS', 10000),
      receiveTimeoutMs: _readInt('HELEN_RECEIVE_TIMEOUT_MS', 30000),
      retryAttempts: _readInt('HELEN_RETRY_ATTEMPTS', 3),
      appName: _read('HELEN_APP_NAME', 'Helen'),
    );
  }

  static String _stripTrailingSlash(String s) =>
      s.endsWith('/') ? s.substring(0, s.length - 1) : s;

  /// Allow runtime override (Settings → Server URL). Resets the singleton.
  static void overrideServer({String? api, String? ws}) {
    _instance = Env._(
      apiBaseUrl: _stripTrailingSlash(api ?? _instance.apiBaseUrl),
      socketUrl: _stripTrailingSlash(ws ?? api ?? _instance.socketUrl),
      oauthRedirect: _instance.oauthRedirect,
      pairingRedirect: _instance.pairingRedirect,
      logLevel: _instance.logLevel,
      connectTimeoutMs: _instance.connectTimeoutMs,
      receiveTimeoutMs: _instance.receiveTimeoutMs,
      retryAttempts: _instance.retryAttempts,
      appName: _instance.appName,
    );
  }
}
