/// Auth REST endpoints — matches `app/api/routes/auth.py`.
library;

import '../../core/config/constants.dart';
import '../models/auth_tokens.dart';
import '../models/user.dart';
import 'api_client.dart';

class AuthApi {
  AuthApi(this._client);
  final ApiClient _client;

  Future<AuthResponse> register({
    required String username,
    required String displayName,
    required String password,
    String? avatarUrl,
    String? bio,
  }) async {
    return guardApi(() async {
      final Map<String, dynamic> data = await _client.post<Map<String, dynamic>>(
        K.pAuthRegister,
        body: <String, dynamic>{
          'username': username,
          'display_name': displayName,
          'password': password,
          if (avatarUrl != null) 'avatar_url': avatarUrl,
          if (bio != null) 'bio': bio,
        },
      );
      return AuthResponse.fromJson(data);
    });
  }

  Future<AuthResponse> login({
    required String username,
    required String password,
    String? deviceName,
  }) async {
    return guardApi(() async {
      final Map<String, dynamic> data = await _client.post<Map<String, dynamic>>(
        K.pAuthLogin,
        body: <String, dynamic>{
          'username': username,
          'password': password,
          if (deviceName != null) 'device_name': deviceName,
        },
      );
      return AuthResponse.fromJson(data);
    });
  }

  Future<AuthTokens> refresh(String refreshToken) async {
    return guardApi(() async {
      final Map<String, dynamic> data = await _client.post<Map<String, dynamic>>(
        K.pAuthRefresh,
        body: <String, String>{'refresh_token': refreshToken},
      );
      return AuthTokens.fromJson(data);
    });
  }

  Future<void> logout({String? refreshToken}) async {
    return guardApi(() async {
      await _client.post<dynamic>(
        K.pAuthLogout,
        body: refreshToken != null
            ? <String, String>{'refresh_token': refreshToken}
            : null,
      );
    });
  }

  Future<User> getMe() async {
    return guardApi(() async {
      final Map<String, dynamic> data =
          await _client.get<Map<String, dynamic>>(K.pUsersMe);
      return User.fromJson(data);
    });
  }

  Future<void> changePassword({
    required String currentPassword,
    required String newPassword,
  }) async {
    return guardApi(() async {
      await _client.post<dynamic>(
        '/api/auth/change-password',
        body: <String, String>{
          'current_password': currentPassword,
          'new_password': newPassword,
        },
      );
    });
  }
}
