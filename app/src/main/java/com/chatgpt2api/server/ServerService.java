package com.chatgpt2api.server;

import android.app.Notification;
import android.app.NotificationChannel;
import android.app.NotificationManager;
import android.app.PendingIntent;
import android.app.Service;
import android.content.Intent;
import android.content.pm.ServiceInfo;
import android.content.res.AssetManager;
import android.os.Build;
import android.os.IBinder;
import android.os.PowerManager;

import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;

import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.util.zip.ZipEntry;
import java.util.zip.ZipInputStream;

/**
 * 前台服务: 在独立线程里运行内嵌 Python(uvicorn)提供 ChatGPT2API 后端。
 *
 * 状态机: STOPPED -> STARTING(解压/初始化) -> RUNNING -> STOPPING -> STOPPED
 * 任何阶段异常都会进入日志并将状态置为 ERROR。
 */
public class ServerService extends Service {
    public static final String ACTION_START = "com.chatgpt2api.server.START";
    public static final String ACTION_STOP = "com.chatgpt2api.server.STOP";
    public static final int PORT = 3000;

    public static final int STATE_STOPPED = 0;
    public static final int STATE_STARTING = 1;
    public static final int STATE_RUNNING = 2;
    public static final int STATE_ERROR = 3;

    private static final String CHANNEL_ID = "server";
    private static final int NOTIFICATION_ID = 1;
    private static final String TAG = "ServerService";

    private static volatile int state = STATE_STOPPED;
    private static volatile String lastError = "";
    private static volatile Runnable stateListener;

    private Thread serverThread;
    private PowerManager.WakeLock wakeLock;
    private PyObject bootstrap;

    public static int getState() {
        return state;
    }

    public static String getLastError() {
        return lastError;
    }

    public static void setStateListener(Runnable r) {
        stateListener = r;
    }

    private static void setState(int s, String err) {
        state = s;
        lastError = err == null ? "" : err;
        Runnable r = stateListener;
        if (r != null) r.run();
    }

    @Override
    public IBinder onBind(Intent intent) {
        return null;
    }

    @Override
    public void onCreate() {
        super.onCreate();
        LogBus.init(getFilesDir());
        CfClearanceHelper.init(getApplicationContext());
        createChannel();
    }

    @Override
    public int onStartCommand(Intent intent, int flags, int startId) {
        if (intent != null && ACTION_STOP.equals(intent.getAction())) {
            stopServer();
            return START_NOT_STICKY;
        }
        if (state == STATE_RUNNING || state == STATE_STARTING) {
            LogBus.i(TAG, "服务已在运行或正在启动, 忽略启动请求");
            return START_STICKY;
        }
        startServer();
        return START_STICKY;
    }

    // ------------------------------------------------------------------
    private void startServer() {
        setState(STATE_STARTING, "");
        startForegroundCompat("ChatGPT2API 服务启动中...");
        acquireWakeLock();
        serverThread = new Thread(this::serverMain, "chatgpt2api-server");
        serverThread.start();
    }

    private void serverMain() {
        try {
            File backendDir = ensureBackendExtracted();
            LogBus.i(TAG, "后端目录就绪: " + backendDir.getAbsolutePath());

            if (!Python.isStarted()) {
                LogBus.i(TAG, "正在初始化 Python 运行时...");
                Python.start(new AndroidPlatform(this));
            }
            Python py = Python.getInstance();
            bootstrap = py.getModule("bootstrap");

            LogBus.i(TAG, "启动 HTTP 服务, 端口 " + PORT + " ...");
            updateNotification("ChatGPT2API 运行中 · 端口 " + PORT);
            setState(STATE_RUNNING, "");
            // 阻塞调用: 直到服务退出才返回
            PyObject result = bootstrap.callAttr("start", backendDir.getAbsolutePath(), PORT);
            int code = result == null ? -1 : result.toInt();
            LogBus.i(TAG, "服务进程退出, code=" + code);
            if (code != 0 && state != STATE_STOPPED) {
                setState(STATE_ERROR, "服务异常退出 code=" + code);
            }
        } catch (Throwable t) {
            LogBus.e(TAG, "服务启动失败: " + t);
            setState(STATE_ERROR, String.valueOf(t));
        } finally {
            bootstrap = null;
            releaseWakeLock();
            if (state != STATE_ERROR) setState(STATE_STOPPED, "");
            updateNotification("ChatGPT2API 已停止");
            stopSelf();
        }
    }

    private void stopServer() {
        LogBus.i(TAG, "正在停止服务...");
        final PyObject bs = bootstrap;
        if (bs != null) {
            new Thread(() -> {
                try {
                    bs.callAttr("stop");
                } catch (Throwable t) {
                    LogBus.e(TAG, "停止调用失败: " + t);
                }
            }, "chatgpt2api-stopper").start();
        } else {
            LogBus.i(TAG, "服务未在运行");
        }
    }

    // ------------------------------------------------------------------
    /** 解压 assets/backend.zip 到 filesDir/backend-<版本>, 已存在则跳过。 */
    private File ensureBackendExtracted() throws Exception {
        String stamp = BuildConfig.BACKEND_STAMP;
        File base = new File(getFilesDir(), "backend-" + stamp);
        File marker = new File(base, ".extract_ok");
        if (marker.exists()) {
            LogBus.i(TAG, "后端已解压, 跳过 (版本 " + stamp + ")");
            return base;
        }
        LogBus.i(TAG, "首次运行或版本更新, 正在解压后端资源 (版本 " + stamp + ")...");
        long t0 = System.currentTimeMillis();
        deleteRecursively(base);
        //noinspection ResultOfMethodCallIgnored
        base.mkdirs();

        AssetManager am = getAssets();
        int count = 0;
        try (InputStream is = am.open("backend.zip");
             ZipInputStream zis = new ZipInputStream(is)) {
            byte[] buf = new byte[64 * 1024];
            ZipEntry entry;
            while ((entry = zis.getNextEntry()) != null) {
                File out = new File(base, entry.getName());
                if (entry.isDirectory()) {
                    //noinspection ResultOfMethodCallIgnored
                    out.mkdirs();
                    continue;
                }
                File parent = out.getParentFile();
                if (parent != null) {
                    //noinspection ResultOfMethodCallIgnored
                    parent.mkdirs();
                }
                try (FileOutputStream fos = new FileOutputStream(out)) {
                    int n;
                    while ((n = zis.read(buf)) > 0) fos.write(buf, 0, n);
                }
                count++;
            }
        }
        //noinspection ResultOfMethodCallIgnored
        marker.createNewFile();
        LogBus.i(TAG, "解压完成: " + count + " 个文件, 耗时 " +
                (System.currentTimeMillis() - t0) + "ms");
        return base;
    }

    private static void deleteRecursively(File f) {
        if (f == null || !f.exists()) return;
        if (f.isDirectory()) {
            File[] kids = f.listFiles();
            if (kids != null) for (File k : kids) deleteRecursively(k);
        }
        //noinspection ResultOfMethodCallIgnored
        f.delete();
    }

    // ------------------------------------------------------------------
    private void acquireWakeLock() {
        if (wakeLock != null) return;
        PowerManager pm = (PowerManager) getSystemService(POWER_SERVICE);
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "chatgpt2api:server");
        wakeLock.acquire();
    }

    private void releaseWakeLock() {
        if (wakeLock != null) {
            try {
                wakeLock.release();
            } catch (Throwable ignored) {
            }
            wakeLock = null;
        }
    }

    private void createChannel() {
        NotificationManager nm = getSystemService(NotificationManager.class);
        NotificationChannel ch = new NotificationChannel(
                CHANNEL_ID, "ChatGPT2API 服务", NotificationManager.IMPORTANCE_LOW);
        nm.createNotificationChannel(ch);
    }

    private Notification buildNotification(String text) {
        Intent open = new Intent(this, MainActivity.class);
        PendingIntent pi = PendingIntent.getActivity(
                this, 0, open, PendingIntent.FLAG_IMMUTABLE);
        return new Notification.Builder(this, CHANNEL_ID)
                .setContentTitle("ChatGPT2API")
                .setContentText(text)
                .setSmallIcon(android.R.drawable.stat_sys_upload_done)
                .setContentIntent(pi)
                .setOngoing(true)
                .build();
    }

    private void startForegroundCompat(String text) {
        Notification n = buildNotification(text);
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else if (Build.VERSION.SDK_INT >= 29) {
            startForeground(NOTIFICATION_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC);
        } else {
            startForeground(NOTIFICATION_ID, n);
        }
    }

    private void updateNotification(String text) {
        NotificationManager nm = getSystemService(NotificationManager.class);
        nm.notify(NOTIFICATION_ID, buildNotification(text));
    }

    @Override
    public void onDestroy() {
        stopServer();
        super.onDestroy();
    }
}
