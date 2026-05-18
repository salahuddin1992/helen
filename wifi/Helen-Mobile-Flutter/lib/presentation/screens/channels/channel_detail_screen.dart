import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import '../../../data/models/message.dart';
import '../../../providers/messages_provider.dart';
import '../../widgets/error_view.dart';
import '../../widgets/loading_indicator.dart';
import '../messages/message_bubble.dart';
import '../messages/message_composer.dart';

class ChannelDetailScreen extends ConsumerStatefulWidget {
  const ChannelDetailScreen({super.key, required this.channelId});
  final String channelId;

  @override
  ConsumerState<ChannelDetailScreen> createState() =>
      _ChannelDetailScreenState();
}

class _ChannelDetailScreenState extends ConsumerState<ChannelDetailScreen> {
  final ScrollController _scroll = ScrollController();
  final List<Message> _local = <Message>[];

  @override
  void dispose() {
    _scroll.dispose();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) {
    final AsyncValue<List<Message>> initial =
        ref.watch(channelMessagesProvider(widget.channelId));

    // Stream live messages and merge.
    ref.listen<AsyncValue<Message>>(liveMessagesProvider(widget.channelId),
        (AsyncValue<Message>? prev, AsyncValue<Message> next) {
      next.whenData((Message m) {
        setState(() {
          _local.insert(0, m);
        });
      });
    });

    return Scaffold(
      appBar: AppBar(
        title: const Text('Conversation'),
        actions: <Widget>[
          IconButton(
            icon: const Icon(Icons.call_outlined),
            onPressed: () {},
            tooltip: 'Voice call',
          ),
          IconButton(
            icon: const Icon(Icons.videocam_outlined),
            onPressed: () {},
            tooltip: 'Video call',
          ),
        ],
      ),
      body: Column(
        children: <Widget>[
          Expanded(
            child: initial.when(
              loading: () => const LoadingIndicator(),
              error: (Object e, _) => ErrorView(
                error: e,
                onRetry: () =>
                    ref.invalidate(channelMessagesProvider(widget.channelId)),
              ),
              data: (List<Message> remote) {
                final List<Message> merged = <Message>[..._local, ...remote];
                if (merged.isEmpty) {
                  return Center(
                    child: Text(
                      'No messages yet — say hi.',
                      style: Theme.of(context).textTheme.bodyMedium,
                    ),
                  );
                }
                return ListView.builder(
                  controller: _scroll,
                  reverse: true,
                  padding: const EdgeInsets.all(12),
                  itemCount: merged.length,
                  itemBuilder: (BuildContext c, int i) =>
                      MessageBubble(message: merged[i]),
                );
              },
            ),
          ),
          const Divider(height: 1),
          MessageComposer(channelId: widget.channelId),
        ],
      ),
    );
  }
}
