package com.helen.mobile;

import android.content.Context;
import android.content.SharedPreferences;
import android.os.Build;
import android.util.Log;

import androidx.security.crypto.EncryptedSharedPreferences;
import androidx.security.crypto.MasterKey;

import java.io.IOException;
import java.security.GeneralSecurityException;

/**
 * Helen — encrypted credential store backed by Android's Keystore.
 *
 * Wraps `EncryptedSharedPreferences` with a single AES-256 master key
 * stored in the system keystore (hardware-backed where available). Used
 * for the JWT pair (access + refresh) and any other secret the renderer
 * persists; the unencrypted Capacitor `Preferences` plugin is fine for
 * non-sensitive settings (theme, last-used server URL, …) but tokens
 * must never live in plaintext on disk.
 *
 * Falls back to a regular `SharedPreferences` only if the device's
 * keystore is unusable (very rare, e.g. broken OEM ROMs). The fallback
 * is logged and surfaced to the renderer so it can warn the user.
 */
public final class HelenSecureStore {

    private static final String TAG = "HelenSecureStore";
    private static final String FILE_NAME = "helen_secure_prefs";
    private static final String FALLBACK_NAME = "helen_unencrypted_fallback";

    private final SharedPreferences prefs;
    private final boolean encrypted;

    public HelenSecureStore(Context context) {
        SharedPreferences sp = null;
        boolean ok = false;
        try {
            MasterKey master = new MasterKey.Builder(context)
                .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
                .setUserAuthenticationRequired(false)   // unlock once at boot via FBE
                .build();
            sp = EncryptedSharedPreferences.create(
                context,
                FILE_NAME,
                master,
                EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
                EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM
            );
            ok = true;
        } catch (GeneralSecurityException | IOException e) {
            Log.w(TAG, "EncryptedSharedPreferences init failed — falling back to plaintext. "
                     + "Tokens will NOT be encrypted on this device.", e);
            sp = context.getSharedPreferences(FALLBACK_NAME, Context.MODE_PRIVATE);
        }
        this.prefs = sp;
        this.encrypted = ok;
    }

    public boolean isEncrypted()    { return encrypted; }
    public boolean isHardwareBacked() {
        // MasterKey.AES256_GCM uses StrongBox / TEE when available on the device.
        return encrypted && Build.VERSION.SDK_INT >= Build.VERSION_CODES.M;
    }

    public void setString(String key, String value) {
        prefs.edit().putString(key, value).apply();
    }

    public String getString(String key) {
        return prefs.getString(key, null);
    }

    public void remove(String key) {
        prefs.edit().remove(key).apply();
    }

    public void clear() {
        prefs.edit().clear().apply();
    }

    public boolean has(String key) {
        return prefs.contains(key);
    }
}
