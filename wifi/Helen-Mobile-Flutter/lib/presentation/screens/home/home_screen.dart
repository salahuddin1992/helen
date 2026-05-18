import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../../../providers/auth_provider.dart';
import '../../../providers/socket_provider.dart';
import '../../../router/routes.dart';
import '../channels/channels_list_screen.dart';
import '../settings/settings_screen.dart';

class HomeScreen extends ConsumerStatefulWidget {
  const HomeScreen({super.key});

  @override
  ConsumerState<HomeScreen> createState() => _HomeScreenState();
}

class _HomeScreenState extends ConsumerState<HomeScreen> {
  int _index = 0;

  @override
  void initState() {
    super.initState();
    // Touch the socket provider to start connection in the background.
    Future<void>.microtask(() {
      ref.read(socketProvider);
    });
  }

  @override
  Widget build(BuildContext context) {
    final AuthState auth = ref.watch(authProvider);
    final String? name = auth is AuthAuthenticated ? auth.user.displayName : null;

    final List<Widget> tabs = const <Widget>[
      ChannelsListScreen(embedded: true),
      _ContactsTab(),
      SettingsScreen(embedded: true),
    ];

    return Scaffold(
      appBar: AppBar(
        title: Text(name != null ? 'Hi, $name' : 'Helen'),
        actions: <Widget>[
          IconButton(
            onPressed: () => context.push(Routes.pairScan),
            icon: const Icon(Icons.qr_code_scanner_rounded),
            tooltip: 'Pair device',
          ),
        ],
      ),
      body: IndexedStack(index: _index, children: tabs),
      bottomNavigationBar: NavigationBar(
        selectedIndex: _index,
        onDestinationSelected: (int i) => setState(() => _index = i),
        destinations: const <NavigationDestination>[
          NavigationDestination(
            icon: Icon(Icons.chat_bubble_outline_rounded),
            selectedIcon: Icon(Icons.chat_bubble_rounded),
            label: 'Chats',
          ),
          NavigationDestination(
            icon: Icon(Icons.people_outline_rounded),
            selectedIcon: Icon(Icons.people_rounded),
            label: 'Contacts',
          ),
          NavigationDestination(
            icon: Icon(Icons.settings_outlined),
            selectedIcon: Icon(Icons.settings_rounded),
            label: 'Settings',
          ),
        ],
      ),
    );
  }
}

class _ContactsTab extends StatelessWidget {
  const _ContactsTab();
  @override
  Widget build(BuildContext context) {
    return const Center(child: Text('Contacts (placeholder)'));
  }
}
