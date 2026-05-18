/// Strongly-typed socket event surface.
///
/// Server's Socket.IO emits the events listed below. We funnel everything
/// through [SocketEvent] enum + [SocketEnvelope] so handlers never use
/// stringly-typed event names.
library;

import '../../core/config/constants.dart';

enum SocketEvent {
  message,
  messageEdit,
  messageDelete,
  reaction,
  typing,
  presence,
  callInvite,
  callAccept,
  callReject,
  callEnd,
  iceCandidate,
  sdpOffer,
  sdpAnswer,
  unknown;

  static SocketEvent fromName(String name) {
    switch (name) {
      case K.sEvtMessage:
        return SocketEvent.message;
      case K.sEvtMessageEdit:
        return SocketEvent.messageEdit;
      case K.sEvtMessageDelete:
        return SocketEvent.messageDelete;
      case K.sEvtReaction:
        return SocketEvent.reaction;
      case K.sEvtTyping:
        return SocketEvent.typing;
      case K.sEvtPresence:
        return SocketEvent.presence;
      case K.sEvtCallInvite:
        return SocketEvent.callInvite;
      case K.sEvtCallAccept:
        return SocketEvent.callAccept;
      case K.sEvtCallReject:
        return SocketEvent.callReject;
      case K.sEvtCallEnd:
        return SocketEvent.callEnd;
      case K.sEvtIceCandidate:
        return SocketEvent.iceCandidate;
      case K.sEvtSdpOffer:
        return SocketEvent.sdpOffer;
      case K.sEvtSdpAnswer:
        return SocketEvent.sdpAnswer;
      default:
        return SocketEvent.unknown;
    }
  }

  String get wireName {
    switch (this) {
      case SocketEvent.message:
        return K.sEvtMessage;
      case SocketEvent.messageEdit:
        return K.sEvtMessageEdit;
      case SocketEvent.messageDelete:
        return K.sEvtMessageDelete;
      case SocketEvent.reaction:
        return K.sEvtReaction;
      case SocketEvent.typing:
        return K.sEvtTyping;
      case SocketEvent.presence:
        return K.sEvtPresence;
      case SocketEvent.callInvite:
        return K.sEvtCallInvite;
      case SocketEvent.callAccept:
        return K.sEvtCallAccept;
      case SocketEvent.callReject:
        return K.sEvtCallReject;
      case SocketEvent.callEnd:
        return K.sEvtCallEnd;
      case SocketEvent.iceCandidate:
        return K.sEvtIceCandidate;
      case SocketEvent.sdpOffer:
        return K.sEvtSdpOffer;
      case SocketEvent.sdpAnswer:
        return K.sEvtSdpAnswer;
      case SocketEvent.unknown:
        return 'unknown';
    }
  }
}

class SocketEnvelope {
  const SocketEnvelope({required this.event, required this.payload});
  final SocketEvent event;
  final Map<String, dynamic> payload;
}
