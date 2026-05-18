/// Global error handler.
///
/// Catches:
///   - Flutter framework errors (`FlutterError.onError`)
///   - Platform/zone errors (`PlatformDispatcher.instance.onError`)
///   - Unawaited async errors via the `runZonedGuarded` boundary in main
library;

import 'dart:async';

import 'package:flutter/foundation.dart';

import '../logger/app_logger.dart';
import 'app_exception.dart';

class ErrorHandler {
  ErrorHandler._();

  static final StreamController<AppException> _stream =
      StreamController<AppException>.broadcast();

  /// UI layer subscribes to surface toasts/snackbars on terminal errors.
  static Stream<AppException> get errors => _stream.stream;

  static void handleFlutterError(FlutterErrorDetails details) {
    AppLogger.I.e(
      'flutter-error: ${details.summary}',
      details.exception,
      details.stack,
    );
    if (kDebugMode) {
      FlutterError.presentError(details);
    }
    _stream.add(toAppException(details.exception, details.stack));
  }

  static bool handlePlatformError(Object error, StackTrace st) {
    AppLogger.I.e('platform-error', error, st);
    _stream.add(toAppException(error, st));
    return true;
  }

  static void handleZoneError(Object error, StackTrace st) {
    AppLogger.I.e('zone-error', error, st);
    _stream.add(toAppException(error, st));
  }

  /// Wrap a future so any error becomes an [AppException] and is logged.
  /// Callers receive a [Result]-style tuple to avoid throw-in-async pitfalls.
  static Future<({T? value, AppException? error})> guard<T>(
    Future<T> Function() body, {
    String? tag,
  }) async {
    try {
      final T v = await body();
      return (value: v, error: null);
    } on Object catch (e, st) {
      final AppException ex = toAppException(e, st);
      AppLogger.I.e('guard:${tag ?? 'op'}', ex, st);
      return (value: null, error: ex);
    }
  }
}
