package com.helen.mobile;

import android.Manifest;
import android.content.ComponentName;
import android.content.pm.PackageManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.telecom.PhoneAccount;
import android.telecom.PhoneAccountHandle;
import android.telecom.TelecomManager;
import android.telecom.VideoProfile;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;
import com.getcapacitor.annotation.Permission;

/**
 * Helen — Telecom integration bridge (Self-Managed ConnectionService).
 *
 * Available API 26+ (Android 8.0). On older devices every method
 * resolves with `{ supported: false }` so the renderer can fall through
 * to its existing heads-up notification path.
 *
 * JS API:
 *   await HelenConnection.isSupported()                        → { supported }
 *   await HelenConnection.registerPhoneAccount()               → { registered }
 *   await HelenConnection.placeOutgoingCall({ channelId, peerName, isVideo })
 *   await HelenConnection.notifyIncomingCall({ channelId, callerName, isVideo })
 *   await HelenConnection.unregisterPhoneAccount()
 *
 * Event channel: `helen://telecom/<event>?audio=…&state=…&cause=…`
 *   events: answer, reject, disconnect, hold, unhold, abort
 *   audio:  earpiece | speaker | bluetooth | wired | unknown
 *
 * The renderer subscribes through `electronAPI.connection.onTelecomEvent`
 * which wraps Capacitor's `appUrlOpen` listener.
 */
@CapacitorPlugin(
    name = "HelenConnection",
    permissions = {
        @Permission(strings = { Manifest.permission.MANAGE_OWN_CALLS },
                    alias = "manage_own_calls")
    }
)
public class HelenConnectionPlugin extends Plugin {

    private static final String ACCOUNT_ID = "helen-mobile-account";

    @PluginMethod
    public void isSupported(PluginCall call) {
        JSObject ret = new JSObject();
        boolean ok = Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                  && getContext().getPackageManager()
                       .hasSystemFeature(PackageManager.FEATURE_TELEPHONY) == false
                          ? Build.VERSION.SDK_INT >= Build.VERSION_CODES.O
                          : Build.VERSION.SDK_INT >= Build.VERSION_CODES.O;
        // (telephony presence isn't required for self-managed accounts;
        //  Wi-Fi-only tablets work too — we just need API 26+.)
        ret.put("supported", Build.VERSION.SDK_INT >= Build.VERSION_CODES.O);
        ret.put("apiLevel",  Build.VERSION.SDK_INT);
        call.resolve(ret);
    }

    @PluginMethod
    public void registerPhoneAccount(PluginCall call) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            JSObject ret = new JSObject();
            ret.put("registered", false);
            ret.put("reason", "api-too-low");
            call.resolve(ret);
            return;
        }
        try {
            TelecomManager tm = (TelecomManager)
                getContext().getSystemService(android.content.Context.TELECOM_SERVICE);
            if (tm == null) { call.reject("TelecomManager unavailable"); return; }

            PhoneAccountHandle handle = makeHandle();
            PhoneAccount account = PhoneAccount.builder(handle, "Helen")
                .setCapabilities(
                    PhoneAccount.CAPABILITY_SELF_MANAGED
                  | PhoneAccount.CAPABILITY_SUPPORTS_VIDEO_CALLING
                  | PhoneAccount.CAPABILITY_VIDEO_CALLING
                )
                .setShortDescription("Helen LAN calls")
                .build();
            tm.registerPhoneAccount(account);

            JSObject ret = new JSObject();
            ret.put("registered", true);
            ret.put("accountId",  ACCOUNT_ID);
            call.resolve(ret);
        } catch (SecurityException se) {
            // MANAGE_OWN_CALLS not granted yet.
            call.reject("missing-permission: MANAGE_OWN_CALLS");
        } catch (Exception e) {
            call.reject("registerPhoneAccount: " + e.getMessage());
        }
    }

    @PluginMethod
    public void unregisterPhoneAccount(PluginCall call) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            call.resolve();
            return;
        }
        try {
            TelecomManager tm = (TelecomManager)
                getContext().getSystemService(android.content.Context.TELECOM_SERVICE);
            if (tm != null) tm.unregisterPhoneAccount(makeHandle());
            call.resolve();
        } catch (Exception e) {
            call.reject("unregisterPhoneAccount: " + e.getMessage());
        }
    }

    @PluginMethod
    public void placeOutgoingCall(PluginCall call) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            JSObject ret = new JSObject();
            ret.put("placed", false);
            ret.put("reason", "api-too-low");
            call.resolve(ret);
            return;
        }
        try {
            String channelId = call.getString("channelId");
            String peerName  = call.getString("peerName", "Helen");
            boolean isVideo  = Boolean.TRUE.equals(call.getBoolean("isVideo", false));
            if (channelId == null) { call.reject("Missing channelId"); return; }

            TelecomManager tm = (TelecomManager)
                getContext().getSystemService(android.content.Context.TELECOM_SERVICE);
            if (tm == null) { call.reject("TelecomManager unavailable"); return; }

            Bundle extras = new Bundle();
            extras.putParcelable(
                TelecomManager.EXTRA_PHONE_ACCOUNT_HANDLE, makeHandle()
            );
            Bundle inner = new Bundle();
            inner.putString(HelenConnectionService.EXTRA_CHANNEL_ID, channelId);
            inner.putString(HelenConnectionService.EXTRA_PEER_NAME,  peerName);
            inner.putBoolean(HelenConnectionService.EXTRA_IS_VIDEO,  isVideo);
            extras.putBundle(TelecomManager.EXTRA_OUTGOING_CALL_EXTRAS, inner);
            extras.putInt(TelecomManager.EXTRA_START_CALL_WITH_VIDEO_STATE,
                isVideo ? VideoProfile.STATE_BIDIRECTIONAL
                        : VideoProfile.STATE_AUDIO_ONLY);

            // Self-managed accounts use a synthetic URI (the channel ID,
            // not a phone number) — Helen never dials PSTN.
            Uri uri = Uri.fromParts("helen", channelId, null);
            tm.placeCall(uri, extras);

            JSObject ret = new JSObject();
            ret.put("placed", true);
            call.resolve(ret);
        } catch (SecurityException se) {
            call.reject("missing-permission: MANAGE_OWN_CALLS");
        } catch (Exception e) {
            call.reject("placeOutgoingCall: " + e.getMessage());
        }
    }

    @PluginMethod
    public void notifyIncomingCall(PluginCall call) {
        if (Build.VERSION.SDK_INT < Build.VERSION_CODES.O) {
            JSObject ret = new JSObject();
            ret.put("notified", false);
            ret.put("reason", "api-too-low");
            call.resolve(ret);
            return;
        }
        try {
            String channelId  = call.getString("channelId");
            String callerName = call.getString("callerName", "Helen call");
            boolean isVideo   = Boolean.TRUE.equals(call.getBoolean("isVideo", false));
            if (channelId == null) { call.reject("Missing channelId"); return; }

            TelecomManager tm = (TelecomManager)
                getContext().getSystemService(android.content.Context.TELECOM_SERVICE);
            if (tm == null) { call.reject("TelecomManager unavailable"); return; }

            Bundle extras = new Bundle();
            Bundle inner = new Bundle();
            inner.putString(HelenConnectionService.EXTRA_CHANNEL_ID, channelId);
            inner.putString(HelenConnectionService.EXTRA_PEER_NAME,  callerName);
            inner.putBoolean(HelenConnectionService.EXTRA_IS_VIDEO,  isVideo);
            extras.putBundle(TelecomManager.EXTRA_INCOMING_CALL_EXTRAS, inner);
            extras.putInt(TelecomManager.EXTRA_INCOMING_VIDEO_STATE,
                isVideo ? VideoProfile.STATE_BIDIRECTIONAL
                        : VideoProfile.STATE_AUDIO_ONLY);

            tm.addNewIncomingCall(makeHandle(), extras);

            JSObject ret = new JSObject();
            ret.put("notified", true);
            call.resolve(ret);
        } catch (SecurityException se) {
            call.reject("missing-permission: MANAGE_OWN_CALLS");
        } catch (Exception e) {
            call.reject("notifyIncomingCall: " + e.getMessage());
        }
    }

    private PhoneAccountHandle makeHandle() {
        ComponentName component = new ComponentName(
            getContext(), HelenConnectionService.class
        );
        return new PhoneAccountHandle(component, ACCOUNT_ID);
    }
}
