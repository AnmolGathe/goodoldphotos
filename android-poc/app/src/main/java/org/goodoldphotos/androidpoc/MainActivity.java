package org.goodoldphotos.androidpoc;

import android.app.Activity;
import android.content.ActivityNotFoundException;
import android.content.Intent;
import android.content.res.AssetManager;
import android.net.Uri;
import android.os.Bundle;
import android.util.Log;
import android.webkit.CookieManager;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.webkit.WebResourceRequest;
import android.widget.TextView;

import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.io.ByteArrayOutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.security.GeneralSecurityException;
import java.security.SecureRandom;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.atomic.AtomicBoolean;

public class MainActivity extends Activity {
    private static final String TAG = "FastApiRuntimePoc";
    private static final String BASE_URL = "http://127.0.0.1:8765/";
    private static final String HEALTH_URL = BASE_URL + "health";
    private static final String LOCAL_REDEEM_URL = BASE_URL + "auth/android/redeem";
    private static final String RENDER_ANDROID_START_URL =
            "https://mygoodoldphotos.onrender.com/auth/google/android/start";
    private static final int STARTUP_TIMEOUT_MS = 30_000;
    private static final Object SERVER_LOCK = new Object();
    private static boolean serverLaunchRequested;
    private static volatile String serverStartupError;

    private TextView status;
    private WebView webView;
    private volatile boolean activityDestroyed;
    private volatile String handoffStatusMessage;
    private volatile String receivedHandoffCode;
    private volatile String receivedHandoffVerifier;
    private volatile boolean localServerReady;
    private final AtomicBoolean handoffRedemptionInProgress = new AtomicBoolean();

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);

        status = new TextView(this);
        status.setText("Starting embedded Python and FastAPI...");
        status.setPadding(32, 32, 32, 32);
        setContentView(status);

        webView = new WebView(this);
        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri uri = request.getUrl();
                if (isLocalLoginUri(uri)) {
                    beginAndroidLogin();
                    return true;
                }
                return false;
            }

            @Override
            public boolean shouldOverrideUrlLoading(WebView view, String url) {
                Uri uri = Uri.parse(url);
                if (isLocalLoginUri(uri)) {
                    beginAndroidLogin();
                    return true;
                }
                return false;
            }
        });

        handleAppLink(getIntent());
        startServerIfNeeded();
        waitForServerAndLoadPage();
    }

    private static boolean isLocalLoginUri(Uri uri) {
        return "http".equals(uri.getScheme())
                && "127.0.0.1".equals(uri.getHost())
                && uri.getPort() == 8765
                && "/auth/google".equals(uri.getPath());
    }

    private void beginAndroidLogin() {
        boolean pendingCreated = false;
        try {
            AndroidOAuthHandoff.PendingLogin pending = AndroidOAuthHandoff.createAndStore(
                    this,
                    System.currentTimeMillis(),
                    new SecureRandom());
            pendingCreated = true;

            Uri startUri = Uri.parse(RENDER_ANDROID_START_URL)
                    .buildUpon()
                    .appendQueryParameter("code_challenge", pending.challenge)
                    .appendQueryParameter("code_challenge_method", "S256")
                    .build();
            startActivity(new Intent(Intent.ACTION_VIEW, startUri));
        } catch (ActivityNotFoundException exception) {
            clearPendingLoginAfterFailure(pendingCreated);
            showHandoffStatus("No system browser is available to continue sign-in.");
        } catch (GeneralSecurityException | IllegalStateException exception) {
            clearPendingLoginAfterFailure(pendingCreated);
            Log.e(TAG, "Unable to start Android OAuth.", exception);
            showHandoffStatus(exception.getMessage() == null
                    ? "Unable to start sign-in."
                    : exception.getMessage());
        }
    }

    private void clearPendingLoginAfterFailure(boolean pendingCreated) {
        if (!pendingCreated) {
            return;
        }
        try {
            AndroidOAuthHandoff.clear(this);
        } catch (IllegalStateException exception) {
            Log.e(TAG, "Unable to clear failed Android OAuth attempt.", exception);
        }
    }

    private boolean handleAppLink(Intent intent) {
        if (intent == null || intent.getData() == null) {
            return false;
        }
        String url = intent.getData().toString();
        if (!AndroidOAuthHandoff.isCallbackUri(url)) {
            return false;
        }

        String code = AndroidOAuthHandoff.extractHandoffCode(url);
        if (code == null) {
            clearPendingLoginAfterFailureIfPresent();
            showHandoffStatus("The authentication handoff link is invalid.");
            Log.w(TAG, "Android OAuth handoff link was invalid.");
            return true;
        }

        try {
            AndroidOAuthHandoff.PendingLogin pending = AndroidOAuthHandoff.load(
                    this,
                    System.currentTimeMillis());
            if (!AndroidOAuthHandoff.isHandoffAcceptable(
                    url,
                    pending,
                    System.currentTimeMillis())) {
                showHandoffStatus(pending == null
                        ? "No Android sign-in is currently pending."
                        : "The pending Android sign-in has expired.");
                Log.w(TAG, "Android OAuth handoff received without a valid pending login.");
                return true;
            }

            receivedHandoffCode = code;
            receivedHandoffVerifier = pending.verifier;
            handoffStatusMessage = "Android authentication handoff received.";
            Log.i(TAG, "Android OAuth handoff received.");
            showHandoffStatus(handoffStatusMessage);
            redeemHandoffWhenReady();
        } catch (GeneralSecurityException | IllegalStateException exception) {
            Log.e(TAG, "Unable to read the pending Android OAuth attempt.", exception);
            clearPendingLoginAfterFailureIfPresent();
            showHandoffStatus("Unable to verify the Android authentication handoff.");
        }
        return true;
    }

    private void clearPendingLoginAfterFailureIfPresent() {
        try {
            AndroidOAuthHandoff.clear(this);
        } catch (IllegalStateException exception) {
            Log.e(TAG, "Unable to clear Android OAuth state after failure.", exception);
        }
    }

    private void showHandoffStatus(String message) {
        handoffStatusMessage = message;
        runOnUiThread(() -> {
            if (!activityDestroyed) {
                status.setText(message);
                setContentView(status);
            }
        });
    }

    private void redeemHandoffWhenReady() {
        if (!localServerReady
                || receivedHandoffCode == null
                || receivedHandoffVerifier == null
                || !handoffRedemptionInProgress.compareAndSet(false, true)) {
            return;
        }

        String code = receivedHandoffCode;
        String verifier = receivedHandoffVerifier;
        new Thread(() -> redeemHandoff(code, verifier), "android-oauth-redemption").start();
    }

    private void redeemHandoff(String code, String verifier) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(LOCAL_REDEEM_URL).openConnection();
            connection.setRequestMethod("POST");
            connection.setConnectTimeout(10_000);
            connection.setReadTimeout(15_000);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", "application/json; charset=utf-8");
            byte[] body = ("{\"code\":\"" + code
                    + "\",\"code_verifier\":\"" + verifier + "\"}")
                    .getBytes(StandardCharsets.UTF_8);
            connection.setFixedLengthStreamingMode(body.length);
            try (OutputStream output = connection.getOutputStream()) {
                output.write(body);
            }

            int statusCode = connection.getResponseCode();
            String responseBody = statusCode == HttpURLConnection.HTTP_OK
                    ? readResponse(connection)
                    : "";
            String setCookie = connection.getHeaderField("Set-Cookie");
            if (!AndroidOAuthHandoff.isSuccessfulRedemption(
                    statusCode,
                    responseBody,
                    setCookie)) {
                finishRedemptionFailure();
                return;
            }

            installLocalSessionCookie(setCookie);
        } catch (IOException exception) {
            Log.w(TAG, "Local Android sign-in redemption failed.");
            finishRedemptionFailure();
        } finally {
            if (connection != null) {
                connection.disconnect();
            }
        }
    }

    private static String readResponse(HttpURLConnection connection) throws IOException {
        try (InputStream input = connection.getInputStream();
                ByteArrayOutputStream output = new ByteArrayOutputStream()) {
            byte[] buffer = new byte[1024];
            int count;
            while ((count = input.read(buffer)) != -1) {
                output.write(buffer, 0, count);
                if (output.size() > 16 * 1024) {
                    throw new IOException("Local redemption response was too large.");
                }
            }
            return output.toString(StandardCharsets.UTF_8.name());
        }
    }

    private void installLocalSessionCookie(String setCookie) {
        runOnUiThread(() -> {
            CookieManager cookieManager = CookieManager.getInstance();
            cookieManager.setAcceptCookie(true);
            cookieManager.setCookie(BASE_URL, setCookie, accepted -> {
                if (!accepted) {
                    finishRedemptionFailure();
                    return;
                }
                cookieManager.flush();
                finishRedemptionSuccess();
            });
        });
    }

    private void finishRedemptionSuccess() {
        clearPendingRedemption();
        handoffStatusMessage = null;
        Log.i(TAG, "Android sign-in completed.");
        runOnUiThread(() -> {
            if (!activityDestroyed) {
                setContentView(webView);
                webView.loadUrl(BASE_URL);
            }
        });
    }

    private void finishRedemptionFailure() {
        clearPendingRedemption();
        showHandoffStatus("Android sign-in could not be completed. Please try again.");
    }

    private void clearPendingRedemption() {
        receivedHandoffCode = null;
        receivedHandoffVerifier = null;
        try {
            AndroidOAuthHandoff.clear(this);
        } catch (IllegalStateException exception) {
            Log.e(TAG, "Unable to clear completed Android OAuth state.", exception);
        }
        handoffRedemptionInProgress.set(false);
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        setIntent(intent);
        handleAppLink(intent);
    }

    private void startServerIfNeeded() {
        synchronized (SERVER_LOCK) {
            if (serverLaunchRequested) {
                return;
            }
            serverLaunchRequested = true;
            serverStartupError = null;
        }

        Thread serverThread = new Thread(() -> {
            try {
                File webRoot = new File(getFilesDir(), "goodoldphotos-web");
                copyAssetDirectory("templates", new File(webRoot, "templates"));
                copyAssetDirectory("static", new File(webRoot, "static"));

                if (!Python.isStarted()) {
                    Python.start(new AndroidPlatform(getApplicationContext()));
                }
                Python.getInstance()
                        .getModule("server")
                        .callAttr(
                                "start_server",
                                getFilesDir().getAbsolutePath(),
                                getCacheDir().getAbsolutePath(),
                                webRoot.getAbsolutePath());
                recordServerFailure("The FastAPI server stopped unexpectedly.");
            } catch (Exception exception) {
                Log.e(TAG, "Failed to start the embedded FastAPI server.", exception);
                String message = exception.getMessage();
                recordServerFailure(
                        message == null ? exception.getClass().getSimpleName() : message);
            }
        }, "embedded-fastapi");
        serverThread.start();
    }

    private void copyAssetDirectory(String assetPath, File destination) throws IOException {
        AssetManager assets = getAssets();
        String[] children = assets.list(assetPath);
        if (children == null) {
            throw new IOException("Unable to list packaged assets at " + assetPath);
        }

        if (children.length == 0) {
            File parent = destination.getParentFile();
            if (parent == null || (!parent.isDirectory() && !parent.mkdirs())) {
                throw new IOException("Unable to create asset directory for " + destination);
            }
            try (InputStream input = assets.open(assetPath);
                    OutputStream output = new FileOutputStream(destination)) {
                byte[] buffer = new byte[8192];
                int count;
                while ((count = input.read(buffer)) != -1) {
                    output.write(buffer, 0, count);
                }
            }
            return;
        }

        if (!destination.isDirectory() && !destination.mkdirs()) {
            throw new IOException("Unable to create asset directory " + destination);
        }
        for (String child : children) {
            copyAssetDirectory(assetPath + "/" + child, new File(destination, child));
        }
    }

    private static void recordServerFailure(String message) {
        synchronized (SERVER_LOCK) {
            serverStartupError = message;
            serverLaunchRequested = false;
        }
    }

    private void waitForServerAndLoadPage() {
        Thread readinessThread = new Thread(() -> {
            long deadline = System.currentTimeMillis() + STARTUP_TIMEOUT_MS;
            String lastFailure = "The health endpoint did not respond.";

            while (!activityDestroyed && System.currentTimeMillis() < deadline) {
                String startupError = serverStartupError;
                if (startupError != null) {
                    showStartupFailure("FastAPI startup failed: " + startupError);
                    return;
                }

                HttpURLConnection connection = null;
                try {
                    connection = (HttpURLConnection) new URL(HEALTH_URL).openConnection();
                    connection.setRequestMethod("GET");
                    connection.setConnectTimeout(500);
                    connection.setReadTimeout(500);
                    int responseCode = connection.getResponseCode();
                    if (responseCode == HttpURLConnection.HTTP_OK) {
                        localServerReady = true;
                        if (receivedHandoffCode != null) {
                            redeemHandoffWhenReady();
                            return;
                        }
                        runOnUiThread(() -> {
                            if (!activityDestroyed) {
                                if (handoffStatusMessage != null) {
                                    status.setText(handoffStatusMessage);
                                    setContentView(status);
                                } else {
                                    setContentView(webView);
                                    webView.loadUrl(BASE_URL);
                                }
                            }
                        });
                        return;
                    }
                    lastFailure = "Health endpoint returned HTTP " + responseCode + ".";
                } catch (IOException exception) {
                    lastFailure = exception.getMessage() == null
                            ? exception.getClass().getSimpleName()
                            : exception.getMessage();
                } finally {
                    if (connection != null) {
                        connection.disconnect();
                    }
                }

                try {
                    Thread.sleep(200);
                } catch (InterruptedException exception) {
                    Thread.currentThread().interrupt();
                    return;
                }
            }

            if (!activityDestroyed) {
                showStartupFailure(
                        "FastAPI did not become ready within "
                                + (STARTUP_TIMEOUT_MS / 1000)
                                + " seconds. "
                                + lastFailure);
            }
        }, "fastapi-readiness");
        readinessThread.start();
    }

    private void showStartupFailure(String message) {
        Log.e(TAG, message);
        if (receivedHandoffCode != null) {
            finishRedemptionFailure();
        }
        runOnUiThread(() -> {
            if (!activityDestroyed) {
                status.setText(message);
            }
        });
    }

    @Override
    protected void onDestroy() {
        activityDestroyed = true;
        if (webView != null) {
            webView.destroy();
        }
        super.onDestroy();
    }
}
