/// Channel / conversation model.
library;

import 'package:freezed_annotation/freezed_annotation.dart';

part 'channel.freezed.dart';
part 'channel.g.dart';

@freezed
class Channel with _$Channel {
  const factory Channel({
    required String id,
    required String type, // direct | group | public | system
    String? name,
    String? description,
    @JsonKey(name: 'owner_id') String? ownerId,
    @JsonKey(name: 'last_message_id') String? lastMessageId,
    @JsonKey(name: 'last_message_at') DateTime? lastMessageAt,
    @JsonKey(name: 'unread_count') @Default(0) int unreadCount,
    @JsonKey(name: 'member_count') @Default(0) int memberCount,
    @Default(<ChannelMember>[]) List<ChannelMember> members,
    @JsonKey(name: 'is_pinned') @Default(false) bool isPinned,
    @JsonKey(name: 'is_archived') @Default(false) bool isArchived,
    @JsonKey(name: 'slow_mode_seconds') @Default(0) int slowModeSeconds,
    @JsonKey(name: 'ttl_seconds') @Default(0) int ttlSeconds,
    @JsonKey(name: 'created_at') DateTime? createdAt,
    @JsonKey(name: 'updated_at') DateTime? updatedAt,
  }) = _Channel;

  factory Channel.fromJson(Map<String, dynamic> json) => _$ChannelFromJson(json);
}

@freezed
class ChannelMember with _$ChannelMember {
  const factory ChannelMember({
    @JsonKey(name: 'user_id') required String userId,
    @Default('member') String role,
    @JsonKey(name: 'joined_at') DateTime? joinedAt,
    @JsonKey(name: 'last_read_at') DateTime? lastReadAt,
  }) = _ChannelMember;

  factory ChannelMember.fromJson(Map<String, dynamic> json) =>
      _$ChannelMemberFromJson(json);
}
