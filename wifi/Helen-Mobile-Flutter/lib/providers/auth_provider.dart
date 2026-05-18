/// Auth state + repository providers.
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../core/errors/app_exception.dart';
import '../core/logger/app_logger.dart';
import '../core/storage/secure_storage.dart';
import '../data/api/api_client.dart';
import '../data/api/auth_api.dart';
import '../data/models/auth_tokens.dart';
import '../data/models/user.dart';
import '../domain/repositories/auth_repository.dart';

// ── Bootstrap ──────────────────────────────────────────────────────

final Provider<ApiClient> apiClientProvider =
    Provider<ApiClient>((Ref ref) => ApiClient.I);

final Provider<AuthApi> authApiProvider = Provider<AuthApi>(
  (Ref ref) => AuthApi(ref.watch(apiClientProvider)),
);

final Provider<AuthRepository> authRepositoryProvider =
    Provider<AuthRepository>(
  (Ref ref) => AuthRepository(ref.watch(authApiProvider)),
);

// ── State ──────────────────────────────────────────────────────────

sealed class AuthState {
  const AuthState();
}

class AuthInitial extends AuthState {
  const AuthInitial();
}

class AuthLoading extends AuthState {
  const AuthLoading();
}

class AuthAuthenticated extends AuthState {
  const AuthAuthenticated(this.user);
  final User user;
}

class AuthUnauthenticated extends AuthState {
  const AuthUnauthenticated();
}

class AuthFailed extends AuthState {
  const AuthFailed(this.error);
  final AppException error;
}

// ── Controller ─────────────────────────────────────────────────────

class AuthController extends StateNotifier<AuthState> {
  AuthController(this._repo) : super(const AuthInitial()) {
    unawaited(_restore());
  }

  final AuthRepository _repo;

  Future<void> _restore() async {
    state = const AuthLoading();
    try {
      final String? a = await SecureStorage.getAccessToken();
      final String? r = await SecureStorage.getRefreshToken();
      if (a == null || r == null) {
        state = const AuthUnauthenticated();
        return;
      }
      ApiClient.I.setTokens(a, r);
      final User u = await _repo.me();
      state = AuthAuthenticated(u);
    } on AppException catch (e) {
      AppLogger.I.w('auth restore failed: ${e.message}');
      // refresh-once is automatic via interceptor; if /me still 401's,
      // we drop to unauth.
      if (e is AuthException) {
        await SecureStorage.clearTokens();
        ApiClient.I.clearTokens();
        state = const AuthUnauthenticated();
      } else {
        state = AuthFailed(e);
      }
    } on Object catch (e, st) {
      state = AuthFailed(toAppException(e, st));
    }
  }

  Future<void> login(String username, String password,
      {String? deviceName}) async {
    state = const AuthLoading();
    try {
      final AuthResponse r = await _repo.login(
        username: username,
        password: password,
        deviceName: deviceName,
      );
      state = AuthAuthenticated(r.user);
    } on AppException catch (e) {
      state = AuthFailed(e);
      rethrow;
    } on Object catch (e, st) {
      final AppException ex = toAppException(e, st);
      state = AuthFailed(ex);
      throw ex;
    }
  }

  Future<void> register(String username, String displayName,
      String password) async {
    state = const AuthLoading();
    try {
      final AuthResponse r = await _repo.register(
        username: username,
        displayName: displayName,
        password: password,
      );
      state = AuthAuthenticated(r.user);
    } on AppException catch (e) {
      state = AuthFailed(e);
      rethrow;
    } on Object catch (e, st) {
      final AppException ex = toAppException(e, st);
      state = AuthFailed(ex);
      throw ex;
    }
  }

  Future<void> logout() async {
    state = const AuthLoading();
    await _repo.logout();
    state = const AuthUnauthenticated();
  }

  /// Used by router redirect — synchronous check that doesn't await.
  bool get isAuthed => state is AuthAuthenticated;
}

void unawaited(Future<Object?> f) {
  // We intentionally fire and forget the initial restore.
  f.then((_) {}, onError: (Object _, StackTrace __) {});
}

final StateNotifierProvider<AuthController, AuthState> authProvider =
    StateNotifierProvider<AuthController, AuthState>(
  (Ref ref) => AuthController(ref.watch(authRepositoryProvider)),
);
