/// Message model. Mirrors the server's `Message` resource.
library;

import 'package:freezed_annotation/freezed_annotation.dart';

part 'message.freezed.dart';
part 'message.g.dart';

enum MessageType {
  @JsonValue('text') text,
  @JsonValue('image') image,
  @JsonValue('file') file,
  @JsonValue('reply') reply,
  @JsonValue('system') system,
}

@freezed
class Message with _$Message {
  const factory Message({
    required String id,
    @JsonKey(name: 'channel_id') required String channelId,
    @JsonKey(name: 'sender_id') required String senderId,
    required String content,
    @Default(MessageType.text) MessageType type,
    @JsonKey(name: 'reply_to') String? replyTo,
    @JsonKey(name: 'file_id') String? fileId,
    @JsonKey(name: 'is_pinned') @Default(false) bool isPinned,
    @JsonKey(name: 'is_edited') @Default(false) bool isEdited,
    @JsonKey(name: 'is_deleted') @Default(false) bool isDeleted,
    @Default(<MessageReaction>[]) List<MessageReaction> reactions,
    @JsonKey(name: 'created_at') DateTime? createdAt,
    @JsonKey(name: 'edited_at') DateTime? editedAt,
  }) = _Message;

  factory Message.fromJson(Map<String, dynamic> json) =>
      _$MessageFromJson(json);
}

@freezed
class MessageReaction with _$MessageReaction {
  const factory MessageReaction({
    required String emoji,
    @Default(<String>[]) List<String> users,
    @Default(0) int count,
  }) = _MessageReaction;

  factory MessageReaction.fromJson(Map<String, dynamic> json) =>
      _$MessageReactionFromJson(json);
}
