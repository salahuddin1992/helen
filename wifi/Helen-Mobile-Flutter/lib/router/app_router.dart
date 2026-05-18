/// go_router config + auth guard + deep link handler.
library;

import 'package:flutter/material.dart';
import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:go_router/go_router.dart';

import '../presentation/screens/auth/login_screen.dart';
import '../presentation/screens/auth/oauth_callback_screen.dart';
import '../presentation/screens/auth/register_screen.dart';
import '../presentation/screens/calls/active_call_screen.dart';
import '../presentation/screens/calls/incoming_call_screen.dart';
import '../presentation/screens/channels/channel_detail_screen.dart';
import '../presentation/screens/channels/channels_list_screen.dart';
import '../presentation/screens/home/home_screen.dart';
import '../presentation/screens/pairing/enter_code_screen.dart';
import '../presentation/screens/pairing/scan_qr_screen.dart';
import '../presentation/screens/settings/server_settings_screen.dart';
import '../presentation/screens/settings/settings_screen.dart';
import '../presentation/screens/splash_screen.dart';
import '../providers/auth_provider.dart';
import 'routes.dart';

final Provider<GoRouter> appRouterProvider = Provider<GoRouter>((Ref ref) {
  final AuthState auth = ref.watch(authProvider);
  return GoRouter(
    initialLocation: Routes.splash,
    debugLogDiagnostics: false,
    redirect: (BuildContext ctx, GoRouterState state) {
      final bool loading =
          auth is AuthInitial || auth is AuthLoading;
      final bool authed = auth is AuthAuthenticated;

      final String loc = state.matchedLocation;
      final bool isAuthRoute = loc == Routes.login ||
          loc == Routes.register ||
          loc.startsWith('/oauth/') ||
          loc.startsWith('/pair/');
      final bool isSplash = loc == Routes.splash;

      if (loading && !isSplash) return Routes.splash;
      if (!loading && isSplash) {
        return authed ? Routes.home : Routes.login;
      }
      if (!authed && !isAuthRoute && !isSplash) return Routes.login;
      if (authed && isAuthRoute) return Routes.home;
      return null;
    },
    routes: <RouteBase>[
      GoRoute(
        path: Routes.splash,
        builder: (_, __) => const SplashScreen(),
      ),
      GoRoute(
        path: Routes.login,
        builder: (_, __) => const LoginScreen(),
      ),
      GoRoute(
        path: Routes.register,
        builder: (_, __) => const RegisterScreen(),
      ),
      GoRoute(
        path: Routes.oauthCallback,
        builder: (BuildContext ctx, GoRouterState st) =>
            OauthCallbackScreen(uri: st.uri),
      ),
      GoRoute(
        path: Routes.pairScan,
        builder: (_, __) => const ScanQrScreen(),
      ),
      GoRoute(
        path: Routes.pairCode,
        builder: (_, __) => const EnterCodeScreen(),
      ),
      ShellRoute(
        builder: (BuildContext ctx, GoRouterState st, Widget child) => child,
        routes: <RouteBase>[
          GoRoute(
            path: Routes.home,
            builder: (_, __) => const HomeScreen(),
            routes: <RouteBase>[
              GoRoute(
                path: 'channels',
                builder: (_, __) => const ChannelsListScreen(),
                routes: <RouteBase>[
                  GoRoute(
                    path: ':id',
                    builder: (BuildContext c, GoRouterState s) =>
                        ChannelDetailScreen(channelId: s.pathParameters['id']!),
                  ),
                ],
              ),
              GoRoute(
                path: 'settings',
                builder: (_, __) => const SettingsScreen(),
                routes: <RouteBase>[
                  GoRoute(
                    path: 'server',
                    builder: (_, __) => const ServerSettingsScreen(),
                  ),
                ],
              ),
            ],
          ),
        ],
      ),
      GoRoute(
        path: Routes.activeCall,
        builder: (BuildContext c, GoRouterState s) =>
            ActiveCallScreen(callId: s.pathParameters['callId']!),
      ),
      GoRoute(
        path: Routes.incomingCall,
        builder: (BuildContext c, GoRouterState s) =>
            IncomingCallScreen(callId: s.pathParameters['callId']!),
      ),
    ],
    errorBuilder: (BuildContext c, GoRouterState s) => Scaffold(
      body: Center(
        child: Padding(
          padding: const EdgeInsets.all(24),
          child: Text(
            'Navigation error: ${s.error}',
            textAlign: TextAlign.center,
            style: Theme.of(c).textTheme.titleMedium,
          ),
        ),
      ),
    ),
  );
});
