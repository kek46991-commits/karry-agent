package com.karry.device;

import android.Manifest;
import android.app.Activity;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.media.projection.MediaProjectionManager;
import android.net.Uri;
import android.os.Build;
import android.os.Bundle;
import android.webkit.CookieManager;
import android.webkit.JavascriptInterface;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

/** Opens the server's /monitor or /companion page; the worker build adds native screen sharing (MediaProjection). */
public class MainActivity extends Activity {
    private static final int SHARE_REQUEST = 41;
    private static final int NOTICE_REQUEST = 42;
    private WebView web;
    private Uri origin;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        origin = Uri.parse(BuildConfig.SERVER_URL);
        web = new WebView(this);
        WebSettings settings = web.getSettings();
        settings.setJavaScriptEnabled(true);
        settings.setDomStorageEnabled(true);
        settings.setAllowFileAccess(false);
        settings.setAllowContentAccess(false);
        settings.setMixedContentMode(WebSettings.MIXED_CONTENT_NEVER_ALLOW);
        settings.setUserAgentString(settings.getUserAgentString() + " KarryDevice/" + (BuildConfig.WORKER ? "worker" : "monitor"));
        CookieManager.getInstance().setAcceptCookie(true);
        web.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri url = request.getUrl();
                if (sameOrigin(url)) return false;
                startActivity(new Intent(Intent.ACTION_VIEW, url));
                return true;
            }

            @Override
            public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
                if (request.isForMainFrame()) showProblem(response.getStatusCode());
            }

            @Override
            public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
                if (request.isForMainFrame()) showProblem(0);
            }
        });
        if (BuildConfig.WORKER) web.addJavascriptInterface(new Bridge(), "KarryNative");
        setContentView(web);
        if (savedInstanceState != null) web.restoreState(savedInstanceState);
        else web.loadUrl(BuildConfig.SERVER_URL + BuildConfig.START_PATH);
    }

    /** Replaces raw server errors (e.g. {"detail":"Not Found"}) with a plain explanation and a retry button. */
    private void showProblem(int status) {
        String message = status == 404
                ? "サーバーで見守り機能がまだ有効になっていません。<br>管理者に「KARRY_EXPERIMENTAL_FEATURES=1」の設定を頼んでください。"
                : status == 0
                ? "インターネットにつながっていないか、サーバーに届きませんでした。<br>電波を確認してください。"
                : "サーバーが起動中か、混み合っています（" + status + "）。<br>1分ほど待ってからもう一度お試しください。";
        String html = "<!doctype html><html lang=ja><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                + "<body style='font-family:sans-serif;padding:32px 20px;line-height:1.7;color:#1f2933'>"
                + "<h2 style='margin-top:0'>" + getString(R.string.app_name) + "</h2><p style='font-size:18px'>" + message + "</p>"
                + "<p><a href='" + BuildConfig.SERVER_URL + BuildConfig.START_PATH + "' style='display:block;text-align:center;padding:16px;"
                + "background:#1f6feb;color:#fff;border-radius:12px;font-size:20px;text-decoration:none'>もう一度試す</a></p>"
                + "<p style='color:#616e7c;font-size:14px'>つなぎ先: " + BuildConfig.SERVER_URL + "</p></body></html>";
        web.loadDataWithBaseURL(null, html, "text/html", "utf-8", null);
    }

    private boolean sameOrigin(Uri url) {
        return "https".equals(url.getScheme()) || "http".equals(url.getScheme())
                ? origin.getHost() != null && origin.getHost().equals(url.getHost()) && origin.getPort() == url.getPort() && origin.getScheme().equals(url.getScheme())
                : false;
    }

    /** Only reachable from pages on the configured server (navigation elsewhere opens the browser). */
    private class Bridge {
        @JavascriptInterface
        public boolean isSharing() { return ScreenShareService.running; }

        @JavascriptInterface
        public void startScreenShare() {
            runOnUiThread(() -> {
                if (!sameOrigin(Uri.parse(web.getUrl()))) return;
                if (Build.VERSION.SDK_INT >= 33 && checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS) != PackageManager.PERMISSION_GRANTED) {
                    requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, NOTICE_REQUEST);
                } else {
                    askCapture();
                }
            });
        }

        @JavascriptInterface
        public void stopScreenShare() {
            runOnUiThread(() -> stopService(new Intent(MainActivity.this, ScreenShareService.class)));
        }
    }

    private void askCapture() {
        MediaProjectionManager manager = getSystemService(MediaProjectionManager.class);
        startActivityForResult(manager.createScreenCaptureIntent(), SHARE_REQUEST);
    }

    @Override
    public void onRequestPermissionsResult(int requestCode, String[] permissions, int[] results) {
        super.onRequestPermissionsResult(requestCode, permissions, results);
        if (requestCode == NOTICE_REQUEST) askCapture();
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode != SHARE_REQUEST) return;
        if (resultCode == RESULT_OK && data != null) {
            CookieManager.getInstance().flush();
            Intent service = new Intent(this, ScreenShareService.class)
                    .putExtra(ScreenShareService.EXTRA_RESULT, resultCode).putExtra(ScreenShareService.EXTRA_DATA, data);
            startForegroundService(service);
        }
        web.postDelayed(this::notifyPage, 1500);
    }

    private void notifyPage() {
        web.evaluateJavascript("window.karryNativeChanged && window.karryNativeChanged()", null);
    }

    @Override
    protected void onResume() {
        super.onResume();
        if (BuildConfig.WORKER && web != null) notifyPage();
    }

    @Override
    protected void onSaveInstanceState(Bundle outState) {
        super.onSaveInstanceState(outState);
        web.saveState(outState);
    }

    @Override
    protected void onPause() {
        super.onPause();
        CookieManager.getInstance().flush();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack(); else super.onBackPressed();
    }
}
