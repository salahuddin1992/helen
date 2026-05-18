/// Module N — OAuth / SSO.
///
/// Mobile flow uses the platform's external-user-agent (`url_launcher`
/// with mode `externalApplication`) and a custom URL scheme callback
/// (`helen://oauth/callback?provider=…&code=…&state=…`).
library;

import '../../core/config/constants.dart';
import 'api_client.dart';

class OauthApi {
  OauthApi(this._client);
  final ApiClient _client;

  /// Enumerate enabled providers (google, microsoft, github, oidc-custom).
  Future<List<Map<String, dynamic>>> listProviders() async {
    return guardApi(() async {
      final dynamic raw = await _client.get<dynamic>(K.pOauthProviders);
      final List<dynamic> arr = raw is List<dynamic>
          ? raw
          : (raw['providers'] as List<dynamic>? ?? <dynamic>[]);
      return arr.cast<Map<String, dynamic>>();
    });
  }

  /// Server returns an authorization URL + state nonce. Caller launches it
  /// in the platform browser; the provider redirects back to
  /// `helen://oauth/callback` with `code` + `state`.
  Future<Map<String, dynamic>> buildAuthorizeUrl({
    required String provider,
    required String redirectUri,
  }) async {
    return guardApi(() async {
      return _client.get<Map<String, dynamic>>(
        K.pOauthAuthorize(provider),
        query: <String, dynamic>{'redirect_uri': redirectUri},
      );
    });
  }

  /// Exchange the OAuth code for Helen JWT tokens.
  Future<Map<String, dynamic>> exchangeCode({
    required String provider,
    required String code,
    required String state,
    String? redirectUri,
  }) async {
    return guardApi(() async {
      return _client.post<Map<String, dynamic>>(
        K.pOauthCallback(provider),
        body: <String, dynamic>{
          'code': code,
          'state': state,
          if (redirectUri != null) 'redirect_uri': redirectUri,
        },
      );
    });
  }
}
