/// Login use-case — orchestrates the login flow with device naming.
library;

import 'package:device_info_plus/device_info_plus.dart';

import '../../data/models/auth_tokens.dart';
import '../repositories/auth_repository.dart';

class LoginUseCase {
  LoginUseCase(this._repo);
  final AuthRepository _repo;

  Future<AuthResponse> call({
    required String username,
    required String password,
  }) async {
    final String name = await _deviceName();
    return _repo.login(
      username: username,
      password: password,
      deviceName: name,
    );
  }

  Future<String> _deviceName() async {
    try {
      final DeviceInfoPlugin info = DeviceInfoPlugin();
      try {
        final AndroidDeviceInfo a = await info.androidInfo;
        return '${a.manufacturer} ${a.model}';
      } on Object {
        // not android
      }
      try {
        final IosDeviceInfo i = await info.iosInfo;
        return '${i.name} (${i.model})';
      } on Object {
        // not ios
      }
    } on Object {
      // ignore
    }
    return 'Helen Mobile';
  }
}
