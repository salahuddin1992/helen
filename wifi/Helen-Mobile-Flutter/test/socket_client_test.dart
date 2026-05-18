import 'package:flutter_test/flutter_test.dart';

import 'package:helen_mobile/core/config/constants.dart';
import 'package:helen_mobile/data/socket/socket_events.dart';

void main() {
  group('SocketEvent', () {
    test('round-trips wire names', () {
      for (final SocketEvent e in SocketEvent.values) {
        if (e == SocketEvent.unknown) continue;
        expect(SocketEvent.fromName(e.wireName), e);
      }
    });

    test('unknown event maps to unknown', () {
      expect(SocketEvent.fromName('this-event-does-not-exist'),
          SocketEvent.unknown);
    });

    test('known wire names match constants', () {
      expect(SocketEvent.message.wireName, K.sEvtMessage);
      expect(SocketEvent.callInvite.wireName, K.sEvtCallInvite);
      expect(SocketEvent.iceCandidate.wireName, K.sEvtIceCandidate);
    });
  });
}
