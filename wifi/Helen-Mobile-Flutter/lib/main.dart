/// Helen Mobile — entry point.
///
/// Boots the Flutter runtime, initializes core services
/// (logging, secure storage, push, env), then mounts `HelenApp`
/// inside a Riverpod [ProviderScope].
library;

import 'dart:async';

import 'package:flutter/foundation.dart';
import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_dotenv/flutter_dotenv.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'app.dart';
import 'core/config/env.dart';
import 'core/errors/error_handler.dart';
import 'core/logger/app_logger.dart';
import 'core/storage/secure_storage.dart';
import 'services/push_notifications.dart';

Future<void> main() async {
  // `runZonedGuarded` catches async errors that escape the framework's
  // zone — uploads, isolates, listeners that throw later. Crash data is
  // sent to the logger which mirrors to file.
  await runZonedGuarded<Future<void>>(
    () async {
      WidgetsFlutterBinding.ensureInitialized();

      // System chrome — edge-to-edge with translucent system bars.
      await SystemChrome.setPreferredOrientations(<DeviceOrientation>[
        DeviceOrientation.portraitUp,
        DeviceOrientation.portraitDown,
        DeviceOrientation.landscapeLeft,
        DeviceOrientation.landscapeRight,
      ]);
      SystemChrome.setSystemUIOverlayStyle(
        const SystemUiOverlayStyle(
          statusBarColor: Colors.transparent,
          systemNavigationBarColor: Colors.transparent,
        ),
      );

      // 1. Logger first so every later init step is observable.
      await AppLogger.init();
      AppLogger.I.i('boot: Helen Mobile starting (debug=$kDebugMode)');

      // 2. .env (best-effort — missing file falls back to compile-time defaults).
      try {
        await dotenv.load(fileName: '.env');
      } on Object catch (e) {
        AppLogger.I.w('boot: .env not found, using defaults ($e)');
      }
      Env.bootstrap();
      AppLogger.I.i('boot: API=${Env.I.apiBaseUrl}  WS=${Env.I.socketUrl}');

      // 3. Secure storage — used by auth bootstrap below.
      await SecureStorage.init();

      // 4. Push notifications (FCM/APNs). Non-fatal if unavailable
      // (e.g. simulator without Firebase config).
      try {
        await PushNotifications.I.initialize();
      } on Object catch (e, st) {
        AppLogger.I.w('boot: push init failed ($e)', e, st);
      }

      // 5. Global Flutter error handler.
      FlutterError.onError = (FlutterErrorDetails details) {
        ErrorHandler.handleFlutterError(details);
      };
      PlatformDispatcher.instance.onError = (Object e, StackTrace st) {
        ErrorHandler.handlePlatformError(e, st);
        return true;
      };

      runApp(const ProviderScope(child: HelenApp()));
    },
    (Object error, StackTrace stack) {
      ErrorHandler.handleZoneError(error, stack);
    },
  );
}
