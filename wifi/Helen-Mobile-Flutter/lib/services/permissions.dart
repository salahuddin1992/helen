/// Permissions facade — runtime requests for camera/mic/notifications.
library;

import 'package:permission_handler/permission_handler.dart';

import '../core/logger/app_logger.dart';

class AppPermissions {
  AppPermissions._();

  static Future<bool> requestCamera() async =>
      _request(Permission.camera, 'camera');

  static Future<bool> requestMicrophone() async =>
      _request(Permission.microphone, 'microphone');

  static Future<bool> requestNotifications() async =>
      _request(Permission.notification, 'notification');

  /// Calls require *both* mic + camera (for video) or mic-only (for audio).
  static Future<bool> requestCallPermissions({required bool video}) async {
    final Map<Permission, PermissionStatus> map = await <Permission>[
      Permission.microphone,
      if (video) Permission.camera,
      Permission.bluetoothConnect,
    ].request();
    return map.values.every((PermissionStatus s) => s.isGranted);
  }

  static Future<bool> _request(Permission p, String label) async {
    PermissionStatus s = await p.status;
    if (s.isGranted) return true;
    if (s.isPermanentlyDenied) {
      AppLogger.I.w('perm: $label permanently denied');
      return false;
    }
    s = await p.request();
    AppLogger.I.i('perm: $label → ${s.name}');
    return s.isGranted;
  }
}
