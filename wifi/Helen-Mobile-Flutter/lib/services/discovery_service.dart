/// mDNS LAN discovery for Helen servers.
///
/// Probes for `_helen._tcp.local` and `_commclient._tcp.local`, emits a
/// stream of resolved peers ([host], [port], [txt]) so the UI can populate
/// the "discovered servers" list.
library;

import 'dart:async';
import 'dart:io';

import 'package:multicast_dns/multicast_dns.dart';

import '../core/logger/app_logger.dart';

class DiscoveredPeer {
  const DiscoveredPeer({
    required this.host,
    required this.port,
    required this.name,
    required this.txt,
  });
  final String host;
  final int port;
  final String name;
  final Map<String, String> txt;
}

class DiscoveryService {
  DiscoveryService._();
  static final DiscoveryService I = DiscoveryService._();

  static const List<String> _services = <String>[
    '_helen._tcp.local',
    '_commclient._tcp.local',
  ];

  Future<List<DiscoveredPeer>> discover({
    Duration timeout = const Duration(seconds: 4),
  }) async {
    final List<DiscoveredPeer> peers = <DiscoveredPeer>[];
    final MDnsClient client = MDnsClient(
      rawDatagramSocketFactory: (
        dynamic host,
        int port, {
        bool reuseAddress = true,
        bool reusePort = true,
        int ttl = 255,
      }) =>
          RawDatagramSocket.bind(host, port,
              reuseAddress: reuseAddress, reusePort: false, ttl: ttl),
    );

    try {
      await client.start();
      for (final String svc in _services) {
        await for (final PtrResourceRecord ptr in client
            .lookup<PtrResourceRecord>(
              ResourceRecordQuery.serverPointer(svc),
            )
            .timeout(timeout, onTimeout: (EventSink<PtrResourceRecord> s) => s.close())) {
          await for (final SrvResourceRecord srv in client
              .lookup<SrvResourceRecord>(
                ResourceRecordQuery.service(ptr.domainName),
              )
              .timeout(timeout, onTimeout: (EventSink<SrvResourceRecord> s) => s.close())) {
            final List<TxtResourceRecord> txtRecs = await client
                .lookup<TxtResourceRecord>(
                  ResourceRecordQuery.text(ptr.domainName),
                )
                .toList();
            final Map<String, String> txt = <String, String>{};
            for (final TxtResourceRecord t in txtRecs) {
              for (final String pair in t.text.split('\n')) {
                final int i = pair.indexOf('=');
                if (i > 0) {
                  txt[pair.substring(0, i)] = pair.substring(i + 1);
                }
              }
            }
            peers.add(DiscoveredPeer(
              host: srv.target,
              port: srv.port,
              name: ptr.domainName,
              txt: txt,
            ));
          }
        }
      }
    } on Object catch (e, st) {
      AppLogger.I.w('mdns discovery error', e, st);
    } finally {
      client.stop();
    }

    return peers;
  }
}
