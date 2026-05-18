/// Auth repository — bridges API + secure storage + Dio token state.
library;

import '../../core/logger/app_logger.dart';
import '../../core/storage/secure_storage.dart';
import '../../data/api/api_client.dart';
import '../../data/api/auth_api.dart';
import '../../data/models/auth_tokens.dart';
import '../../data/models/user.dart';

class AuthRepository {
  AuthRepository(this._api);
  final AuthApi _api;

  Future<AuthResponse> login({
    required String username,
    required String password,
    String? deviceName,
  }) async {
    final AuthResponse r = await _api.login(
      username: username,
      password: password,
      deviceName: deviceName,
    );
    await _persist(r);
    return r;
  }

  Future<AuthResponse> register({
    required String username,
    required String displayName,
    required String password,
  }) async {
    final AuthResponse r = await _api.register(
      username: username,
      displayName: displayName,
      password: password,
    );
    await _persist(r);
    return r;
  }

  Future<AuthTokens> refresh(String refreshToken) async {
    final AuthTokens t = await _api.refresh(refreshToken);
    ApiClient.I.setTokens(t.accessToken, t.refreshToken);
    await SecureStorage.setTokens(t.accessToken, t.refreshToken);
    return t;
  }

  Future<User> me() => _api.getMe();

  Future<void> logout() async {
    final String? rt = await SecureStorage.getRefreshToken();
    try {
      await _api.logout(refreshToken: rt);
    } on Object catch (e) {
      AppLogger.I.w('logout-api failed (ignored): $e');
    }
    ApiClient.I.clearTokens();
    await SecureStorage.clearTokens();
  }

  Future<bool> hasTokens() async {
    final String? a = await SecureStorage.getAccessToken();
    final String? r = await SecureStorage.getRefreshToken();
    return a != null && a.isNotEmpty && r != null && r.isNotEmpty;
  }

  Future<void> _persist(AuthResponse r) async {
    ApiClient.I.setTokens(r.tokens.accessToken, r.tokens.refreshToken);
    await SecureStorage.setTokens(r.tokens.accessToken, r.tokens.refreshToken);
    await SecureStorage.setUserId(r.user.id);
  }
}
