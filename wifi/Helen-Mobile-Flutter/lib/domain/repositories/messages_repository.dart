/// Messages repository — wraps remote API + offline cache + outbox.
library;

import '../../data/api/messages_api.dart';
import '../../data/models/channel.dart';
import '../../data/models/message.dart';

class MessagesRepository {
  MessagesRepository(this._api);
  final MessagesApi _api;

  Future<List<Channel>> listChannels() => _api.listChannels();
  Future<Channel> getChannel(String id) => _api.getChannel(id);

  Future<List<Message>> getMessages(String channelId,
          {String? before, int limit = 50}) =>
      _api.getMessages(channelId, before: before, limit: limit);

  Future<Message> sendText(String channelId, String content,
          {String? replyTo}) =>
      _api.sendMessage(channelId, content: content, replyTo: replyTo);

  Future<Message> sendFile(String channelId,
          {required String fileId, String caption = ''}) =>
      _api.sendMessage(channelId,
          content: caption, type: 'file', fileId: fileId);

  Future<Message> edit(String messageId, String content) =>
      _api.editMessage(messageId, content);

  Future<void> remove(String messageId) => _api.deleteMessage(messageId);

  Future<void> react(String messageId, String emoji) =>
      _api.toggleReaction(messageId, emoji);
}
