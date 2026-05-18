/// Module O — pairing v2.
///
/// Flow (mobile-as-claimant):
///   1. Desktop generates a pairing code + QR (server: POST /pairing/v2/start)
///   2. Phone scans QR → calls `complete(code, device_info)`
///   3. Server returns access/refresh tokens for the phone identity.
///
/// Flow (phone initiates pairing to authenticated phone-user):
///   1. Phone calls `start()` to obtain a code displayed on screen
///   2. Phone polls `poll(token)` waiting for the desktop to redeem it
///   3. On redemption phone receives the channel-scoped pair session.
library;

import '../../core/config/constants.dart';
import 'api_client.dart';

class PairingApi {
  PairingApi(this._client);
  final ApiClient _client;

  /// Start a new pairing session (mobile initiates). Returns a short code
  /// the user types into their other device.
  Future<Map<String, dynamic>> start({
    String? deviceName,
    String? deviceFingerprint,
  }) async {
    return guardApi(() async {
      return _client.post<Map<String, dynamic>>(
        K.pPairV2Start,
        body: <String, dynamic>{
          if (deviceName != null) 'device_name': deviceName,
          if (deviceFingerprint != null) 'device_fingerprint': deviceFingerprint,
        },
      );
    });
  }

  /// Complete a pairing (phone scanned QR from desktop, or typed code).
  /// Server validates the code, binds the device, returns auth tokens.
  Future<Map<String, dynamic>> complete({
    required String code,
    required String deviceName,
    String? deviceFingerprint,
    String? userAgent,
  }) async {
    return guardApi(() async {
      return _client.post<Map<String, dynamic>>(
        K.pPairV2Complete,
        body: <String, dynamic>{
          'code': code,
          'device_name': deviceName,
          if (deviceFingerprint != null) 'device_fingerprint': deviceFingerprint,
          if (userAgent != null) 'user_agent': userAgent,
        },
      );
    });
  }

  /// Long-poll for the redemption of a code we generated via [start].
  Future<Map<String, dynamic>> poll(String pairToken) async {
    return guardApi(() async {
      return _client.get<Map<String, dynamic>>(
        K.pPairV2Poll,
        query: <String, dynamic>{'token': pairToken},
      );
    });
  }
}
