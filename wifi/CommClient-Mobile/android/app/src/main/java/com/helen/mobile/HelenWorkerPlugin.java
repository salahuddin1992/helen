package com.helen.mobile;

import androidx.work.Data;

import com.getcapacitor.JSObject;
import com.getcapacitor.Plugin;
import com.getcapacitor.PluginCall;
import com.getcapacitor.PluginMethod;
import com.getcapacitor.annotation.CapacitorPlugin;

/**
 * Helen — JS bridge for offline send-retry.
 *
 * The renderer queues a message via:
 *
 *   await HelenWorker.queueMessageRetry({
 *       baseUrl:        'http://192.168.1.50:3000',
 *       bearer:         <jwt>,
 *       channelId:      <id>,
 *       content:        '...',
 *       type:           'text',
 *       clientMessageId: <uuid>,
 *   });
 *
 * WorkManager persists the request, fires it as soon as the OS sees
 * connectivity (constraint: NetworkType.CONNECTED), and retries on
 * transient failure with 30s exponential backoff. Permanent (4xx)
 * failures are surfaced as a Result.failure that the renderer can
 * inspect via the WorkInfo listener (not exposed yet — current API
 * is fire-and-forget; the server's eventual `chat:new_message`
 * fanout reconciles state).
 *
 * Survives app process death — the queue lives in WorkManager's
 * internal database, not in our heap.
 */
@CapacitorPlugin(name = "HelenWorker")
public class HelenWorkerPlugin extends Plugin {

    @PluginMethod
    public void queueMessageRetry(PluginCall call) {
        String baseUrl   = call.getString("baseUrl");
        String bearer    = call.getString("bearer");
        String channelId = call.getString("channelId");
        String content   = call.getString("content");
        String type      = call.getString("type", "text");
        String clientId  = call.getString("clientMessageId");

        if (baseUrl == null || channelId == null || content == null) {
            call.reject("baseUrl, channelId and content are required");
            return;
        }
        if (clientId == null) {
            // Without a client-side ID we can't dedupe re-enqueues, so
            // synthesize one. The server uses it for idempotency too.
            clientId = java.util.UUID.randomUUID().toString();
        }

        Data data = new Data.Builder()
            .putString(MessageRetryWorker.INPUT_BASE_URL,   baseUrl)
            .putString(MessageRetryWorker.INPUT_BEARER,     bearer)
            .putString(MessageRetryWorker.INPUT_CHANNEL_ID, channelId)
            .putString(MessageRetryWorker.INPUT_CONTENT,    content)
            .putString(MessageRetryWorker.INPUT_TYPE,       type)
            .putString(MessageRetryWorker.INPUT_CLIENT_ID,  clientId)
            .build();

        MessageRetryWorker.enqueue(getContext(), data, clientId);

        JSObject ret = new JSObject();
        ret.put("queued", true);
        ret.put("clientMessageId", clientId);
        call.resolve(ret);
    }

    @PluginMethod
    public void cancelAllRetries(PluginCall call) {
        MessageRetryWorker.cancelAll(getContext());
        call.resolve();
    }
}
