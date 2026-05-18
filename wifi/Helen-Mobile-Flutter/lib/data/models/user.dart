/// User domain model — freezed.
library;

import 'package:freezed_annotation/freezed_annotation.dart';

part 'user.freezed.dart';
part 'user.g.dart';

@freezed
class User with _$User {
  const factory User({
    required String id,
    required String username,
    @JsonKey(name: 'display_name') required String displayName,
    @JsonKey(name: 'avatar_url') String? avatarUrl,
    String? bio,
    @Default('offline') String status,
    @Default('user') String role,
    @JsonKey(name: 'share_code') String? shareCode,
    @JsonKey(name: 'status_message') String? statusMessage,
    @JsonKey(name: 'status_expires_at') DateTime? statusExpiresAt,
    @JsonKey(name: 'last_seen') DateTime? lastSeen,
    @JsonKey(name: 'created_at') DateTime? createdAt,
  }) = _User;

  factory User.fromJson(Map<String, dynamic> json) => _$UserFromJson(json);
}
