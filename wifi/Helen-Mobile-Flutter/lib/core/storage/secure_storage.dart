/// Thin wrapper around [flutter_secure_storage] with typed accessors
/// for tokens + server config.
///
/// On Android: AES-256 inside EncryptedSharedPreferences (StrongBox when
/// available). On iOS: Keychain with first-unlock accessibility.
library;

import 'package:flutter_secure_storage/flutter_secure_storage.dart';

import '../config/constants.dart';
import '../errors/app_exception.dart';

class SecureStorage {
  SecureStorage._();
  static final FlutterSecureStorage _s = FlutterSecureStorage(
    aOptions: const AndroidOptions(
      encryptedSharedPreferences: true,
      resetOnError: true,
    ),
    iOptions: const IOSOptions(
      accessibility: KeychainAccessibility.first_unlock,
    ),
  );

  static Future<void> init() async {
    // Touch a key to surface init errors early (corrupted keystore, etc.).
    try {
      await _s.containsKey(key: K.kAccessToken);
    } on Object catch (e, st) {
      throw StorageException('Secure storage init failed', cause: e, stack: st);
    }
  }

  // ── Tokens ────────────────────────────────────────────────────────

  static Future<String?> getAccessToken() async =>
      _s.read(key: K.kAccessToken);

  static Future<String?> getRefreshToken() async =>
      _s.read(key: K.kRefreshToken);

  static Future<void> setTokens(String access, String refresh) async {
    await _s.write(key: K.kAccessToken, value: access);
    await _s.write(key: K.kRefreshToken, value: refresh);
  }

  static Future<void> clearTokens() async {
    await _s.delete(key: K.kAccessToken);
    await _s.delete(key: K.kRefreshToken);
  }

  // ── User ──────────────────────────────────────────────────────────

  static Future<String?> getUserId() async => _s.read(key: K.kUserId);
  static Future<void> setUserId(String id) async =>
      _s.write(key: K.kUserId, value: id);

  // ── Server config ─────────────────────────────────────────────────

  static Future<String?> getServerUrl() async => _s.read(key: K.kServerUrl);
  static Future<void> setServerUrl(String url) async =>
      _s.write(key: K.kServerUrl, value: url);
  static Future<String?> getSocketUrl() async => _s.read(key: K.kSocketUrl);
  static Future<void> setSocketUrl(String url) async =>
      _s.write(key: K.kSocketUrl, value: url);

  // ── Biometric ─────────────────────────────────────────────────────

  static Future<bool> isBiometricEnabled() async =>
      (await _s.read(key: K.kBiometricEnabled)) == '1';
  static Future<void> setBiometricEnabled(bool v) async =>
      _s.write(key: K.kBiometricEnabled, value: v ? '1' : '0');

  // ── Push token ────────────────────────────────────────────────────

  static Future<String?> getPushToken() async => _s.read(key: K.kPushToken);
  static Future<void> setPushToken(String token) async =>
      _s.write(key: K.kPushToken, value: token);

  // ── Nuke ──────────────────────────────────────────────────────────

  /// Wipes EVERYTHING (logout-all). Survives a single key being missing.
  static Future<void> wipe() async => _s.deleteAll();
}
