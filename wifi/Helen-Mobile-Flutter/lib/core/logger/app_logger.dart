/// Application logger.
///
/// Wraps the `logger` package, adds:
///   - level configurable via Env.logLevel
///   - file mirror under app docs dir (`helen.log`)
///   - structured fields for analytics correlation
///
/// Usage:
///   AppLogger.I.i('login: success', null, null, {'user_id': id});
library;

import 'dart:async';
import 'dart:io';

import 'package:logger/logger.dart';
import 'package:path/path.dart' as p;
import 'package:path_provider/path_provider.dart';

import '../config/env.dart';

class AppLogger {
  AppLogger._(this._logger, this._sink);

  static AppLogger? _instance;
  static AppLogger get I {
    final AppLogger? i = _instance;
    if (i == null) {
      throw StateError('AppLogger.init() must be called before AppLogger.I');
    }
    return i;
  }

  final Logger _logger;
  final IOSink? _sink;

  static Future<void> init() async {
    if (_instance != null) return;

    IOSink? sink;
    try {
      final Directory dir = await getApplicationDocumentsDirectory();
      final File logFile = File(p.join(dir.path, 'helen.log'));
      // Rotate at ~5 MiB.
      if (await logFile.exists() && (await logFile.length()) > 5 * 1024 * 1024) {
        final File backup = File('${logFile.path}.1');
        if (await backup.exists()) await backup.delete();
        await logFile.rename(backup.path);
      }
      sink = logFile.openWrite(mode: FileMode.append);
    } on Object {
      // Fall through — console-only logging is acceptable.
      sink = null;
    }

    final Level level = _resolveLevel();
    final Logger logger = Logger(
      level: level,
      filter: ProductionFilter(),
      printer: PrettyPrinter(
        methodCount: 0,
        errorMethodCount: 12,
        lineLength: 100,
        colors: false,
        printEmojis: false,
        dateTimeFormat: DateTimeFormat.dateAndTime,
      ),
      output: _ForkOutput(sink),
    );
    _instance = AppLogger._(logger, sink);
  }

  static Level _resolveLevel() {
    final String l = (() {
      try {
        return Env.I.logLevel;
      } on StateError {
        return 'info';
      }
    })()
        .toLowerCase();
    switch (l) {
      case 'trace':
        return Level.trace;
      case 'debug':
        return Level.debug;
      case 'info':
        return Level.info;
      case 'warn':
      case 'warning':
        return Level.warning;
      case 'error':
        return Level.error;
      case 'fatal':
        return Level.fatal;
      case 'off':
      case 'silent':
        return Level.off;
      default:
        return Level.info;
    }
  }

  void t(String msg, [Object? error, StackTrace? st, Map<String, Object?>? fields]) =>
      _logger.t(_compose(msg, fields), error: error, stackTrace: st);
  void d(String msg, [Object? error, StackTrace? st, Map<String, Object?>? fields]) =>
      _logger.d(_compose(msg, fields), error: error, stackTrace: st);
  void i(String msg, [Object? error, StackTrace? st, Map<String, Object?>? fields]) =>
      _logger.i(_compose(msg, fields), error: error, stackTrace: st);
  void w(String msg, [Object? error, StackTrace? st, Map<String, Object?>? fields]) =>
      _logger.w(_compose(msg, fields), error: error, stackTrace: st);
  void e(String msg, [Object? error, StackTrace? st, Map<String, Object?>? fields]) =>
      _logger.e(_compose(msg, fields), error: error, stackTrace: st);
  void f(String msg, [Object? error, StackTrace? st, Map<String, Object?>? fields]) =>
      _logger.f(_compose(msg, fields), error: error, stackTrace: st);

  String _compose(String msg, Map<String, Object?>? fields) {
    if (fields == null || fields.isEmpty) return msg;
    final String suffix = fields.entries
        .map((MapEntry<String, Object?> e) => '${e.key}=${e.value}')
        .join(' ');
    return '$msg  $suffix';
  }

  Future<void> dispose() async {
    await _sink?.flush();
    await _sink?.close();
  }
}

class _ForkOutput extends LogOutput {
  _ForkOutput(this._sink);
  final IOSink? _sink;
  @override
  void output(OutputEvent event) {
    for (final String line in event.lines) {
      // ignore: avoid_print
      print(line);
      try {
        _sink?.writeln(line);
      } on Object {
        // ignore
      }
    }
  }
}
