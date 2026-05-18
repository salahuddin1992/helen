/// Calls repository — wraps REST init + delegates signalling to socket.
library;

import '../../data/api/calls_api.dart';
import '../../data/models/call.dart';

class CallsRepository {
  CallsRepository(this._api);
  final CallsApi _api;

  Future<Call> initiate({required String channelId, required CallType type}) =>
      _api.initCall(
        channelId: channelId,
        callType: type == CallType.video ? 'video' : 'audio',
      );

  Future<Map<String, dynamic>> ice() => _api.getIceConfig();
  Future<List<Call>> history() => _api.getHistory();
  Future<Map<String, dynamic>?> activeCallFor(String channelId) =>
      _api.getChannelActiveCall(channelId);
}
