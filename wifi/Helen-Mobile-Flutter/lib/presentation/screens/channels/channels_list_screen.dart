import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';
import 'package:intl/intl.dart';

import '../../../data/models/channel.dart';
import '../../../providers/channels_provider.dart';
import '../../../router/routes.dart';
import '../../widgets/empty_state.dart';
import '../../widgets/error_view.dart';
import '../../widgets/loading_indicator.dart';

class ChannelsListScreen extends ConsumerWidget {
  const ChannelsListScreen({super.key, this.embedded = false});
  final bool embedded;

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final AsyncValue<List<Channel>> data = ref.watch(channelsProvider);

    final Widget body = data.when(
      loading: () => const LoadingIndicator(label: 'Loading conversations…'),
      error: (Object e, _) =>
          ErrorView(error: e, onRetry: () => ref.invalidate(channelsProvider)),
      data: (List<Channel> channels) {
        if (channels.isEmpty) {
          return const EmptyState(
            icon: Icons.chat_outlined,
            title: 'No conversations yet',
            description: 'Start a new chat to see it here.',
          );
        }
        return RefreshIndicator(
          onRefresh: () async => ref.invalidate(channelsProvider),
          child: ListView.separated(
            padding: const EdgeInsets.symmetric(vertical: 8),
            itemCount: channels.length,
            separatorBuilder: (_, __) => const Divider(height: 1),
            itemBuilder: (BuildContext context, int i) {
              final Channel c = channels[i];
              return _ChannelTile(channel: c);
            },
          ),
        );
      },
    );

    if (embedded) return body;
    return Scaffold(
      appBar: AppBar(title: const Text('Chats')),
      body: body,
    );
  }
}

class _ChannelTile extends StatelessWidget {
  const _ChannelTile({required this.channel});
  final Channel channel;

  @override
  Widget build(BuildContext context) {
    final String label = channel.name ??
        (channel.type == 'direct' ? 'Direct chat' : 'Channel');
    final String subtitle = channel.description ?? '';
    final String? when = channel.lastMessageAt != null
        ? _fmt(channel.lastMessageAt!)
        : null;
    final int unread = channel.unreadCount;
    return ListTile(
      onTap: () => context.push(Routes.channelDetailFor(channel.id)),
      leading: CircleAvatar(
        backgroundColor: Theme.of(context).colorScheme.secondaryContainer,
        child: Text(
          label.isNotEmpty ? label.characters.first.toUpperCase() : '?',
        ),
      ),
      title: Text(label,
          maxLines: 1, overflow: TextOverflow.ellipsis,
          style: const TextStyle(fontWeight: FontWeight.w600)),
      subtitle: subtitle.isEmpty
          ? null
          : Text(subtitle, maxLines: 1, overflow: TextOverflow.ellipsis),
      trailing: Column(
        crossAxisAlignment: CrossAxisAlignment.end,
        mainAxisAlignment: MainAxisAlignment.center,
        children: <Widget>[
          if (when != null)
            Text(
              when,
              style: Theme.of(context).textTheme.bodySmall?.copyWith(
                    color: Theme.of(context).colorScheme.onSurfaceVariant,
                  ),
            ),
          if (unread > 0) ...<Widget>[
            const SizedBox(height: 4),
            Container(
              padding:
                  const EdgeInsets.symmetric(horizontal: 8, vertical: 2),
              decoration: BoxDecoration(
                color: Theme.of(context).colorScheme.primary,
                borderRadius: BorderRadius.circular(10),
              ),
              child: Text(
                unread > 99 ? '99+' : '$unread',
                style: TextStyle(
                  color: Theme.of(context).colorScheme.onPrimary,
                  fontSize: 11,
                  fontWeight: FontWeight.bold,
                ),
              ),
            ),
          ],
        ],
      ),
    );
  }

  String _fmt(DateTime dt) {
    final DateTime now = DateTime.now();
    if (now.difference(dt).inDays == 0) {
      return DateFormat.jm().format(dt);
    }
    if (now.difference(dt).inDays < 7) {
      return DateFormat.E().format(dt);
    }
    return DateFormat.yMd().format(dt);
  }
}
