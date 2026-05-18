/// Messages provider per channel — combines REST history with live socket inserts.
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../data/models/message.dart';
import 'channels_provider.dart';
import 'socket_provider.dart';
import '../data/socket/socket_events.dart';

final AutoDisposeFutureProviderFamily<List<Message>, String> channelMessagesProvider =
    FutureProvider.autoDispose.family<List<Message>, String>(
  (AutoDisposeFutureProviderRef<List<Message>> ref, String channelId) async {
    final List<Message> initial =
        await ref.watch(messagesRepositoryProvider).getMessages(channelId);
    return initial;
  },
);

/// Live stream of inbound messages for a channel — fed by socket events.
final AutoDisposeStreamProviderFamily<Message, String> liveMessagesProvider =
    StreamProvider.autoDispose.family<Message, String>(
  (AutoDisposeStreamProviderRef<Message> ref, String channelId) {
    final StreamController<Message> ctrl = StreamController<Message>();
    final StreamSubscription<Map<String, dynamic>> sub = ref
        .watch(socketProvider)
        .on(SocketEvent.message)
        .where((Map<String, dynamic> p) => p['channel_id'] == channelId)
        .listen((Map<String, dynamic> p) {
      try {
        ctrl.add(Message.fromJson(p));
      } on Object {
        // ignore malformed
      }
    });
    ref.onDispose(() {
      sub.cancel();
      ctrl.close();
    });
    return ctrl.stream;
  },
);
