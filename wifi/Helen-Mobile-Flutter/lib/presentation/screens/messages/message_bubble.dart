import 'package:flutter/material.dart';
import 'package:intl/intl.dart';

import '../../../data/models/message.dart';

class MessageBubble extends StatelessWidget {
  const MessageBubble({super.key, required this.message, this.isOutgoing = false});
  final Message message;
  final bool isOutgoing;

  @override
  Widget build(BuildContext context) {
    final ColorScheme cs = Theme.of(context).colorScheme;
    final Color bg = isOutgoing ? cs.primary : cs.surfaceContainerHigh;
    final Color fg = isOutgoing ? cs.onPrimary : cs.onSurface;
    final Alignment align =
        isOutgoing ? Alignment.centerRight : Alignment.centerLeft;
    final BorderRadius radius = BorderRadius.only(
      topLeft: const Radius.circular(16),
      topRight: const Radius.circular(16),
      bottomLeft: Radius.circular(isOutgoing ? 16 : 4),
      bottomRight: Radius.circular(isOutgoing ? 4 : 16),
    );

    final DateTime? created = message.createdAt;

    return Align(
      alignment: align,
      child: ConstrainedBox(
        constraints: BoxConstraints(
          maxWidth: MediaQuery.of(context).size.width * 0.78,
        ),
        child: Container(
          margin: const EdgeInsets.symmetric(vertical: 4),
          padding: const EdgeInsets.symmetric(horizontal: 14, vertical: 10),
          decoration: BoxDecoration(color: bg, borderRadius: radius),
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            mainAxisSize: MainAxisSize.min,
            children: <Widget>[
              if (message.type == MessageType.file && message.fileId != null)
                Padding(
                  padding: const EdgeInsets.only(bottom: 6),
                  child: Row(
                    mainAxisSize: MainAxisSize.min,
                    children: <Widget>[
                      Icon(Icons.attach_file_rounded, size: 16, color: fg),
                      const SizedBox(width: 4),
                      Text(
                        'Attachment',
                        style: TextStyle(color: fg, fontWeight: FontWeight.w600),
                      ),
                    ],
                  ),
                ),
              SelectableText(
                message.content,
                style: TextStyle(color: fg, fontSize: 15, height: 1.3),
              ),
              if (message.isEdited || created != null) ...<Widget>[
                const SizedBox(height: 4),
                Row(
                  mainAxisSize: MainAxisSize.min,
                  children: <Widget>[
                    if (message.isEdited)
                      Padding(
                        padding: const EdgeInsets.only(right: 6),
                        child: Text(
                          'edited',
                          style: TextStyle(
                            color: fg.withValues(alpha: 0.7),
                            fontSize: 11,
                          ),
                        ),
                      ),
                    if (created != null)
                      Text(
                        DateFormat.jm().format(created),
                        style: TextStyle(
                          color: fg.withValues(alpha: 0.7),
                          fontSize: 11,
                        ),
                      ),
                  ],
                ),
              ],
            ],
          ),
        ),
      ),
    );
  }
}
