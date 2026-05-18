/// Push notification service — FCM on Android, APNs on iOS.
///
/// Initializes Firebase, requests permission, registers token with server,
/// shows local notifications for foreground delivery.
library;

import 'dart:async';
import 'dart:io';

import 'package:firebase_core/firebase_core.dart';
import 'package:firebase_messaging/firebase_messaging.dart';
import 'package:flutter/foundation.dart';
import 'package:flutter_local_notifications/flutter_local_notifications.dart';

import '../core/logger/app_logger.dart';
import '../core/storage/secure_storage.dart';

class PushNotifications {
  PushNotifications._();
  static final PushNotifications I = PushNotifications._();

  final FlutterLocalNotificationsPlugin _local =
      FlutterLocalNotificationsPlugin();

  bool _initialized = false;
  String? _fcmToken;
  String? get fcmToken => _fcmToken;

  Future<void> initialize() async {
    if (_initialized) return;

    try {
      await Firebase.initializeApp();
    } on FirebaseException catch (e) {
      if (e.code != 'duplicate-app') rethrow;
    }

    final FirebaseMessaging fm = FirebaseMessaging.instance;
    final NotificationSettings settings = await fm.requestPermission(
      alert: true,
      badge: true,
      sound: true,
      provisional: false,
    );
    AppLogger.I.i('push: permission=${settings.authorizationStatus}');

    // Token
    if (Platform.isIOS) {
      // APNs token first — otherwise FCM token is null on iOS.
      await fm.getAPNSToken();
    }
    _fcmToken = await fm.getToken();
    AppLogger.I.i('push: token=${_fcmToken?.substring(0, 12) ?? "null"}…');
    if (_fcmToken != null) {
      await SecureStorage.setPushToken(_fcmToken!);
    }

    fm.onTokenRefresh.listen((String t) async {
      _fcmToken = t;
      await SecureStorage.setPushToken(t);
      AppLogger.I.i('push: token refreshed');
    });

    // Local notifications (foreground display)
    const AndroidInitializationSettings android =
        AndroidInitializationSettings('@mipmap/ic_launcher');
    const DarwinInitializationSettings ios = DarwinInitializationSettings();
    await _local.initialize(const InitializationSettings(
      android: android,
      iOS: ios,
    ));

    // Default channel for Android.
    if (Platform.isAndroid) {
      await _local
          .resolvePlatformSpecificImplementation<
              AndroidFlutterLocalNotificationsPlugin>()
          ?.createNotificationChannel(const AndroidNotificationChannel(
        'helen_messages',
        'Messages',
        description: 'Direct & channel messages',
        importance: Importance.high,
      ));
    }

    // Handlers
    FirebaseMessaging.onMessage.listen(_onForeground);
    FirebaseMessaging.onMessageOpenedApp.listen(_onTap);
    FirebaseMessaging.onBackgroundMessage(_backgroundHandler);

    _initialized = true;
  }

  Future<void> _onForeground(RemoteMessage msg) async {
    AppLogger.I.d('push: foreground ${msg.messageId}');
    final RemoteNotification? n = msg.notification;
    if (n == null) return;
    await _local.show(
      msg.hashCode,
      n.title,
      n.body,
      const NotificationDetails(
        android: AndroidNotificationDetails(
          'helen_messages',
          'Messages',
          importance: Importance.high,
          priority: Priority.high,
        ),
        iOS: DarwinNotificationDetails(
          presentAlert: true,
          presentBadge: true,
          presentSound: true,
        ),
      ),
      payload: msg.data['route'] as String?,
    );
  }

  void _onTap(RemoteMessage msg) {
    AppLogger.I.i('push: tapped ${msg.messageId}');
    // Deep-link handling — bridge into go_router via a notifier in app.
  }
}

@pragma('vm:entry-point')
Future<void> _backgroundHandler(RemoteMessage msg) async {
  // Ensure Firebase is initialized in the background isolate.
  try {
    await Firebase.initializeApp();
  } on Object {
    // ignore
  }
  if (kDebugMode) {
    // ignore: avoid_print
    print('push:bg ${msg.messageId}');
  }
}
