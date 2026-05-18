import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../core/config/env.dart';
import '../../../core/storage/secure_storage.dart';
import '../../../data/api/api_client.dart';
import '../../../services/discovery_service.dart';

class ServerSettingsScreen extends ConsumerStatefulWidget {
  const ServerSettingsScreen({super.key});

  @override
  ConsumerState<ServerSettingsScreen> createState() =>
      _ServerSettingsScreenState();
}

class _ServerSettingsScreenState extends ConsumerState<ServerSettingsScreen> {
  final TextEditingController _url =
      TextEditingController(text: Env.I.apiBaseUrl);
  bool _busy = false;
  bool _scanning = false;
  List<DiscoveredPeer> _peers = <DiscoveredPeer>[];

  @override
  void dispose() {
    _url.dispose();
    super.dispose();
  }

  Future<void> _save() async {
    setState(() => _busy = true);
    final String v = _url.text.trim();
    await SecureStorage.setServerUrl(v);
    Env.overrideServer(api: v);
    ApiClient.I.configure(baseUrl: v);
    if (mounted) {
      ScaffoldMessenger.of(context)
          .showSnackBar(const SnackBar(content: Text('Server saved')));
      setState(() => _busy = false);
      context.pop();
    }
  }

  Future<void> _scan() async {
    setState(() {
      _scanning = true;
      _peers = <DiscoveredPeer>[];
    });
    try {
      final List<DiscoveredPeer> p = await DiscoveryService.I.discover();
      if (mounted) setState(() => _peers = p);
    } finally {
      if (mounted) setState(() => _scanning = false);
    }
  }

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(title: const Text('Server settings')),
      body: SafeArea(
        child: Padding(
          padding: const EdgeInsets.all(16),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.stretch,
            children: <Widget>[
              TextField(
                controller: _url,
                decoration: const InputDecoration(
                  labelText: 'Server URL',
                  hintText: 'http://192.168.1.10:3000',
                ),
                keyboardType: TextInputType.url,
              ),
              const SizedBox(height: 16),
              FilledButton(
                onPressed: _busy ? null : _save,
                child: _busy
                    ? const SizedBox(
                        width: 20,
                        height: 20,
                        child: CircularProgressIndicator(strokeWidth: 2.5),
                      )
                    : const Text('Save'),
              ),
              const SizedBox(height: 24),
              const Divider(),
              const SizedBox(height: 12),
              Row(
                children: <Widget>[
                  Expanded(
                    child: Text(
                      'Discover on LAN',
                      style: Theme.of(context).textTheme.titleMedium,
                    ),
                  ),
                  IconButton(
                    onPressed: _scanning ? null : _scan,
                    icon: _scanning
                        ? const SizedBox(
                            width: 18,
                            height: 18,
                            child: CircularProgressIndicator(strokeWidth: 2.5),
                          )
                        : const Icon(Icons.search_rounded),
                  ),
                ],
              ),
              if (_peers.isEmpty && !_scanning)
                Padding(
                  padding: const EdgeInsets.all(16),
                  child: Text(
                    'No servers discovered yet — tap search.',
                    style: Theme.of(context).textTheme.bodyMedium,
                  ),
                ),
              Expanded(
                child: ListView.builder(
                  itemCount: _peers.length,
                  itemBuilder: (BuildContext c, int i) {
                    final DiscoveredPeer p = _peers[i];
                    final String url = 'http://${p.host}:${p.port}';
                    return ListTile(
                      leading: const Icon(Icons.lan_outlined),
                      title: Text(p.txt['name'] ?? p.name),
                      subtitle: Text(url),
                      onTap: () => setState(() => _url.text = url),
                    );
                  },
                ),
              ),
            ],
          ),
        ),
      ),
    );
  }
}
