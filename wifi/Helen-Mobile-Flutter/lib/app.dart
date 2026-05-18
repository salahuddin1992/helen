/// Root [MaterialApp.router] widget.
///
/// Configures theming (dynamic Material 3 color, dark/light), localizations
/// (Arabic + English), and the [go_router] instance from [appRouterProvider].
library;

import 'package:dynamic_color/dynamic_color.dart';
import 'package:flutter/material.dart';
import 'package:flutter_localizations/flutter_localizations.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';

import 'l10n/generated/app_localizations.dart';
import 'presentation/theme/app_theme.dart';
import 'providers/auth_provider.dart';
import 'router/app_router.dart';

class HelenApp extends ConsumerWidget {
  const HelenApp({super.key});

  @override
  Widget build(BuildContext context, WidgetRef ref) {
    final GoRouter router = ref.watch(appRouterProvider);
    final Locale? locale = ref.watch(localePrefProvider);

    return DynamicColorBuilder(
      builder: (ColorScheme? lightDynamic, ColorScheme? darkDynamic) {
        return MaterialApp.router(
          title: 'Helen',
          debugShowCheckedModeBanner: false,
          routerConfig: router,
          theme: AppTheme.light(lightDynamic),
          darkTheme: AppTheme.dark(darkDynamic),
          themeMode: ThemeMode.system,
          locale: locale,
          supportedLocales: AppLocalizations.supportedLocales,
          localizationsDelegates: AppLocalizations.localizationsDelegates,
          builder: (BuildContext context, Widget? child) {
            return MediaQuery(
              data: MediaQuery.of(context).copyWith(
                textScaler: MediaQuery.textScalerOf(context).clamp(
                  minScaleFactor: 0.85,
                  maxScaleFactor: 1.3,
                ),
              ),
              child: child ?? const SizedBox.shrink(),
            );
          },
        );
      },
    );
  }
}

/// Locale preference — null = follow system. Persisted via SharedPreferences
/// in production; for now this is in-memory so the app responds to changes.
final StateProvider<Locale?> localePrefProvider =
    StateProvider<Locale?>((Ref ref) => null);
