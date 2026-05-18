/// Calls providers — REST surface + active-call state machine.
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../data/api/calls_api.dart';
import '../data/models/call.dart';
import '../domain/repositories/calls_repository.dart';
import 'auth_provider.dart';

final Provider<CallsApi> callsApiProvider = Provider<CallsApi>(
  (Ref ref) => CallsApi(ref.watch(apiClientProvider)),
);

final Provider<CallsRepository> callsRepositoryProvider =
    Provider<CallsRepository>(
  (Ref ref) => CallsRepository(ref.watch(callsApiProvider)),
);

/// State of the local user's active call (if any). Null = idle.
final StateProvider<Call?> activeCallProvider =
    StateProvider<Call?>((Ref ref) => null);

final FutureProvider<List<Call>> callHistoryProvider =
    FutureProvider<List<Call>>((Ref ref) async {
  ref.watch(authProvider);
  return ref.watch(callsRepositoryProvider).history();
});
