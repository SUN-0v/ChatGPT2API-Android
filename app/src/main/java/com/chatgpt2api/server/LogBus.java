package com.chatgpt2api.server;

import android.os.Handler;
import android.os.Looper;

import java.io.BufferedWriter;
import java.io.File;
import java.io.FileWriter;
import java.text.SimpleDateFormat;
import java.util.ArrayDeque;
import java.util.Date;
import java.util.Locale;

/**
 * 全局日志总线: 收集 Java/Python 双侧日志, 分发给界面, 同时落盘到 files/logs/server.log。
 */
public final class LogBus {
    public interface Listener {
        void onNewLine(String line);
    }

    private static final int MAX_LINES = 3000;
    private static final long MAX_LOG_FILE = 2 * 1024 * 1024; // 2MB 滚动
    private static final SimpleDateFormat TS =
            new SimpleDateFormat("MM-dd HH:mm:ss.SSS", Locale.US);

    private static final ArrayDeque<String> buffer = new ArrayDeque<>();
    private static final Handler mainHandler = new Handler(Looper.getMainLooper());
    private static volatile Listener listener;
    private static volatile File logFile;
    private static volatile BufferedWriter writer;

    private LogBus() {}

    public static synchronized void init(File filesDir) {
        if (logFile != null) return;
        File dir = new File(filesDir, "logs");
        //noinspection ResultOfMethodCallIgnored
        dir.mkdirs();
        logFile = new File(dir, "server.log");
        openWriter();
    }

    private static void openWriter() {
        try {
            writer = new BufferedWriter(new FileWriter(logFile, true));
        } catch (Exception e) {
            writer = null;
        }
    }

    public static void setListener(Listener l) {
        listener = l;
    }

    /** 返回当前缓存的全部日志(用于界面首次填充)。 */
    public static synchronized String snapshot() {
        StringBuilder sb = new StringBuilder();
        for (String line : buffer) sb.append(line).append('\n');
        return sb.toString();
    }

    public static void i(String tag, String msg) {
        post("I/" + tag, msg);
    }

    public static void e(String tag, String msg) {
        post("E/" + tag, msg);
    }

    /** Python 侧 sys.stdout/stderr 的重定向终点。 */
    public static void fromPython(String line) {
        post("PY", line);
    }

    private static void post(String tag, String msg) {
        if (msg == null) msg = "";
        String[] parts = msg.split("\n", -1);
        for (String part : parts) {
            if (part.isEmpty()) continue;
            String line = TS.format(new Date()) + " " + tag + ": " + part;
            append(line);
        }
    }

    private static synchronized void append(String line) {
        buffer.addLast(line);
        while (buffer.size() > MAX_LINES) buffer.removeFirst();
        writeFile(line);
        Listener l = listener;
        if (l != null) mainHandler.post(() -> {
            Listener cur = listener;
            if (cur != null) cur.onNewLine(line);
        });
    }

    private static void writeFile(String line) {
        try {
            if (writer == null) return;
            if (logFile.length() > MAX_LOG_FILE) {
                writer.close();
                File old = new File(logFile.getParentFile(), "server.log.1");
                //noinspection ResultOfMethodCallIgnored
                logFile.renameTo(old);
                openWriter();
                if (writer == null) return;
            }
            writer.write(line);
            writer.newLine();
            writer.flush();
        } catch (Exception ignored) {
        }
    }
}
