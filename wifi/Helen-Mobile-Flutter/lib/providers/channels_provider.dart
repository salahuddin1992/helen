/// Channels list provider.
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../data/api/messages_api.dart';
import '../data/models/channel.dart';
import '../domain/repositories/messages_repository.dart';
import 'auth_provider.dart';

final Provider<MessagesApi> messagesApiProvider = Provider<MessagesApi>(
  (Ref ref) => MessagesApi(ref.watch(apiClientProvider)),
);

final Provider<MessagesRepository> messagesRepositoryProvider =
    Provider<MessagesRepository>(
  (Ref ref) => MessagesRepository(ref.watch(messagesApiProvider)),
);

final FutureProvider<List<Channel>> channelsProvider =
    FutureProvider<List<Channel>>(
  (Ref ref) async {
    // Re-fetch on auth change.
    ref.watch(authProvider);
    final MessagesRepository repo = ref.watch(messagesRepositoryProvider);
    return repo.listChannels();
  },
);
