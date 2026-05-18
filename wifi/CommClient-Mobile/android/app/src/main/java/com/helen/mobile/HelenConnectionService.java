package com.helen.mobile;

import android.os.Build;
import android.telecom.Connection;
import android.telecom.ConnectionRequest;
import android.telecom.ConnectionService;
import android.telecom.DisconnectCause;
import android.telecom.PhoneAccountHandle;

import androidx.annotation.RequiresApi;

/**
 * Helen — Self-managed Telecom ConnectionService (API 26+).
 *
 * Telling Android's Telecom framework about our calls unlocks the
 * native call surface: audio routing through the system audio
 * focus, Bluetooth headset controls, hold/resume on GSM-call collision,
 * Android Auto integration, Wear OS forwarding, and the OS-rendered
 * incoming-call UI on devices that show it (Pixel + Samsung).
 *
 * Self-managed (vs phone-account managed) means:
 *   • We don't replace the dialer — Helen stays its own UI.
 *   • We don't appear as an outgoing-call option for tel: URLs.
 *   • We DO get audio routing, OS interrupt handling, and the Android-
 *     side concept of "active call" that other apps respect.
 *
 * The plugin (HelenConnectionPlugin) registers a PhoneAccountHandle once
 * with CAPABILITY_SELF_MANAGED, then asks TelecomManager to place /
 * accept calls; the framework calls back into us here, and we return a
 * lightweight HelenConnection that mirrors the WebRTC call's state.
 */
@RequiresApi(Build.VERSION_CODES.O)
public class HelenConnectionService extends ConnectionService {

    public static final String EXTRA_CHANNEL_ID = "helen.channel_id";
    public static final String EXTRA_PEER_NAME  = "helen.peer_name";
    public static final String EXTRA_IS_VIDEO   = "helen.is_video";

    @Override
    public Connection onCreateOutgoingConnection(
            PhoneAccountHandle account, ConnectionRequest request) {
        return buildConnection(request, /*incoming=*/false);
    }

    @Override
    public Connection onCreateIncomingConnection(
            PhoneAccountHandle account, ConnectionRequest request) {
        return buildConnection(request, /*incoming=*/true);
    }

    @Override
    public void onCreateIncomingConnectionFailed(
            PhoneAccountHandle account, ConnectionRequest request) {
        // Telecom rejected our incoming call (e.g. user blocked the channel
        // in system settings). Log so the renderer can surface the reason.
        android.util.Log.w("HelenConnSvc",
            "Incoming connection rejected by Telecom: " + request);
    }

    @Override
    public void onCreateOutgoingConnectionFailed(
            PhoneAccountHandle account, ConnectionRequest request) {
        android.util.Log.w("HelenConnSvc",
            "Outgoing connection rejected by Telecom: " + request);
    }

    private Connection buildConnection(ConnectionRequest request, boolean incoming) {
        HelenConnection conn = new HelenConnection(getApplicationContext());

        // CAPABILITY_HOLD lets the OS pause our call when a GSM call comes
        // in. We honour onHold/onUnhold by mirroring to WebRTC tracks.
        conn.setConnectionCapabilities(
            Connection.CAPABILITY_HOLD
          | Connection.CAPABILITY_SUPPORT_HOLD
          | Connection.CAPABILITY_MUTE
        );
        conn.setConnectionProperties(Connection.PROPERTY_SELF_MANAGED);
        conn.setAudioModeIsVoip(true);

        if (request.getExtras() != null) {
            String peer = request.getExtras().getString(EXTRA_PEER_NAME);
            if (peer != null) conn.setCallerDisplayName(
                peer, android.telecom.TelecomManager.PRESENTATION_ALLOWED
            );
        }
        conn.setVideoState(
            request.getVideoState() != 0
              ? request.getVideoState()
              : android.telecom.VideoProfile.STATE_AUDIO_ONLY
        );

        if (incoming) {
            conn.setRinging();
        } else {
            conn.setDialing();
        }
        return conn;
    }

    /**
     * Lightweight Connection that mirrors WebRTC state. Telecom calls
     * back here when the user accepts/rejects from the OS UI, switches
     * audio routes, or holds for a GSM call. We translate each event
     * into something the renderer can react to via a sticky broadcast
     * picked up by HelenCallPlugin.
     */
    @RequiresApi(Build.VERSION_CODES.O)
    static class HelenConnection extends Connection {
        // Connection isn't a Context, so we cache the service's
        // application context at construction. ActivityThread.
        // currentApplication() (the previous trick) is a hidden API
        // that's been blocked by the modular SDK greylist.
        private final android.content.Context appContext;

        HelenConnection(android.content.Context appContext) {
            this.appContext = appContext;
        }

        @Override
        public void onAnswer(int videoState) {
            super.onAnswer(videoState);
            setActive();
            HelenConnectionEvents.broadcast(getApplicationContext(), "answer", this);
        }

        private android.content.Context getApplicationContext() {
            return appContext;
        }

        @Override
        public void onReject() {
            super.onReject();
            setDisconnected(new DisconnectCause(DisconnectCause.REJECTED));
            HelenConnectionEvents.broadcast(getApplicationContext(), "reject", this);
            destroy();
        }

        @Override
        public void onDisconnect() {
            super.onDisconnect();
            setDisconnected(new DisconnectCause(DisconnectCause.LOCAL));
            HelenConnectionEvents.broadcast(getApplicationContext(), "disconnect", this);
            destroy();
        }

        @Override
        public void onHold() {
            super.onHold();
            setOnHold();
            HelenConnectionEvents.broadcast(getApplicationContext(), "hold", this);
        }

        @Override
        public void onUnhold() {
            super.onUnhold();
            setActive();
            HelenConnectionEvents.broadcast(getApplicationContext(), "unhold", this);
        }

        @Override
        public void onAbort() {
            super.onAbort();
            setDisconnected(new DisconnectCause(DisconnectCause.CANCELED));
            HelenConnectionEvents.broadcast(getApplicationContext(), "abort", this);
            destroy();
        }
    }
}
