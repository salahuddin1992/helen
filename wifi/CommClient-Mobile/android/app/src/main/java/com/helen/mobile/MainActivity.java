package com.helen.mobile;

import android.os.Bundle;
import android.webkit.PermissionRequest;
import android.webkit.WebChromeClient;
import android.webkit.WebSettings;

import com.getcapacitor.BridgeActivity;
import java.util.ArrayList;

/**
 * Helen Mobile entry point.
 *
 * Two responsibilities beyond Capacitor's defaults:
 *
 *  1. Grant WebRTC permissions to the rendered HTML. Android's
 *     default WebView denies getUserMedia / getDisplayMedia even
 *     when the manifest already grants RECORD_AUDIO / CAMERA at the
 *     OS level. We install a custom WebChromeClient that auto-grants
 *     mic + camera so the renderer's call flow works the same as on
 *     desktop.
 *
 *  2. Enable WebRTC-friendly WebView settings: media playback without
 *     user gesture (the desktop renderer auto-plays remote audio
 *     once the call connects), DOM storage (used by zustand
 *     persistence), and mixed-content for cleartext LAN traffic.
 */
public class MainActivity extends BridgeActivity {

    @Override
    public void onCreate(Bundle savedInstanceState) {
        // Register our native bridge plugins BEFORE Capacitor finishes wiring
        // up its bridge so the renderer's `registerPlugin(...)` calls can
        // find them on first touch.
        //
        //   HelenCall       — foreground service + heads-up call notifications
        //   HelenSecure     — encrypted token store + biometric gate
        //   HelenWorker     — offline message retry via WorkManager
        //   HelenConnection — self-managed Telecom ConnectionService (API 26+)
        ArrayList<Class<? extends com.getcapacitor.Plugin>> plugins = new ArrayList<>();
        plugins.add(HelenCallPlugin.class);
        plugins.add(HelenSecurePlugin.class);
        plugins.add(HelenWorkerPlugin.class);
        plugins.add(HelenConnectionPlugin.class);
        registerPlugins(plugins);

        super.onCreate(savedInstanceState);

        // Tighten / relax WebView settings for our use-case.
        if (this.bridge != null && this.bridge.getWebView() != null) {
            WebSettings settings = this.bridge.getWebView().getSettings();
            settings.setMediaPlaybackRequiresUserGesture(false);
            settings.setDomStorageEnabled(true);
            settings.setJavaScriptEnabled(true);
            settings.setAllowFileAccess(true);
            settings.setAllowContentAccess(true);
            settings.setMixedContentMode(WebSettings.MIXED_CONTENT_ALWAYS_ALLOW);

            // Auto-grant WebRTC permission requests. The renderer can
            // only ask for resources the manifest already declares,
            // so this is gated by the OS-level permission grant the
            // user already approved at first launch.
            this.bridge.getWebView().setWebChromeClient(new WebChromeClient() {
                @Override
                public void onPermissionRequest(final PermissionRequest request) {
                    runOnUiThread(() -> request.grant(request.getResources()));
                }
            });
        }
    }
}
