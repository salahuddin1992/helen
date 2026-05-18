package com.helen.mobile;

import android.os.Build;

import androidx.biometric.BiometricManager;
import androidx.biometric.BiometricPrompt;
import androidx.core.content.ContextCompat;
import androidx.fragment.app.FragmentActivity;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

import java.util.concurrent.Executor;

/**
 * Helen — encrypted secret store + biometric gate.
 *
 * JS API surface (registered as `HelenSecure`):
 *
 *   // Encrypted token storage
 *   await HelenSecure.setSecret({ key, value })   // AES-256-GCM via Keystore
 *   await HelenSecure.getSecret({ key })          → { value: string|null }
 *   await HelenSecure.removeSecret({ key })
 *   await HelenSecure.clearAll()
 *   await HelenSecure.info()                      → { encrypted, hardwareBacked }
 *
 *   // Biometric gate (BiometricPrompt — Class 3 / strong)
 *   await HelenSecure.canUseBiometrics()          → { available, reason? }
 *   await HelenSecure.authenticate({              // shows the BiometricPrompt UI
 *       title?:string, subtitle?:string, reason?:string,
 *       allowDeviceCredential?:boolean })          → { authenticated:true } or rejection
 *
 * The renderer wraps these via window.electronAPI.secure.* (mobile-bridge.js).
 * All methods are no-ops on desktop / web — the renderer falls through to
 * unencrypted `Preferences` and skips the biometric step there.
 */
@CapacitorPlugin(name = "HelenSecure")
public class HelenSecurePlugin extends Plugin {

    private HelenSecureStore store;

    @Override
    public void load() {
        store = new HelenSecureStore(getContext());
    }

    // ── Encrypted store ──────────────────────────────────────────────

    @PluginMethod
    public void setSecret(PluginCall call) {
        String key = call.getString("key");
        String val = call.getString("value");
        if (key == null || val == null) {
            call.reject("Missing key or value");
            return;
        }
        store.setString(key, val);
        call.resolve();
    }

    @PluginMethod
    public void getSecret(PluginCall call) {
        String key = call.getString("key");
        if (key == null) { call.reject("Missing key"); return; }
        JSObject ret = new JSObject();
        ret.put("value", store.getString(key));
        call.resolve(ret);
    }

    @PluginMethod
    public void removeSecret(PluginCall call) {
        String key = call.getString("key");
        if (key == null) { call.reject("Missing key"); return; }
        store.remove(key);
        call.resolve();
    }

    @PluginMethod
    public void clearAll(PluginCall call) {
        store.clear();
        call.resolve();
    }

    @PluginMethod
    public void info(PluginCall call) {
        JSObject ret = new JSObject();
        ret.put("encrypted",       store.isEncrypted());
        ret.put("hardwareBacked",  store.isHardwareBacked());
        call.resolve(ret);
    }

    // ── Biometric gate ───────────────────────────────────────────────

    @PluginMethod
    public void canUseBiometrics(PluginCall call) {
        BiometricManager bm = BiometricManager.from(getContext());
        int allowed = BiometricManager.Authenticators.BIOMETRIC_STRONG
                    | BiometricManager.Authenticators.DEVICE_CREDENTIAL;
        int status = bm.canAuthenticate(allowed);

        JSObject ret = new JSObject();
        switch (status) {
            case BiometricManager.BIOMETRIC_SUCCESS:
                ret.put("available", true);
                break;
            case BiometricManager.BIOMETRIC_ERROR_NO_HARDWARE:
                ret.put("available", false);
                ret.put("reason", "no-hardware");
                break;
            case BiometricManager.BIOMETRIC_ERROR_HW_UNAVAILABLE:
                ret.put("available", false);
                ret.put("reason", "hw-unavailable");
                break;
            case BiometricManager.BIOMETRIC_ERROR_NONE_ENROLLED:
                ret.put("available", false);
                ret.put("reason", "none-enrolled");
                break;
            case BiometricManager.BIOMETRIC_ERROR_SECURITY_UPDATE_REQUIRED:
                ret.put("available", false);
                ret.put("reason", "security-update-required");
                break;
            default:
                ret.put("available", false);
                ret.put("reason", "unknown:" + status);
        }
        call.resolve(ret);
    }

    @PluginMethod
    public void authenticate(PluginCall call) {
        FragmentActivity activity = (FragmentActivity) getActivity();
        if (activity == null) { call.reject("No activity"); return; }

        String title       = call.getString("title",    "Unlock Helen");
        String subtitle    = call.getString("subtitle", "Authenticate to continue");
        String reason      = call.getString("reason",   "");
        boolean allowDevCred = Boolean.TRUE.equals(
            call.getBoolean("allowDeviceCredential", true)
        );

        // BiometricPrompt cannot mix DEVICE_CREDENTIAL with negative button,
        // and can't use DEVICE_CREDENTIAL on Android 9 or older without
        // setNegativeButtonText("..."). Build a robust info object:
        BiometricPrompt.PromptInfo.Builder b = new BiometricPrompt.PromptInfo.Builder()
            .setTitle(title)
            .setSubtitle(subtitle);
        if (!reason.isEmpty()) b.setDescription(reason);

        if (allowDevCred && Build.VERSION.SDK_INT >= Build.VERSION_CODES.R) {
            b.setAllowedAuthenticators(
                BiometricManager.Authenticators.BIOMETRIC_STRONG
              | BiometricManager.Authenticators.DEVICE_CREDENTIAL
            );
            // No negative button when device credential is allowed.
        } else {
            b.setAllowedAuthenticators(BiometricManager.Authenticators.BIOMETRIC_STRONG);
            b.setNegativeButtonText("Cancel");
        }
        BiometricPrompt.PromptInfo info = b.build();

        Executor exec = ContextCompat.getMainExecutor(getContext());

        BiometricPrompt prompt = new BiometricPrompt(
            activity, exec,
            new BiometricPrompt.AuthenticationCallback() {
                @Override
                public void onAuthenticationSucceeded(
                        BiometricPrompt.AuthenticationResult result) {
                    JSObject ret = new JSObject();
                    ret.put("authenticated", true);
                    ret.put("authType", result.getAuthenticationType());
                    call.resolve(ret);
                }

                @Override
                public void onAuthenticationError(int code, CharSequence msg) {
                    JSObject ret = new JSObject();
                    ret.put("authenticated", false);
                    ret.put("errorCode", code);
                    ret.put("errorMessage", msg.toString());
                    call.resolve(ret);
                }

                @Override
                public void onAuthenticationFailed() {
                    // Note: "failed" means the bio reading didn't match,
                    // not that the user cancelled — the prompt stays
                    // visible and the user can try again. We don't resolve
                    // here; we wait for either a success or final error.
                }
            }
        );

        // BiometricPrompt MUST run on the UI thread — the FragmentActivity
        // requirement enforces this at the framework level.
        activity.runOnUiThread(() -> prompt.authenticate(info));
    }
}
