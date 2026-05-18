/// Dio-based HTTP client with auth, retry, and refresh interceptors.
///
/// Mirrors the desktop renderer's `api.client.ts`:
///   - Bearer token attached when present
///   - 401 → refresh-once-and-retry, single-flight
///   - 429 → honor Retry-After once
///   - 5xx / connection errors → exponential backoff up to N attempts
library;

import 'dart:async';
import 'dart:math' as math;

import 'package:dio/dio.dart';

import '../../core/config/constants.dart';
import '../../core/config/env.dart';
import '../../core/errors/app_exception.dart';
import '../../core/logger/app_logger.dart';
import '../../core/storage/secure_storage.dart';

typedef OnTokensRefreshed = void Function(String access, String refresh);
typedef OnAuthFailed = void Function();

class ApiClient {
  ApiClient._internal();
  static final ApiClient I = ApiClient._internal();

  late Dio _dio;
  String? _accessToken;
  String? _refreshToken;

  OnTokensRefreshed? onTokensRefreshed;
  OnAuthFailed? onAuthFailed;

  /// Single-flight refresh guard — concurrent 401s collapse to one round-trip.
  Completer<bool>? _refreshFlight;

  void configure({
    String? baseUrl,
    String? accessToken,
    String? refreshToken,
    OnTokensRefreshed? onTokensRefreshed,
    OnAuthFailed? onAuthFailed,
  }) {
    _accessToken = accessToken;
    _refreshToken = refreshToken;
    this.onTokensRefreshed = onTokensRefreshed ?? this.onTokensRefreshed;
    this.onAuthFailed = onAuthFailed ?? this.onAuthFailed;

    _dio = Dio(BaseOptions(
      baseUrl: baseUrl ?? Env.I.apiBaseUrl,
      connectTimeout: Duration(milliseconds: Env.I.connectTimeoutMs),
      receiveTimeout: Duration(milliseconds: Env.I.receiveTimeoutMs),
      sendTimeout: Duration(milliseconds: Env.I.receiveTimeoutMs),
      headers: <String, dynamic>{
        'Accept': 'application/json',
        'User-Agent': 'HelenMobile-Flutter/1.0',
      },
      validateStatus: (int? status) => status != null && status < 500,
    ));

    _dio.interceptors.add(_AuthInterceptor(this));
    _dio.interceptors.add(_RetryInterceptor(this));
    _dio.interceptors.add(_LoggingInterceptor());
  }

  Dio get dio => _dio;

  String? get accessToken => _accessToken;
  String? get refreshToken => _refreshToken;

  void setTokens(String access, String refresh) {
    _accessToken = access;
    _refreshToken = refresh;
  }

  void clearTokens() {
    _accessToken = null;
    _refreshToken = null;
  }

  /// Public refresh helper — used by socket reconnect logic & non-REST callers.
  Future<bool> refreshTokensIfPossible() => _refreshTokens();

  Future<bool> _refreshTokens() {
    final String? rt = _refreshToken;
    if (rt == null || rt.isEmpty) return Future<bool>.value(false);

    final Completer<bool>? inflight = _refreshFlight;
    if (inflight != null) return inflight.future;

    final Completer<bool> c = Completer<bool>();
    _refreshFlight = c;
    Future<void>(() async {
      try {
        // Bypass interceptors to avoid recursive 401 handling.
        final Dio bare = Dio(BaseOptions(
          baseUrl: _dio.options.baseUrl,
          connectTimeout: const Duration(seconds: 8),
          receiveTimeout: const Duration(seconds: 8),
        ));
        final Response<Map<String, dynamic>> res =
            await bare.post<Map<String, dynamic>>(
          K.pAuthRefresh,
          data: <String, String>{'refresh_token': rt},
        );
        if (res.statusCode == 200 && res.data != null) {
          final String a = res.data!['access_token'] as String;
          final String r = res.data!['refresh_token'] as String;
          _accessToken = a;
          _refreshToken = r;
          await SecureStorage.setTokens(a, r);
          onTokensRefreshed?.call(a, r);
          c.complete(true);
        } else {
          c.complete(false);
        }
      } on Object catch (e, st) {
        AppLogger.I.w('refresh failed', e, st);
        c.complete(false);
      } finally {
        _refreshFlight = null;
      }
    });
    return c.future;
  }

  // ── Convenience verbs ─────────────────────────────────────────────

  Future<T> get<T>(String path,
          {Map<String, dynamic>? query, Options? options}) async =>
      _unwrap<T>(await _dio.get<dynamic>(path,
          queryParameters: query, options: options));

  Future<T> post<T>(String path,
      {Object? body,
      Map<String, dynamic>? query,
      Options? options}) async =>
      _unwrap<T>(await _dio.post<dynamic>(path,
          data: body, queryParameters: query, options: options));

  Future<T> patch<T>(String path,
      {Object? body, Options? options}) async =>
      _unwrap<T>(
          await _dio.patch<dynamic>(path, data: body, options: options));

  Future<T> put<T>(String path,
      {Object? body, Options? options}) async =>
      _unwrap<T>(await _dio.put<dynamic>(path, data: body, options: options));

  Future<T> delete<T>(String path, {Object? body, Options? options}) async =>
      _unwrap<T>(
          await _dio.delete<dynamic>(path, data: body, options: options));

  T _unwrap<T>(Response<dynamic> r) {
    final int status = r.statusCode ?? 0;
    if (status >= 200 && status < 300) {
      if (T == dynamic || r.data == null) return r.data as T;
      return r.data as T;
    }
    throw DioException(
      requestOptions: r.requestOptions,
      response: r,
      type: DioExceptionType.badResponse,
      error: 'HTTP $status',
    );
  }
}

class _AuthInterceptor extends Interceptor {
  _AuthInterceptor(this._client);
  final ApiClient _client;

  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    final String? t = _client.accessToken;
    if (t != null && t.isNotEmpty) {
      options.headers['Authorization'] = 'Bearer $t';
    }
    handler.next(options);
  }

  @override
  Future<void> onResponse(
      Response<dynamic> response, ResponseInterceptorHandler handler) async {
    if (response.statusCode == 401) {
      // Don't refresh-loop on the refresh endpoint itself.
      if (response.requestOptions.path.contains(K.pAuthRefresh)) {
        return handler.next(response);
      }
      final bool ok = await _client._refreshTokens();
      if (ok) {
        try {
          final Response<dynamic> retry = await _retry(response.requestOptions);
          return handler.resolve(retry);
        } on DioException catch (e) {
          return handler.reject(e);
        }
      }
      _client.onAuthFailed?.call();
    }
    return handler.next(response);
  }

  Future<Response<dynamic>> _retry(RequestOptions req) {
    final Options o = Options(
      method: req.method,
      headers: <String, dynamic>{
        ...req.headers,
        'Authorization': 'Bearer ${_client.accessToken ?? ''}',
      },
      contentType: req.contentType,
      responseType: req.responseType,
    );
    return _client.dio.request<dynamic>(
      req.path,
      data: req.data,
      queryParameters: req.queryParameters,
      options: o,
      cancelToken: req.cancelToken,
    );
  }
}

class _RetryInterceptor extends Interceptor {
  _RetryInterceptor(this._client);
  final ApiClient _client;

  static const String _kAttempt = '__retry_attempt';

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) async {
    final RequestOptions req = err.requestOptions;
    final int attempt = (req.extra[_kAttempt] as int?) ?? 0;
    final bool retriable = _isRetriable(err);
    final int max = Env.I.retryAttempts;

    if (!retriable || attempt >= max) {
      return handler.next(err);
    }

    // 429 → respect Retry-After once (cap 5s).
    Duration wait;
    final int? status = err.response?.statusCode;
    if (status == 429) {
      final String? ra = err.response?.headers.value('retry-after');
      final int secs = int.tryParse(ra ?? '') ?? 1;
      wait = Duration(milliseconds: math.min(5000, math.max(250, secs * 1000)));
    } else {
      // Exponential backoff with jitter: 500ms, 1s, 2s + 0–250ms jitter.
      final int base = 500 * (1 << attempt);
      final int jitter = math.Random().nextInt(250);
      wait = Duration(milliseconds: base + jitter);
    }

    await Future<void>.delayed(wait);
    req.extra[_kAttempt] = attempt + 1;
    try {
      final Response<dynamic> r = await _client.dio.fetch<dynamic>(req);
      return handler.resolve(r);
    } on DioException catch (e) {
      return handler.reject(e);
    }
  }

  bool _isRetriable(DioException err) {
    if (err.type == DioExceptionType.connectionTimeout ||
        err.type == DioExceptionType.receiveTimeout ||
        err.type == DioExceptionType.sendTimeout ||
        err.type == DioExceptionType.connectionError) {
      return true;
    }
    final int? s = err.response?.statusCode;
    if (s == null) return false;
    return s == 429 || (s >= 500 && s <= 599);
  }
}

class _LoggingInterceptor extends Interceptor {
  @override
  void onRequest(RequestOptions options, RequestInterceptorHandler handler) {
    AppLogger.I.d('→ ${options.method} ${options.uri}');
    handler.next(options);
  }

  @override
  void onResponse(Response<dynamic> response, ResponseInterceptorHandler handler) {
    AppLogger.I.d(
      '← ${response.statusCode} ${response.requestOptions.method} ${response.requestOptions.uri}',
    );
    handler.next(response);
  }

  @override
  void onError(DioException err, ErrorInterceptorHandler handler) {
    AppLogger.I.w(
      '× ${err.response?.statusCode ?? '-'} ${err.requestOptions.method} ${err.requestOptions.uri}  ${err.message ?? ''}',
    );
    handler.next(err);
  }
}

/// Top-level helper: convert thrown [DioException] / generic errors into
/// the typed [AppException] hierarchy. Use in repos around `await ApiClient.*`.
Future<T> guardApi<T>(Future<T> Function() body) async {
  try {
    return await body();
  } on DioException catch (e, st) {
    throw toAppException(e, st);
  } on Object catch (e, st) {
    throw toAppException(e, st);
  }
}
