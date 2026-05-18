/// Send message use case — handles optimistic UI + outbox enqueue.
library;

import '../../data/models/message.dart';
import '../repositories/messages_repository.dart';

class SendMessageUseCase {
  SendMessageUseCase(this._repo);
  final MessagesRepository _repo;

  Future<Message> call({
    required String channelId,
    required String content,
    String? replyTo,
  }) async {
    final String trimmed = content.trim();
    if (trimmed.isEmpty) {
      throw ArgumentError.value(content, 'content', 'must not be empty');
    }
    return _repo.sendText(channelId, trimmed, replyTo: replyTo);
  }
}
