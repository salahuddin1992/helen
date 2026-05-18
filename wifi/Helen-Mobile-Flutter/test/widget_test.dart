import 'package:flutter/material.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:helen_mobile/presentation/widgets/empty_state.dart';
import 'package:helen_mobile/presentation/widgets/loading_indicator.dart';

void main() {
  testWidgets('LoadingIndicator shows progress + label',
      (WidgetTester tester) async {
    await tester.pumpWidget(const MaterialApp(
      home: Scaffold(body: LoadingIndicator(label: 'Loading')),
    ));
    expect(find.byType(CircularProgressIndicator), findsOneWidget);
    expect(find.text('Loading'), findsOneWidget);
  });

  testWidgets('EmptyState renders icon + title',
      (WidgetTester tester) async {
    await tester.pumpWidget(const MaterialApp(
      home: Scaffold(
        body: EmptyState(title: 'Nothing here', icon: Icons.inbox_rounded),
      ),
    ));
    expect(find.byIcon(Icons.inbox_rounded), findsOneWidget);
    expect(find.text('Nothing here'), findsOneWidget);
  });
}
