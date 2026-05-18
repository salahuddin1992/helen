/// Call model — matches `app/api/routes/calls.py`.
library;

import 'package:freezed_annotation/freezed_annotation.dart';

part 'call.freezed.dart';
part 'call.g.dart';

enum CallRouting {
  @JsonValue('p2p') p2p,
  @JsonValue('mesh') mesh,
  @JsonValue('sfu') sfu,
  @JsonValue('hybrid') hybrid,
}

enum CallType {
  @JsonValue('audio') audio,
  @JsonValue('video') video,
}

enum CallStatus {
  @JsonValue('ringing') ringing,
  @JsonValue('active') active,
  @JsonValue('ended') ended,
  @JsonValue('missed') missed,
  @JsonValue('declined') declined,
}

@freezed
class Call with _$Call {
  const factory Call({
    @JsonKey(name: 'call_id') required String callId,
    @JsonKey(name: 'channel_id') required String channelId,
    @JsonKey(name: 'call_type') @Default(CallType.audio) CallType callType,
    @Default(CallRouting.p2p) CallRouting routing,
    @Default(CallStatus.ringing) CallStatus status,
    @JsonKey(name: 'host_id') String? hostId,
    @JsonKey(name: 'started_at') DateTime? startedAt,
    @JsonKey(name: 'ended_at') DateTime? endedAt,
    @JsonKey(name: 'participant_count') @Default(0) int participantCount,
    @Default(<CallParticipant>[]) List<CallParticipant> participants,
    @JsonKey(name: 'ice_servers') List<Map<String, dynamic>>? iceServers,
  }) = _Call;

  factory Call.fromJson(Map<String, dynamic> json) => _$CallFromJson(json);
}

@freezed
class CallParticipant with _$CallParticipant {
  const factory CallParticipant({
    @JsonKey(name: 'user_id') required String userId,
    @Default(false) bool muted,
    @JsonKey(name: 'video_off') @Default(false) bool videoOff,
    @JsonKey(name: 'sharing_screen') @Default(false) bool sharingScreen,
    @JsonKey(name: 'on_hold') @Default(false) bool onHold,
  }) = _CallParticipant;

  factory CallParticipant.fromJson(Map<String, dynamic> json) =>
      _$CallParticipantFromJson(json);
}
