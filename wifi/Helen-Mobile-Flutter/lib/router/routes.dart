/// Typed route names + path templates.
library;

class Routes {
  Routes._();

  static const String splash = '/';
  static const String login = '/login';
  static const String register = '/register';
  static const String oauthCallback = '/oauth/callback';
  static const String pairScan = '/pair/scan';
  static const String pairCode = '/pair/code';
  static const String home = '/home';
  static const String channels = '/channels';
  static const String channelDetail = '/channels/:id';
  static const String settings = '/settings';
  static const String serverSettings = '/settings/server';
  static const String activeCall = '/calls/active/:callId';
  static const String incomingCall = '/calls/incoming/:callId';

  static String channelDetailFor(String id) => '/channels/$id';
  static String activeCallFor(String callId) => '/calls/active/$callId';
  static String incomingCallFor(String callId) => '/calls/incoming/$callId';
}
