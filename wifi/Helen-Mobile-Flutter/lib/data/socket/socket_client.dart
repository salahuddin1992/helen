/// Socket.IO wrapper.
///
/// Pattern matches `CommClient-Desktop/src/renderer/services/socket.manager.ts`:
///   - Bearer JWT in `auth` payload
///   - Auto-reconnect with backoff
///   - 401 → refresh tokens → reconnect
///   - Typed event stream consumers subscribe to
library;

import 'dart:async';

import 'package:rxdart/rxdart.dart';
import 'package:socket_io_client/socket_io_client.dart' as io;

import '../../core/config/constants.dart';
import '../../core/config/env.dart';
import '../../core/logger/app_logger.dart';
import '../api/api_client.dart';
import 'socket_events.dart';

enum SocketState { idle, connecting, connected, reconnecting, error, closed }

class SocketClient {
  SocketClient._();
  static final SocketClient I = SocketClient._();

  io.Socket? _socket;

  final BehaviorSubject<SocketState> _state =
      BehaviorSubject<SocketState>.seeded(SocketState.idle);
  Stream<SocketState> get state => _state.stream;
  SocketState get currentState => _state.value;

  final PublishSubject<SocketEnvelope> _events = PublishSubject<SocketEnvelope>();
  Stream<SocketEnvelope> get events => _events.stream;

  /// Stream a single event type.
  Stream<Map<String, dynamic>> on(SocketEvent e) => _events.stream
      .where((SocketEnvelope env) => env.event == e)
      .map((SocketEnvelope env) => env.payload);

  String? _accessToken;

  Future<void> connect({required String accessToken}) async {
    _accessToken = accessToken;

    // If a socket is open, swap auth and let it reconnect.
    if (_socket != null) {
      _socket!.auth = <String, String>{'token': accessToken};
      _socket!.disconnect();
      _socket!.connect();
      return;
    }

    final String url = Env.I.socketUrl;
    AppLogger.I.i('socket: connecting → $url');
    _state.add(SocketState.connecting);

    final io.Socket s = io.io(
      url,
      io.OptionBuilder()
          .setTransports(<String>['websocket'])
          .setAuth(<String, String>{'token': accessToken})
          .enableForceNew()
          .enableReconnection()
          .setReconnectionAttempts(0x7fffffff)
          .setReconnectionDelay(1000)
          .setReconnectionDelayMax(30000)
          .setRandomizationFactor(0.5)
          .setTimeout(15000)
          .build(),
    );
    _socket = s;

    s.onConnect((_) {
      AppLogger.I.i('socket: connected sid=${s.id}');
      _state.add(SocketState.connected);
    });
    s.onDisconnect((dynamic reason) {
      AppLogger.I.w('socket: disconnected ($reason)');
      _state.add(SocketState.reconnecting);
    });
    s.onConnectError((dynamic err) async {
      AppLogger.I.w('socket: connect-error $err');
      // 401 → refresh and retry. Matches desktop behavior.
      final String msg = err.toString().toLowerCase();
      if (msg.contains('401') ||
          msg.contains('unauthorized') ||
          msg.contains('invalid token')) {
        final bool ok = await ApiClient.I.refreshTokensIfPossible();
        if (ok) {
          _accessToken = ApiClient.I.accessToken;
          if (_accessToken != null) {
            s.auth = <String, String>{'token': _accessToken!};
          }
        }
      }
      _state.add(SocketState.reconnecting);
    });
    s.onError((dynamic err) {
      AppLogger.I.w('socket: error $err');
      _state.add(SocketState.error);
    });

    // Wire every known event into the unified stream.
    for (final SocketEvent ev in SocketEvent.values) {
      if (ev == SocketEvent.unknown) continue;
      s.on(ev.wireName, (dynamic data) {
        final Map<String, dynamic> payload =
            data is Map<String, dynamic> ? data : <String, dynamic>{'raw': data};
        _events.add(SocketEnvelope(event: ev, payload: payload));
      });
    }

    s.connect();
  }

  /// Emit a typed event. Returns immediately; server ack (if any) is
  /// delivered through the regular stream.
  void emit(SocketEvent e, Map<String, dynamic> payload) {
    final io.Socket? s = _socket;
    if (s == null || !s.connected) {
      AppLogger.I.w('socket: emit ${e.wireName} while not connected — dropped');
      return;
    }
    s.emit(e.wireName, payload);
  }

  /// Emit + ack with timeout.
  Future<Map<String, dynamic>?> emitWithAck(
    SocketEvent e,
    Map<String, dynamic> payload, {
    Duration timeout = const Duration(seconds: 8),
  }) async {
    final io.Socket? s = _socket;
    if (s == null || !s.connected) return null;
    final Completer<Map<String, dynamic>?> c = Completer<Map<String, dynamic>?>();
    s.emitWithAck(
      e.wireName,
      payload,
      ack: (dynamic data) {
        if (c.isCompleted) return;
        final Map<String, dynamic> p =
            data is Map<String, dynamic> ? data : <String, dynamic>{};
        c.complete(p);
      },
    );
    return c.future.timeout(timeout, onTimeout: () => null);
  }

  Future<void> disconnect() async {
    final io.Socket? s = _socket;
    if (s != null) {
      s.disconnect();
      s.dispose();
    }
    _socket = null;
    _state.add(SocketState.closed);
  }

  Future<void> dispose() async {
    await disconnect();
    await _state.close();
    await _events.close();
  }

  // ── Convenience helpers used by callers ───────────────────────────

  void joinChannel(String channelId) =>
      emit(SocketEvent.unknown, <String, dynamic>{'channel_id': channelId});

  void emitTyping(String channelId, {bool isTyping = true}) {
    final io.Socket? s = _socket;
    if (s == null || !s.connected) return;
    s.emit(K.sEvtTyping,
        <String, dynamic>{'channel_id': channelId, 'is_typing': isTyping});
  }
}
