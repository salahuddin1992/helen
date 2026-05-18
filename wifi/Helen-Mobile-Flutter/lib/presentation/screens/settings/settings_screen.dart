import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../providers/auth_provider.dart';
import '../../../router/routes.dart';

class SettingsScreen extends ConsumerWidget {
  const SettingsScreen({super.key, this.embedded = false});
  final bool embedded;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final AuthState auth = ref.watch(authProvider);
    final String name = auth is AuthAuthenticated ? auth.user.displayName : '—';
    final String username =
        auth is AuthAuthenticated ? '@${auth.user.username}' : '';

    final Widget body = ListView(
      padding: const EdgeInsets.symmetric(vertical: 8),
      children: <Widget>[
        ListTile(
          leading: const CircleAvatar(child: Icon(Icons.person)),
          title: Text(name),
          subtitle: Text(username),
        ),
        const Divider(),
        ListTile(
          leading: const Icon(Icons.dns_outlined),
          title: const Text('Server'),
          subtitle: const Text('Change the server URL'),
          onTap: () => context.push(Routes.serverSettings),
        ),
        ListTile(
          leading: const Icon(Icons.notifications_outlined),
          title: const Text('Notifications'),
        ),
        ListTile(
          leading: const Icon(Icons.fingerprint_rounded),
          title: const Text('Biometric unlock'),
        ),
        ListTile(
          leading: const Icon(Icons.language_outlined),
          title: const Text('Language'),
        ),
        const Divider(),
        ListTile(
          leading:
              Icon(Icons.logout_rounded, color: Theme.of(context).colorScheme.error),
          title: Text(
            'Sign out',
            style: TextStyle(color: Theme.of(context).colorScheme.error),
          ),
          onTap: () async {
            await ref.read(authProvider.notifier).logout();
            if (context.mounted) context.go(Routes.login);
          },
        ),
      ],
    );

    if (embedded) return body;
    return Scaffold(
      appBar: AppBar(title: const Text('Settings')),
      body: body,
    );
  }
}
