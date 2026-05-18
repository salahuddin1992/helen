/// App-wide constants. Centralized so magic numbers/strings live in one place.
library;

class K {
  K._();

  // Storage keys (flutter_secure_storage)
  static const String kAccessToken = 'helen.access_token';
  static const String kRefreshToken = 'helen.refresh_token';
  static const String kUserId = 'helen.user_id';
  static const String kServerUrl = 'helen.server_url';
  static const String kSocketUrl = 'helen.socket_url';
  static const String kBiometricEnabled = 'helen.biometric_enabled';
  static const String kPushToken = 'helen.push_token';

  // API paths
  static const String pAuthLogin = '/api/auth/login';
  static const String pAuthRegister = '/api/auth/register';
  static const String pAuthRefresh = '/api/auth/refresh';
  static const String pAuthLogout = '/api/auth/logout';
  static const String pUsersMe = '/api/users/me';
  static const String pChannels = '/api/channels';
  static const String pMessagesSearch = '/api/messages/search';
  static const String pFilesUpload = '/api/files/upload';
  static const String pFilesResumableInit = '/api/files/resumable/init';
  static const String pFilesResumableChunk = '/api/files/resumable/chunk';
  static const String pFilesResumableComplete = '/api/files/resumable/complete';
  static const String pCallsInit = '/api/calls/init';
  static const String pIceConfig = '/api/turn/ice-config';

  // Module O — pairing v2
  static const String pPairV2Start = '/api/pairing/v2/start';
  static const String pPairV2Complete = '/api/pairing/v2/complete';
  static const String pPairV2Poll = '/api/pairing/v2/poll';

  // Module N — OAuth
  static const String pOauthProviders = '/api/oauth/providers';
  static String pOauthAuthorize(String provider) =>
      '/api/oauth/authorize/$provider';
  static String pOauthCallback(String provider) =>
      '/api/oauth/callback/$provider';

  // Socket events
  static const String sEvtMessage = 'message';
  static const String sEvtMessageEdit = 'message:edit';
  static const String sEvtMessageDelete = 'message:delete';
  static const String sEvtReaction = 'message:reaction';
  static const String sEvtTyping = 'typing';
  static const String sEvtPresence = 'presence';
  static const String sEvtCallInvite = 'call:invite';
  static const String sEvtCallAccept = 'call:accept';
  static const String sEvtCallReject = 'call:reject';
  static const String sEvtCallEnd = 'call:end';
  static const String sEvtIceCandidate = 'call:ice';
  static const String sEvtSdpOffer = 'call:offer';
  static const String sEvtSdpAnswer = 'call:answer';

  // UI
  static const Duration animFast = Duration(milliseconds: 150);
  static const Duration animMedium = Duration(milliseconds: 300);
  static const Duration animSlow = Duration(milliseconds: 500);
  static const Duration debounceTyping = Duration(milliseconds: 500);
  static const Duration toastShort = Duration(seconds: 2);
  static const Duration toastLong = Duration(seconds: 4);

  // Pagination
  static const int defaultMessagePageSize = 50;
  static const int defaultChannelPageSize = 25;

  // Files
  static const int defaultChunkSize = 1024 * 1024; // 1 MiB
  static const int maxUploadSize = 2 * 1024 * 1024 * 1024; // 2 GiB
}
