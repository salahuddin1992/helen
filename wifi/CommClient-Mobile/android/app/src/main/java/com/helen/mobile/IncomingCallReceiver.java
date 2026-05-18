package com.helen.mobile;

import android.app.NotificationManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.net.Uri;

/**
 * Helen — incoming-call notification action handler.
 *
 * Wired to the Accept / Decline buttons on the heads-up notification we
 * post when a Socket.IO `call:incoming` event arrives. Both actions:
 *
 *   1. Dismiss the heads-up notification.
 *   2. Bring MainActivity to the front with an extra describing the
 *      decision — the React renderer reads it on resume and either
 *      auto-answers or signals decline back through the API.
 *
 * Decline additionally stops any active CallForegroundService that may
 * have been speculatively started.
 */
public class IncomingCallReceiver extends BroadcastReceiver {

    public static final String ACTION_ACCEPT  = "com.helen.mobile.CALL_ACCEPT";
    public static final String ACTION_DECLINE = "com.helen.mobile.CALL_DECLINE";

    public static final String EXTRA_NOTIF_ID    = "notif_id";
    public static final String EXTRA_CHANNEL_ID  = "channel_id";
    public static final String EXTRA_CALLER_ID   = "caller_id";
    public static final String EXTRA_IS_VIDEO    = "is_video";

    @Override
    public void onReceive(Context context, Intent intent) {
        String action = intent.getAction();
        if (action == null) return;

        int notifId = intent.getIntExtra(EXTRA_NOTIF_ID, -1);
        String channelId = intent.getStringExtra(EXTRA_CHANNEL_ID);
        String callerId  = intent.getStringExtra(EXTRA_CALLER_ID);
        boolean isVideo  = intent.getBooleanExtra(EXTRA_IS_VIDEO, false);

        // Always dismiss the notification.
        if (notifId > 0) {
            NotificationManager nm = (NotificationManager)
                context.getSystemService(Context.NOTIFICATION_SERVICE);
            if (nm != null) nm.cancel(notifId);
        }

        if (ACTION_DECLINE.equals(action)) {
            // Tell the foreground service to stop (no-op if not running).
            Intent stop = new Intent(context, CallForegroundService.class)
                .setAction(CallForegroundService.ACTION_STOP);
            context.startService(stop);

            // Bring app to front so the renderer can post the decline to API.
            launchAppWithDecision(context, "decline", channelId, callerId, isVideo);
            return;
        }

        if (ACTION_ACCEPT.equals(action)) {
            launchAppWithDecision(context, "accept", channelId, callerId, isVideo);
        }
    }

    private void launchAppWithDecision(Context context, String decision,
                                       String channelId, String callerId,
                                       boolean isVideo) {
        // Encode the decision as a deep-link URL so the renderer receives
        // it through Capacitor's standard `App.appUrlOpen` listener — no
        // bespoke Intent-extras plumbing required on the JS side.
        Uri uri = Uri.parse("helen://call/" + decision)
            .buildUpon()
            .appendQueryParameter("channelId", channelId == null ? "" : channelId)
            .appendQueryParameter("callerId",  callerId  == null ? "" : callerId)
            .appendQueryParameter("isVideo",   String.valueOf(isVideo))
            .build();

        Intent open = new Intent(Intent.ACTION_VIEW, uri)
            .setPackage(context.getPackageName())
            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                    | Intent.FLAG_ACTIVITY_SINGLE_TOP
                    | Intent.FLAG_ACTIVITY_CLEAR_TOP);
        context.startActivity(open);
    }
}
