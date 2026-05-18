/// Biometric authentication — local_auth wrapper.
library;

import 'package:local_auth/local_auth.dart';

import '../core/logger/app_logger.dart';

class BiometricAuth {
  BiometricAuth._();
  static final BiometricAuth I = BiometricAuth._();

  final LocalAuthentication _auth = LocalAuthentication();

  Future<bool> get isAvailable async {
    try {
      final bool supported = await _auth.isDeviceSupported();
      final bool can = await _auth.canCheckBiometrics;
      return supported && can;
    } on Object catch (e) {
      AppLogger.I.w('biometric: probe failed $e');
      return false;
    }
  }

  Future<List<BiometricType>> enrolledTypes() async {
    try {
      return await _auth.getAvailableBiometrics();
    } on Object {
      return const <BiometricType>[];
    }
  }

  Future<bool> authenticate({String reason = 'Unlock Helen'}) async {
    try {
      return await _auth.authenticate(
        localizedReason: reason,
        options: const AuthenticationOptions(
          biometricOnly: false,
          stickyAuth: true,
          useErrorDialogs: true,
        ),
      );
    } on Object catch (e) {
      AppLogger.I.w('biometric: auth failed $e');
      return false;
    }
  }
}
