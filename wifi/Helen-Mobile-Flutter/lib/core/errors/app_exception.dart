/// Typed exception hierarchy.
///
/// All errors that surface in the UI should be one of these — the global
/// error widgets render `AppException.userMessage` directly.
library;

import 'package:dio/dio.dart';

sealed class AppException implements Exception {
  AppException(this.message, {this.cause, this.stack, this.code});
  final String message;
  final Object? cause;
  final StackTrace? stack;
  final String? code;

  /// Localized user-facing string. Override in subclasses if needed.
  String get userMessage => message;

  @override
  String toString() => '$runtimeType($code): $message';
}

class NetworkException extends AppException {
  NetworkException(super.message, {super.cause, super.stack, super.code});
  factory NetworkException.fromDio(DioException e) {
    switch (e.type) {
      case DioExceptionType.connectionTimeout:
      case DioExceptionType.sendTimeout:
      case DioExceptionType.receiveTimeout:
        return NetworkException('Network timeout', cause: e, code: 'timeout');
      case DioExceptionType.badCertificate:
        return NetworkException('Bad TLS certificate',
            cause: e, code: 'bad_cert');
      case DioExceptionType.connectionError:
        return NetworkException('Cannot reach server',
            cause: e, code: 'unreachable');
      case DioExceptionType.cancel:
        return NetworkException('Request cancelled',
            cause: e, code: 'cancelled');
      case DioExceptionType.badResponse:
      case DioExceptionType.unknown:
        return NetworkException(e.message ?? 'Network error',
            cause: e, code: 'unknown');
    }
  }
}

class AuthException extends AppException {
  AuthException(super.message, {super.cause, super.stack, super.code});
  factory AuthException.invalidCredentials() =>
      AuthException('Invalid username or password', code: 'invalid_credentials');
  factory AuthException.tokenExpired() =>
      AuthException('Session expired', code: 'token_expired');
  factory AuthException.locked() =>
      AuthException('Account temporarily locked', code: 'account_locked');
  factory AuthException.rateLimited() =>
      AuthException('Too many attempts, slow down', code: 'rate_limited');
}

class ServerException extends AppException {
  ServerException(super.message,
      {super.cause, super.stack, super.code, this.statusCode});
  final int? statusCode;
}

class ValidationException extends AppException {
  ValidationException(super.message, {super.cause, super.stack, super.code});
}

class StorageException extends AppException {
  StorageException(super.message, {super.cause, super.stack, super.code});
}

class NotFoundException extends AppException {
  NotFoundException(super.message, {super.cause, super.stack, super.code});
}

class PermissionException extends AppException {
  PermissionException(super.message, {super.cause, super.stack, super.code});
}

class UnknownException extends AppException {
  UnknownException(super.message, {super.cause, super.stack, super.code});
}

/// Convert *any* error into an [AppException].
AppException toAppException(Object error, [StackTrace? st]) {
  if (error is AppException) return error;
  if (error is DioException) {
    final int? status = error.response?.statusCode;
    if (status == 401) return AuthException.tokenExpired();
    if (status == 403) {
      return AuthException('Permission denied', cause: error, code: 'forbidden');
    }
    if (status == 404) {
      return NotFoundException(_extractMessage(error) ?? 'Not found',
          cause: error, code: 'not_found');
    }
    if (status == 423) return AuthException.locked();
    if (status == 429) return AuthException.rateLimited();
    if (status != null && status >= 500) {
      return ServerException(_extractMessage(error) ?? 'Server error',
          cause: error, code: 'server', statusCode: status);
    }
    if (status != null && status >= 400) {
      return ValidationException(_extractMessage(error) ?? 'Bad request',
          cause: error, code: 'bad_request');
    }
    return NetworkException.fromDio(error);
  }
  return UnknownException(error.toString(), cause: error, stack: st);
}

String? _extractMessage(DioException e) {
  final dynamic data = e.response?.data;
  if (data is Map) {
    final Object? d = data['detail'] ?? data['error'] ?? data['message'];
    if (d is String) return d;
  }
  return null;
}
