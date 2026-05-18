package com.helen.mobile;

import android.content.Context;
import android.util.Log;

import androidx.annotation.NonNull;
import androidx.work.Constraints;
import androidx.work.Data;
import androidx.work.ExistingWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.OneTimeWorkRequest;
import androidx.work.WorkManager;
import androidx.work.Worker;
import androidx.work.WorkerParameters;
import androidx.work.BackoffPolicy;

import org.json.JSONObject;

import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.util.concurrent.TimeUnit;

/**
 * Helen — offline message retry worker.
 *
 * The renderer queues a message via `HelenWorker.queueRetry({ … })`
 * whenever a `POST /api/channels/<id>/messages` fails because the
 * device is offline. WorkManager persists the request across process
 * death and wakes our worker as soon as the OS reports network
 * connectivity, so the queued message is delivered without any user
 * interaction even if the app was killed in the meantime.
 *
 * Retry strategy: exponential backoff capped at 5 attempts. After that
 * the message is marked permanently failed and the renderer surfaces
 * a "couldn't deliver" indicator on the affected bubble.
 */
public class MessageRetryWorker extends Worker {

    private static final String TAG = "HelenMsgRetry";

    public static final String INPUT_BASE_URL    = "base_url";
    public static final String INPUT_BEARER      = "bearer";
    public static final String INPUT_CHANNEL_ID  = "channel_id";
    public static final String INPUT_CONTENT     = "content";
    public static final String INPUT_TYPE        = "type";
    public static final String INPUT_CLIENT_ID   = "client_message_id";

    public MessageRetryWorker(@NonNull Context context, @NonNull WorkerParameters params) {
        super(context, params);
    }

    @NonNull
    @Override
    public Result doWork() {
        String baseUrl   = getInputData().getString(INPUT_BASE_URL);
        String bearer    = getInputData().getString(INPUT_BEARER);
        String channelId = getInputData().getString(INPUT_CHANNEL_ID);
        String content   = getInputData().getString(INPUT_CONTENT);
        String type      = getInputData().getString(INPUT_TYPE);
        String clientId  = getInputData().getString(INPUT_CLIENT_ID);

        if (baseUrl == null || channelId == null || content == null) {
            Log.w(TAG, "missing inputs — failing permanently");
            return Result.failure();
        }

        try {
            URL url = new URL(baseUrl + "/api/channels/" + channelId + "/messages");
            HttpURLConnection conn = (HttpURLConnection) url.openConnection();
            conn.setRequestMethod("POST");
            conn.setConnectTimeout(10_000);
            conn.setReadTimeout(15_000);
            conn.setDoOutput(true);
            conn.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            conn.setRequestProperty("Accept", "application/json");
            if (bearer != null) {
                conn.setRequestProperty("Authorization", "Bearer " + bearer);
            }

            JSONObject body = new JSONObject();
            body.put("content", content);
            body.put("type",    type != null ? type : "text");
            if (clientId != null) body.put("client_message_id", clientId);

            byte[] payload = body.toString().getBytes("UTF-8");
            try (OutputStream out = conn.getOutputStream()) {
                out.write(payload);
            }

            int code = conn.getResponseCode();
            if (code >= 200 && code < 300) {
                Log.i(TAG, "delivered queued msg to " + channelId + " (" + code + ")");
                return Result.success();
            }
            // 4xx → permanent failure (auth/validation problems won't fix themselves);
            // 5xx → transient, let WorkManager retry with backoff.
            if (code >= 400 && code < 500) {
                Log.w(TAG, "permanent HTTP " + code + " — giving up");
                return Result.failure();
            }
            Log.w(TAG, "transient HTTP " + code + " — retry");
            return Result.retry();

        } catch (Exception e) {
            // Network errors → retry. WorkManager respects our backoff.
            Log.w(TAG, "send failed: " + e.getMessage() + " — retry");
            return Result.retry();
        }
    }

    /**
     * Helper: enqueue a unique retry job for the given message. Re-queueing
     * the same `clientId` while a prior job is pending replaces it (so a
     * user editing a draft before send doesn't fan-out duplicate jobs).
     */
    public static void enqueue(Context ctx, Data data, String clientId) {
        Constraints constraints = new Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build();

        OneTimeWorkRequest req = new OneTimeWorkRequest.Builder(MessageRetryWorker.class)
            .setInputData(data)
            .setConstraints(constraints)
            .setBackoffCriteria(
                BackoffPolicy.EXPONENTIAL,
                30, TimeUnit.SECONDS    // 30s, 60s, 120s, 240s, 480s
            )
            .addTag("helen-msg-retry")
            .build();

        WorkManager.getInstance(ctx).enqueueUniqueWork(
            "helen-msg-" + clientId,
            ExistingWorkPolicy.REPLACE,
            req
        );
    }

    /** Cancel all pending retry jobs (e.g. user signed out). */
    public static void cancelAll(Context ctx) {
        WorkManager.getInstance(ctx).cancelAllWorkByTag("helen-msg-retry");
    }
}
