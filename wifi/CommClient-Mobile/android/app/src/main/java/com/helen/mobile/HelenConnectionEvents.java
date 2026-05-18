package com.helen.mobile;

import android.content.Context;
import android.content.Intent;
import android.os.Build;
import android.telecom.Connection;
import android.telecom.DisconnectCause;

import androidx.annotation.RequiresApi;

/**
 * Helen — Telecom Connection event bus.
 *
 * The ConnectionService callbacks (`onAnswer`, `onHold`, `onDisconnect`,
 * etc.) run inside Telecom's own process boundary; we can't return data
 * to the renderer from there directly. This helper broadcasts each
 * event as a `helen://telecom/<event>?...` deep-link Intent that
 * Capacitor's App plugin surfaces to JS via `appUrlOpen`, mirroring
 * the same pattern we use for IncomingCallReceiver.
 *
 * Decoupling Telecom from JS this way means the renderer never has to
 * import an Android-specific binding — it just listens for one URL
 * scheme and handles every native call lifecycle there.
 */
@RequiresApi(Build.VERSION_CODES.O)
final class HelenConnectionEvents {

    private HelenConnectionEvents() {}

    static void broadcast(Context ctx, String event, Connection conn) {
        if (ctx == null) return;

        StringBuilder sb = new StringBuilder("helen://telecom/").append(event);
        sb.append("?audio=").append(audioRoute(conn));
        sb.append("&state=").append(stateName(conn));
        if (conn.getDisconnectCause() != null) {
            DisconnectCause dc = conn.getDisconnectCause();
            sb.append("&cause=").append(dc.getCode());
        }

        Intent open = new Intent(Intent.ACTION_VIEW, android.net.Uri.parse(sb.toString()))
            .setPackage(ctx.getPackageName())
            .setFlags(Intent.FLAG_ACTIVITY_NEW_TASK
                    | Intent.FLAG_ACTIVITY_SINGLE_TOP);
        try {
            ctx.startActivity(open);
        } catch (Exception ignored) {
            // App may have been killed already; renderer will catch up
            // through Socket.IO state on next launch.
        }
    }

    private static String audioRoute(Connection conn) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.P) return "unknown";
        try {
            android.telecom.CallAudioState s = conn.getCallAudioState();
            if (s == null) return "unknown";
            switch (s.getRoute()) {
                case android.telecom.CallAudioState.ROUTE_BLUETOOTH: return "bluetooth";
                case android.telecom.CallAudioState.ROUTE_EARPIECE:  return "earpiece";
                case android.telecom.CallAudioState.ROUTE_SPEAKER:   return "speaker";
                case android.telecom.CallAudioState.ROUTE_WIRED_HEADSET: return "wired";
                default: return "unknown";
            }
        } catch (Exception e) {
            return "unknown";
        }
    }

    private static String stateName(Connection conn) {
        switch (conn.getState()) {
            case Connection.STATE_INITIALIZING: return "initializing";
            case Connection.STATE_NEW:          return "new";
            case Connection.STATE_RINGING:      return "ringing";
            case Connection.STATE_DIALING:      return "dialing";
            case Connection.STATE_ACTIVE:       return "active";
            case Connection.STATE_HOLDING:      return "holding";
            case Connection.STATE_DISCONNECTED: return "disconnected";
            case Connection.STATE_PULLING_CALL: return "pulling";
            default: return "unknown";
        }
    }
}
