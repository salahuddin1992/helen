package com.helen.mobile;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Context;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.net.wifi.WifiManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import androidx.core.app.NotificationCompat;

/**
 * Helen — active-call foreground service.
 *
 * Started by HelenCallPlugin when the React renderer enters a call. Keeps
 * the OS from killing the process while the user backgrounds the app:
 *
 *   • Persistent notification (required by Android 8+ for foreground svc).
 *   • PARTIAL_WAKE_LOCK so the CPU stays awake to feed the WebRTC pipeline.
 *   • WiFi Multicast lock so any concurrent mDNS rediscovery still works.
 *   • Stop intent on the notification so the user can hang up from the
 *     shade without unlocking the device.
 *
 * The foreground type is the union of microphone + camera + media-projection
 * (for screen-share) + dataSync (for file transfer mid-call) — declared in
 * AndroidManifest.xml.
 */
public class CallForegroundService extends Service {

    public static final String ACTION_START = "com.helen.mobile.CALL_START";
    public static final String ACTION_STOP  = "com.helen.mobile.CALL_STOP";

    public static final String EXTRA_CHANNEL_ID = "channel_id";
    public static final String EXTRA_PEER_NAME  = "peer_name";
    public static final String EXTRA_IS_VIDEO   = "is_video";

    static final String NOTIF_CHANNEL_ID = "helen_active_call";
    static final int     NOTIF_ID        = 1001;

    private PowerManager.WakeLock wakeLock;
    private WifiManager.MulticastLock multicastLock;

    @Override
    public void onCreate() {
        super.onCreate();
        ensureNotificationChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        String action = intent != null ? intent.getAction() : null;
        if (ACTION_STOP.equals(action)) {
            stopSelf();
            return START_NOT_STICKY;
        }

        String channelId = intent != null ? intent.getStringExtra(EXTRA_CHANNEL_ID) : null;
        String peerName  = intent != null ? intent.getStringExtra(EXTRA_PEER_NAME)  : null;
        boolean isVideo  = intent != null && intent.getBooleanExtra(EXTRA_IS_VIDEO, false);

        Notification notification = buildCallNotification(
            peerName != null ? peerName : "Helen call",
            isVideo,
            channelId
        );

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
            // Android 14+: explicit foreground service type required.
            int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE
                     | ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
            if (isVideo) type |= ServiceInfo.FOREGROUND_SERVICE_TYPE_CAMERA;
            startForeground(NOTIF_ID, notification, type);
        } else if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            int type = ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC;
            startForeground(NOTIF_ID, notification, type);
        } else {
            startForeground(NOTIF_ID, notification);
        }

        acquireLocks();
        return START_STICKY;
    }

    @Override
    public void onDestroy() {
        releaseLocks();
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
            stopForeground(STOP_FOREGROUND_REMOVE);
        } else {
            //noinspection deprecation
            stopForeground(true);
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }

    // ── Helpers ──────────────────────────────────────────────────────

    private void ensureNotificationChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = getSystemService(NotificationManager.class);
        if (nm == null) return;
        NotificationChannel ch = new NotificationChannel(
            NOTIF_CHANNEL_ID,
            "Active Helen calls",
            NotificationManager.IMPORTANCE_LOW   // silent — call audio carries the UX
        );
        ch.setDescription("Persistent indicator while you're on a Helen call.");
        ch.setShowBadge(false);
        ch.setSound(null, null);
        nm.createNotificationChannel(ch);
    }

    private Notification buildCallNotification(String title, boolean isVideo, String channelId) {
        // Tap → open the app on the active call screen.
        Intent open = new Intent(this, MainActivity.class)
            .setFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP);
        if (channelId != null) open.putExtra("openChannelId", channelId);
        PendingIntent openPi = PendingIntent.getActivity(
            this, 0, open,
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT
        );

        // Hang-up action.
        Intent hangup = new Intent(this, CallForegroundService.class)
            .setAction(ACTION_STOP);
        PendingIntent hangupPi = PendingIntent.getService(
            this, 1, hangup, PendingIntent.FLAG_IMMUTABLE
        );

        return new NotificationCompat.Builder(this, NOTIF_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_call)
            .setContentTitle(isVideo ? "Helen video call" : "Helen call")
            .setContentText(title)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setUsesChronometer(true)
            .setShowWhen(true)
            .setWhen(System.currentTimeMillis())
            .setContentIntent(openPi)
            .addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                "Hang up",
                hangupPi
            )
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .build();
    }

    private void acquireLocks() {
        try {
            PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
            if (pm != null) {
                wakeLock = pm.newWakeLock(
                    PowerManager.PARTIAL_WAKE_LOCK,
                    "Helen::ActiveCall"
                );
                wakeLock.setReferenceCounted(false);
                wakeLock.acquire(60 * 60 * 1000L);   // 1h cap
            }
            WifiManager wm = (WifiManager) getApplicationContext()
                .getSystemService(Context.WIFI_SERVICE);
            if (wm != null) {
                multicastLock = wm.createMulticastLock("Helen::MulticastDuringCall");
                multicastLock.setReferenceCounted(false);
                multicastLock.acquire();
            }
        } catch (Exception ignored) { /* best-effort */ }
    }

    private void releaseLocks() {
        try {
            if (wakeLock != null && wakeLock.isHeld()) wakeLock.release();
        } catch (Exception ignored) {}
        try {
            if (multicastLock != null && multicastLock.isHeld()) multicastLock.release();
        } catch (Exception ignored) {}
        wakeLock = null;
        multicastLock = null;
    }
}
