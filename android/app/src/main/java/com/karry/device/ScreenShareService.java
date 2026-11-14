package com.karry.device;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.graphics.Bitmap;
import android.graphics.PixelFormat;
import android.hardware.display.DisplayManager;
import android.hardware.display.VirtualDisplay;
import android.media.Image;
import android.media.ImageReader;
import android.media.projection.MediaProjection;
import android.media.projection.MediaProjectionManager;
import android.os.BatteryManager;
import android.os.Build;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.IBinder;
import android.util.DisplayMetrics;
import android.webkit.CookieManager;

import java.io.ByteArrayOutputStream;
import java.io.OutputStream;
import java.net.HttpURLConnection;
import java.net.URL;
import java.nio.ByteBuffer;
import java.nio.charset.StandardCharsets;

/**
 * Sends a low-resolution JPEG of the screen to the monitor every few seconds while the user has granted
 * MediaProjection. A persistent notification shows sharing is on and offers a one-tap stop.
 */
public class ScreenShareService extends Service {
    static final String EXTRA_RESULT = "result";
    static final String EXTRA_DATA = "data";
    static final String ACTION_STOP = "com.karry.device.STOP_SHARE";
    static volatile boolean running;
    private static final String CHANNEL = "share";
    private static final int MAX_WIDTH = 720;
    private static final long INTERVAL_MS = 3000;

    private MediaProjection projection;
    private VirtualDisplay display;
    private ImageReader reader;
    private HandlerThread thread;
    private Handler handler;
    private byte[] lastFrame;

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent == null || ACTION_STOP.equals(intent.getAction())) {
            stopSelf();
            return START_NOT_STICKY;
        }
        startForegroundNotice();
        MediaProjectionManager manager = getSystemService(MediaProjectionManager.class);
        Intent data = Build.VERSION.SDK_INT >= 33 ? intent.getParcelableExtra(EXTRA_DATA, Intent.class) : intent.getParcelableExtra(EXTRA_DATA);
        projection = manager.getMediaProjection(intent.getIntExtra(EXTRA_RESULT, 0), data);
        if (projection == null) {
            stopSelf();
            return START_NOT_STICKY;
        }
        thread = new HandlerThread("karry-share");
        thread.start();
        handler = new Handler(thread.getLooper());
        projection.registerCallback(new MediaProjection.Callback() {
            @Override
            public void onStop() { stopSelf(); }
        }, handler);
        DisplayMetrics metrics = getResources().getDisplayMetrics();
        float scale = Math.min(1f, (float) MAX_WIDTH / metrics.widthPixels);
        int width = Math.round(metrics.widthPixels * scale), height = Math.round(metrics.heightPixels * scale);
        reader = ImageReader.newInstance(width, height, PixelFormat.RGBA_8888, 2);
        display = projection.createVirtualDisplay("karry-share", width, height, metrics.densityDpi,
                DisplayManager.VIRTUAL_DISPLAY_FLAG_AUTO_MIRROR, reader.getSurface(), null, handler);
        running = true;
        handler.post(() -> heartbeat(true));
        handler.postDelayed(this::tick, 800);
        return START_NOT_STICKY;
    }

    private void startForegroundNotice() {
        NotificationManager notifications = getSystemService(NotificationManager.class);
        notifications.createNotificationChannel(new NotificationChannel(CHANNEL, "画面共有", NotificationManager.IMPORTANCE_LOW));
        PendingIntent stop = PendingIntent.getService(this, 0, new Intent(this, ScreenShareService.class).setAction(ACTION_STOP),
                PendingIntent.FLAG_IMMUTABLE);
        Notification notice = new Notification.Builder(this, CHANNEL)
                .setSmallIcon(android.R.drawable.ic_menu_view)
                .setContentTitle("画面を監視用に共有中")
                .setContentText("タップで共有をやめます")
                .setContentIntent(stop)
                .addAction(new Notification.Action.Builder(null, "共有をやめる", stop).build())
                .setOngoing(true)
                .build();
        if (Build.VERSION.SDK_INT >= 29) startForeground(1, notice, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PROJECTION);
        else startForeground(1, notice);
    }

    private void tick() {
        if (!running) return;
        byte[] jpeg = capture();
        if (jpeg != null) lastFrame = jpeg;
        else jpeg = lastFrame;
        if (jpeg != null) send("PUT", "/api/autonomy/device/frame", "image/jpeg", jpeg);
        handler.postDelayed(this::tick, INTERVAL_MS);
    }

    private byte[] capture() {
        Image image = reader.acquireLatestImage();
        if (image == null) return null;
        try {
            Image.Plane plane = image.getPlanes()[0];
            ByteBuffer buffer = plane.getBuffer();
            int rowPadding = plane.getRowStride() - plane.getPixelStride() * image.getWidth();
            Bitmap padded = Bitmap.createBitmap(image.getWidth() + rowPadding / plane.getPixelStride(), image.getHeight(), Bitmap.Config.ARGB_8888);
            padded.copyPixelsFromBuffer(buffer);
            Bitmap frame = Bitmap.createBitmap(padded, 0, 0, image.getWidth(), image.getHeight());
            ByteArrayOutputStream out = new ByteArrayOutputStream();
            frame.compress(Bitmap.CompressFormat.JPEG, 60, out);
            padded.recycle();
            frame.recycle();
            return out.toByteArray();
        } finally {
            image.close();
        }
    }

    private void heartbeat(boolean sharing) {
        BatteryManager battery = getSystemService(BatteryManager.class);
        String body = "{\"sharing\":" + sharing + ",\"visible\":true,\"app\":\"作業用アプリ\",\"platform\":\"Android "
                + Build.VERSION.RELEASE + "\",\"battery\":" + battery.getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)
                + ",\"charging\":" + battery.isCharging() + "}";
        send("POST", "/api/autonomy/device/heartbeat", "application/json", body.getBytes(StandardCharsets.UTF_8));
    }

    private static String deviceToken() {
        String cookies = CookieManager.getInstance().getCookie(BuildConfig.SERVER_URL + "/api/autonomy/");
        if (cookies == null) return null;
        for (String part : cookies.split(";")) {
            String[] pair = part.trim().split("=", 2);
            if (pair.length == 2 && pair[0].equals("karry_device")) return pair[1];
        }
        return null;
    }

    private void send(String method, String path, String type, byte[] body) {
        String token = deviceToken();
        if (token == null) return;
        try {
            HttpURLConnection connection = (HttpURLConnection) new URL(BuildConfig.SERVER_URL + path).openConnection();
            connection.setRequestMethod(method);
            connection.setConnectTimeout(8000);
            connection.setReadTimeout(8000);
            connection.setInstanceFollowRedirects(false);
            connection.setDoOutput(true);
            connection.setRequestProperty("Content-Type", type);
            connection.setRequestProperty("X-Device-Token", token);
            try (OutputStream out = connection.getOutputStream()) {
                out.write(body);
            }
            int status = connection.getResponseCode();
            connection.disconnect();
            if (status == 401) running = false;
        } catch (Exception ignored) {
            // Network hiccups: the next tick retries; the monitor shows the device as offline meanwhile.
        }
    }

    @Override
    public void onDestroy() {
        boolean wasRunning = running;
        running = false;
        if (display != null) display.release();
        if (reader != null) reader.close();
        if (projection != null) projection.stop();
        if (handler != null) {
            if (wasRunning) handler.post(() -> heartbeat(false));
            thread.quitSafely();
        }
        super.onDestroy();
    }

    @Override
    public IBinder onBind(Intent intent) { return null; }
}
