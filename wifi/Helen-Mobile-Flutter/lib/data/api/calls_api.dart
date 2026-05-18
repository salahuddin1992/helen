/// Calls REST surface — call initialization, ICE servers, history.
///
/// Real-time signalling (offer/answer/candidate/end) flows over Socket.IO,
/// not REST. See `socket_client.dart`.
library;

import '../../core/config/constants.dart';
import '../models/call.dart';
import 'api_client.dart';

class CallsApi {
  CallsApi(this._client);
  final ApiClient _client;

  /// Bootstrap a call: server allocates a call_id, returns initial routing
  /// decision (p2p / mesh / sfu) based on participant count + policy.
  Future<Call> initCall({
    required String channelId,
    required String callType, // 'audio' | 'video'
  }) async {
    return guardApi(() async {
      final Map<String, dynamic> data = await _client.post<Map<String, dynamic>>(
        K.pCallsInit,
        body: <String, dynamic>{
          'channel_id': channelId,
          'call_type': callType,
        },
      );
      return Call.fromJson(data);
    });
  }

  /// Short-lived TURN credentials for `RTCPeerConnection`. Cache client-side
  /// until `ttl_seconds` from issue; the desktop client refreshes lazily.
  Future<Map<String, dynamic>> getIceConfig() async {
    return guardApi(() =>
        _client.get<Map<String, dynamic>>(K.pIceConfig));
  }

  Future<List<Call>> getHistory() async {
    return guardApi(() async {
      final dynamic raw = await _client.get<dynamic>('/api/calls');
      final List<dynamic> arr =
          raw is List<dynamic> ? raw : (raw['calls'] as List<dynamic>? ?? <dynamic>[]);
      return arr
          .map((dynamic e) => Call.fromJson(e as Map<String, dynamic>))
          .toList();
    });
  }

  Future<Map<String, dynamic>?> getChannelActiveCall(String channelId) async {
    return guardApi(() async {
      final Map<String, dynamic> data = await _client
          .get<Map<String, dynamic>>('/api/channels/$channelId/active-call');
      return data['active_call'] as Map<String, dynamic>?;
    });
  }

  Future<void> deleteCall(String id) =>
      guardApi(() => _client.delete<void>('/api/calls/$id'));
}
