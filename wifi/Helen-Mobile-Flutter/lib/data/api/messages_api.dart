/// Channels + messages REST endpoints.
library;

import '../../core/config/constants.dart';
import '../models/channel.dart';
import '../models/message.dart';
import 'api_client.dart';

class MessagesApi {
  MessagesApi(this._client);
  final ApiClient _client;

  // ── Channels ──────────────────────────────────────────────────────

  Future<List<Channel>> listChannels() async {
    return guardApi(() async {
      final dynamic raw = await _client.get<dynamic>(K.pChannels);
      final List<dynamic> arr =
          raw is List<dynamic> ? raw : (raw['channels'] as List<dynamic>? ?? <dynamic>[]);
      return arr
          .map((dynamic e) => Channel.fromJson(e as Map<String, dynamic>))
          .toList();
    });
  }

  Future<Channel> getChannel(String id) async {
    return guardApi(() async {
      final Map<String, dynamic> data =
          await _client.get<Map<String, dynamic>>('${K.pChannels}/$id');
      return Channel.fromJson(data);
    });
  }

  Future<Channel> createChannel({
    required String type,
    String? name,
    String? description,
    required List<String> memberIds,
  }) async {
    return guardApi(() async {
      final Map<String, dynamic> data = await _client.post<Map<String, dynamic>>(
        K.pChannels,
        body: <String, dynamic>{
          'type': type,
          if (name != null) 'name': name,
          if (description != null) 'description': description,
          'member_ids': memberIds,
        },
      );
      return Channel.fromJson(data);
    });
  }

  Future<void> deleteChannel(String id) =>
      guardApi(() => _client.delete<void>('${K.pChannels}/$id'));

  Future<Channel> updateChannel(String id, Map<String, dynamic> body) async {
    return guardApi(() async {
      final Map<String, dynamic> data =
          await _client.patch<Map<String, dynamic>>('${K.pChannels}/$id',
              body: body);
      return Channel.fromJson(data);
    });
  }

  // ── Messages ──────────────────────────────────────────────────────

  Future<List<Message>> getMessages(
    String channelId, {
    String? before,
    int limit = K.defaultMessagePageSize,
  }) async {
    return guardApi(() async {
      final dynamic raw = await _client.get<dynamic>(
        '${K.pChannels}/$channelId/messages',
        query: <String, dynamic>{
          if (before != null) 'before': before,
          'limit': limit,
        },
      );
      final List<dynamic> arr =
          raw is List<dynamic> ? raw : (raw['messages'] as List<dynamic>? ?? <dynamic>[]);
      return arr
          .map((dynamic e) => Message.fromJson(e as Map<String, dynamic>))
          .toList();
    });
  }

  Future<Message> sendMessage(
    String channelId, {
    required String content,
    String type = 'text',
    String? replyTo,
    String? fileId,
  }) async {
    return guardApi(() async {
      final Map<String, dynamic> data = await _client.post<Map<String, dynamic>>(
        '${K.pChannels}/$channelId/messages',
        body: <String, dynamic>{
          'content': content,
          'type': type,
          if (replyTo != null) 'reply_to': replyTo,
          if (fileId != null) 'file_id': fileId,
        },
      );
      return Message.fromJson(data);
    });
  }

  Future<Message> editMessage(String messageId, String content) async {
    return guardApi(() async {
      final Map<String, dynamic> data = await _client.patch<Map<String, dynamic>>(
        '/api/messages/$messageId',
        body: <String, String>{'content': content},
      );
      return Message.fromJson(data);
    });
  }

  Future<void> deleteMessage(String messageId) =>
      guardApi(() => _client.delete<void>('/api/messages/$messageId'));

  Future<void> toggleReaction(String messageId, String emoji) async {
    return guardApi(() async {
      await _client.post<dynamic>(
        '/api/messages/$messageId/reactions',
        body: <String, String>{'emoji': emoji},
      );
    });
  }

  Future<Map<String, dynamic>> searchMessages(String q, {String? channelId}) async {
    return guardApi(() async {
      return _client.get<Map<String, dynamic>>(
        K.pMessagesSearch,
        query: <String, dynamic>{
          'q': q,
          if (channelId != null) 'channel_id': channelId,
        },
      );
    });
  }
}
