import 'package:dio/dio.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:http_mock_adapter/http_mock_adapter.dart';

import 'package:helen_mobile/core/config/env.dart';
import 'package:helen_mobile/data/models/auth_tokens.dart';
import 'package:helen_mobile/data/models/user.dart';

void main() {
  setUpAll(() {
    Env.bootstrap();
  });

  group('Auth REST contract', () {
    late Dio dio;
    late DioAdapter adapter;

    setUp(() {
      dio = Dio(BaseOptions(baseUrl: 'http://test.local'));
      adapter = DioAdapter(dio: dio);
    });

    test('/api/auth/login returns AuthResponse-shaped JSON', () async {
      adapter.onPost(
        '/api/auth/login',
        (MockServer s) => s.reply(200, <String, dynamic>{
          'user': <String, dynamic>{
            'id': 'u1',
            'username': 'alice',
            'display_name': 'Alice',
            'status': 'online',
            'role': 'user',
          },
          'tokens': <String, dynamic>{
            'access_token': 'a',
            'refresh_token': 'r',
            'expires_in': 3600,
          },
        }),
        data: <String, dynamic>{'username': 'alice', 'password': 'pw'},
      );

      final Response<Map<String, dynamic>> r =
          await dio.post<Map<String, dynamic>>(
        '/api/auth/login',
        data: <String, dynamic>{'username': 'alice', 'password': 'pw'},
      );
      expect(r.statusCode, 200);

      final AuthResponse parsed = AuthResponse.fromJson(r.data!);
      expect(parsed.user.username, 'alice');
      expect(parsed.user.displayName, 'Alice');
      expect(parsed.tokens.accessToken, 'a');
      expect(parsed.tokens.refreshToken, 'r');
      expect(parsed.tokens.expiresIn, 3600);
    });

    test('/api/auth/refresh returns AuthTokens-shaped JSON', () async {
      adapter.onPost(
        '/api/auth/refresh',
        (MockServer s) => s.reply(200, <String, dynamic>{
          'access_token': 'a2',
          'refresh_token': 'r2',
          'expires_in': 1800,
        }),
        data: <String, dynamic>{'refresh_token': 'r1'},
      );
      final Response<Map<String, dynamic>> r =
          await dio.post<Map<String, dynamic>>(
        '/api/auth/refresh',
        data: <String, dynamic>{'refresh_token': 'r1'},
      );
      final AuthTokens t = AuthTokens.fromJson(r.data!);
      expect(t.accessToken, 'a2');
      expect(t.refreshToken, 'r2');
      expect(t.expiresIn, 1800);
    });

    test('User.fromJson handles minimal payload', () {
      final User u = User.fromJson(<String, dynamic>{
        'id': 'u1',
        'username': 'bob',
        'display_name': 'Bob',
      });
      expect(u.id, 'u1');
      expect(u.username, 'bob');
      expect(u.role, 'user');
      expect(u.status, 'offline');
    });
  });
}
