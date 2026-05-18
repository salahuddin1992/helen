package com.helen.mobile;

import android.Manifest;
import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.content.Context;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.net.wifi.WifiManager;
import android.os.Build;

import androidx.core.app.NotificationCompat;
import androidx.core.content.ContextCompat;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;
import com.getcapacitor.annotation.PermissionCallback;

import java.util.concurrent.atomic.AtomicInteger;

/**
 * Helen — native call lifecycle bridge for the React renderer.
 *
 * The renderer is shared with desktop / web and runs the same WebRTC code
 * everywhere. On Android, two things have to happen on the native side
 * to keep the call session healthy:
 *
 *   1. Start a foreground service while the call is active so the OS
 *      doesn't kill the WebView when the user backgrounds the app.
 *   2. Surface incoming-call signalling as a heads-up notification with
 *      Accept / Decline actions, even when the app is in the background.
 *
 * The renderer calls this plugin via Capacitor's `registerPlugin` API
 * (see assets/mobile-bridge.js for the JS shim).
 *
 * JS API:
 *   await HelenCall.startActiveCall({ channelId, peerName, isVideo })
 *   await HelenCall.stopActiveCall()
 *   await HelenCall.notifyIncomingCall({ callerName, callerId,
 *                                         channelId, isVideo })
 *   await HelenCall.cancelIncomingCall({ notifId })
 *   await HelenCall.acquireMulticastLock()
 *   await HelenCall.releaseMulticastLock()
 *   await HelenCall.isOnCall()                  → { active: boolean }
 */
@CapacitorPlugin(
    name = "HelenCall",
    permissions = {
        @Permission(strings = { Manifest.permission.POST_NOTIFICATIONS },
                    alias   = "notifications")
    }
)
public class HelenCallPlugin extends Plugin {

    private static final String INCOMING_CHANNEL_ID = "helen_incoming_calls";
    private static final AtomicInteger NEXT_NOTIF_ID = new AtomicInteger(2000);

    private WifiManager.MulticastLock standaloneMulticastLock;
    private boolean callActive = false;

    // ── Active call ──────────────────────────────────────────────────

    @PluginMethod
    public void startActiveCall(PluginCall call) {
        String channelId = call.getString("channelId");
        String peerName  = call.getString("peerName", "Helen");
        boolean isVideo  = Boolean.TRUE.equals(call.getBoolean("isVideo", false));

        Intent intent = new Intent(getContext(), CallForegroundService.class)
            .setAction(CallForegroundService.ACTION_START)
            .putExtra(CallForegroundService.EXTRA_CHANNEL_ID, channelId)
            .putExtra(CallForegroundService.EXTRA_PEER_NAME,  peerName)
            .putExtra(CallForegroundService.EXTRA_IS_VIDEO,   isVideo);

        try {
            ContextCompat.startForegroundService(getContext(), intent);
            callActive = true;
            JSObject ret = new JSObject();
            ret.put("started", true);
            call.resolve(ret);
        } catch (Exception e) {
            call.reject("Failed to start foreground service: " + e.getMessage());
        }
    }

    @PluginMethod
    public void stopActiveCall(PluginCall call) {
        Intent intent = new Intent(getContext(), CallForegroundService.class)
            .setAction(CallForegroundService.ACTION_STOP);
        getContext().startService(intent);
        callActive = false;
        JSObject ret = new JSObject();
        ret.put("stopped", true);
        call.resolve(ret);
    }

    @PluginMethod
    public void isOnCall(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("active", callActive);
        call.resolve(ret);
    }

    // ── Incoming call notification ───────────────────────────────────

    @PluginMethod
    public void notifyIncomingCall(PluginCall call) {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU
            && getContext().checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
               != PackageManager.PERMISSION_GRANTED) {
            // Defer: ask the user, then re-invoke from the JS side.
            call.reject("notifications-permission-denied");
            return;
        }

        ensureIncomingChannel();

        String callerName = call.getString("callerName", "Helen call");
        String callerId   = call.getString("callerId");
        String channelId  = call.getString("channelId");
        boolean isVideo   = Boolean.TRUE.equals(call.getBoolean("isVideo", false));

        int notifId = NEXT_NOTIF_ID.getAndIncrement();

        // Full-screen intent → unlock + open app on Android < 14, fallback
        // to heads-up only on 14+ unless USE_FULL_SCREEN_INTENT is granted.
        Intent fullScreen = new Intent(getContext(), MainActivity.class)
            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                    | Intent.FLAG_ACTIVITY_SINGLE_TOP
                    | Intent.FLAG_ACTIVITY_CLEAR_TOP)
            .putExtra("incomingCallChannelId", channelId)
            .putExtra("incomingCallCallerId",  callerId)
            .putExtra("incomingCallIsVideo",   isVideo);
        PendingIntent fullScreenPi = PendingIntent.getActivity(
            getContext(), notifId, fullScreen,
            PendingIntent.FLAG_IMMUTABLE | PendingIntent.FLAG_UPDATE_CURRENT
        );

        Intent accept = new Intent(getContext(), IncomingCallReceiver.class)
            .setAction(IncomingCallReceiver.ACTION_ACCEPT)
            .putExtra(IncomingCallReceiver.EXTRA_NOTIF_ID,   notifId)
            .putExtra(IncomingCallReceiver.EXTRA_CHANNEL_ID, channelId)
            .putExtra(IncomingCallReceiver.EXTRA_CALLER_ID,  callerId)
            .putExtra(IncomingCallReceiver.EXTRA_IS_VIDEO,   isVideo);
        PendingIntent acceptPi = PendingIntent.getBroadcast(
            getContext(), notifId * 2, accept, PendingIntent.FLAG_IMMUTABLE
        );

        Intent decline = new Intent(getContext(), IncomingCallReceiver.class)
            .setAction(IncomingCallReceiver.ACTION_DECLINE)
            .putExtra(IncomingCallReceiver.EXTRA_NOTIF_ID,   notifId)
            .putExtra(IncomingCallReceiver.EXTRA_CHANNEL_ID, channelId)
            .putExtra(IncomingCallReceiver.EXTRA_CALLER_ID,  callerId);
        PendingIntent declinePi = PendingIntent.getBroadcast(
            getContext(), notifId * 2 + 1, decline, PendingIntent.FLAG_IMMUTABLE
        );

        Notification notif = new NotificationCompat.Builder(getContext(), INCOMING_CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_call)
            .setContentTitle(isVideo ? "Incoming video call" : "Incoming call")
            .setContentText(callerName)
            .setPriority(NotificationCompat.PRIORITY_MAX)
            .setCategory(NotificationCompat.CATEGORY_CALL)
            .setOngoing(true)
            .setAutoCancel(false)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .setContentIntent(fullScreenPi)
            .setFullScreenIntent(fullScreenPi, true)
            .setSound(android.provider.Settings.System.DEFAULT_RINGTONE_URI,
                      android.media.AudioManager.STREAM_RING)
            .setVibrate(new long[]{ 0, 1000, 1000, 1000, 1000 })
            .addAction(
                android.R.drawable.ic_menu_close_clear_cancel,
                "Decline",
                declinePi
            )
            .addAction(
                android.R.drawable.ic_menu_call,
                isVideo ? "Answer video" : "Answer",
                acceptPi
            )
            .build();

        NotificationManager nm = (NotificationManager) getContext()
            .getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) nm.notify(notifId, notif);

        JSObject ret = new JSObject();
        ret.put("notifId", notifId);
        call.resolve(ret);
    }

    @PluginMethod
    public void cancelIncomingCall(PluginCall call) {
        int notifId = call.getInt("notifId", -1);
        if (notifId <= 0) {
            call.reject("Missing notifId");
            return;
        }
        NotificationManager nm = (NotificationManager) getContext()
            .getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm != null) nm.cancel(notifId);
        call.resolve();
    }

    // ── Multicast lock for mDNS rediscovery ──────────────────────────

    @PluginMethod
    public void acquireMulticastLock(PluginCall call) {
        try {
            if (standaloneMulticastLock == null) {
                WifiManager wm = (WifiManager) getContext().getApplicationContext()
                    .getSystemService(Context.WIFI_SERVICE);
                if (wm == null) { call.reject("WIFI_SERVICE unavailable"); return; }
                standaloneMulticastLock = wm.createMulticastLock("Helen::mDNS");
                standaloneMulticastLock.setReferenceCounted(false);
            }
            if (!standaloneMulticastLock.isHeld()) standaloneMulticastLock.acquire();
            call.resolve();
        } catch (Exception e) {
            call.reject("acquireMulticastLock: " + e.getMessage());
        }
    }

    @PluginMethod
    public void releaseMulticastLock(PluginCall call) {
        try {
            if (standaloneMulticastLock != null && standaloneMulticastLock.isHeld()) {
                standaloneMulticastLock.release();
            }
            call.resolve();
        } catch (Exception e) {
            call.reject("releaseMulticastLock: " + e.getMessage());
        }
    }

    // ── Permission flow for POST_NOTIFICATIONS (Android 13+) ─────────

    @PluginMethod
    public void requestNotificationsPermission(PluginCall call) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.TIRAMISU) {
            JSObject ret = new JSObject();
            ret.put("granted", true);
            call.resolve(ret);
            return;
        }
        if (getPermissionState("notifications") == com.getcapacitor.PermissionState.GRANTED) {
            JSObject ret = new JSObject();
            ret.put("granted", true);
            call.resolve(ret);
            return;
        }
        requestPermissionForAlias("notifications", call, "permissionResult");
    }

    @PermissionCallback
    private void permissionResult(PluginCall call) {
        boolean granted = getPermissionState("notifications")
            == com.getcapacitor.PermissionState.GRANTED;
        JSObject ret = new JSObject();
        ret.put("granted", granted);
        call.resolve(ret);
    }

    // ── Helpers ──────────────────────────────────────────────────────

    private void ensureIncomingChannel() {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) return;
        NotificationManager nm = (NotificationManager) getContext()
            .getSystemService(Context.NOTIFICATION_SERVICE);
        if (nm == null) return;
        if (nm.getNotificationChannel(INCOMING_CHANNEL_ID) != null) return;

        NotificationChannel ch = new NotificationChannel(
            INCOMING_CHANNEL_ID,
            "Incoming Helen calls",
            NotificationManager.IMPORTANCE_HIGH
        );
        ch.setDescription("Heads-up notifications for incoming Helen calls.");
        ch.enableVibration(true);
        ch.setVibrationPattern(new long[]{ 0, 1000, 1000, 1000, 1000 });
        ch.setShowBadge(true);
        ch.setBypassDnd(true);
        ch.setSound(
            android.provider.Settings.System.DEFAULT_RINGTONE_URI,
            new android.media.AudioAttributes.Builder()
                .setUsage(android.media.AudioAttributes.USAGE_NOTIFICATION_RINGTONE)
                .setContentType(android.media.AudioAttributes.CONTENT_TYPE_SONIFICATION)
                .build()
        );
        ch.setLockscreenVisibility(Notification.VISIBILITY_PUBLIC);
        nm.createNotificationChannel(ch);
    }
}
