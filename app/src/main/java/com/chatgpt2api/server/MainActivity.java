package com.chatgpt2api.server;

import android.Manifest;
import android.app.Activity;
import android.content.ClipData;
import android.content.ClipboardManager;
import android.content.Intent;
import android.content.pm.PackageManager;
import android.os.Build;
import android.os.Bundle;
import android.view.View;
import android.widget.Button;
import android.widget.ScrollView;
import android.widget.TextView;
import android.widget.Toast;

import java.net.NetworkInterface;
import java.util.Collections;

/**
 * 主界面: 启动/停止服务按钮 + 打开网页按钮 + 实时日志视图。
 */
public class MainActivity extends Activity {
    private TextView statusText;
    private TextView addressText;
    private TextView logText;
    private ScrollView logScroll;
    private Button toggleButton;
    private Button webButton;

    private final LogBus.Listener logListener = line -> {
        logText.append(line + "\n");
        // 用户长按进入文本选择状态时不要自动滚动, 避免打断复制
        if (!hasActiveSelection()) {
            scrollToBottomIfNeeded();
        }
    };

    private final Runnable stateRefresher = this::runOnUiThreadSafely;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        setContentView(R.layout.activity_main);
        LogBus.init(getFilesDir());

        statusText = findViewById(R.id.statusText);
        addressText = findViewById(R.id.addressText);
        logText = findViewById(R.id.logText);
        logScroll = findViewById(R.id.logScroll);
        toggleButton = findViewById(R.id.toggleButton);
        webButton = findViewById(R.id.webButton);

        // 注意: 不要给 logText 设 ScrollingMovementMethod —— 滚动由外层 ScrollView 负责,
        // 否则 textIsSelectable 的长按选择/复制会被 TextView 自身的滚动处理吃掉。
        toggleButton.setOnClickListener(v -> onToggleClicked());
        webButton.setOnClickListener(v -> onOpenWebClicked());

        findViewById(R.id.clearLogButton).setOnClickListener(v -> logText.setText(""));
        findViewById(R.id.copyLogButton).setOnClickListener(v -> copyAllLogs());

        requestNotificationPermission();
    }

    @Override
    protected void onResume() {
        super.onResume();
        LogBus.setListener(logListener);
        ServerService.setStateListener(stateRefresher);
        logText.setText(LogBus.snapshot());
        scrollToBottomNow();
        refreshState();
    }

    @Override
    protected void onPause() {
        LogBus.setListener(null);
        ServerService.setStateListener(null);
        super.onPause();
    }

    // ------------------------------------------------------------------
    private void onToggleClicked() {
        int state = ServerService.getState();
        if (state == ServerService.STATE_RUNNING || state == ServerService.STATE_STARTING) {
            Intent i = new Intent(this, ServerService.class).setAction(ServerService.ACTION_STOP);
            startService(i);
            LogBus.i("UI", "已请求停止服务");
        } else {
            Intent i = new Intent(this, ServerService.class).setAction(ServerService.ACTION_START);
            if (Build.VERSION.SDK_INT >= 26) {
                startForegroundService(i);
            } else {
                startService(i);
            }
            LogBus.i("UI", "已请求启动服务");
        }
        refreshState();
    }

    private void onOpenWebClicked() {
        if (ServerService.getState() != ServerService.STATE_RUNNING) {
            Toast.makeText(this, "服务未运行, 请先启动服务", Toast.LENGTH_SHORT).show();
            return;
        }
        Intent i = new Intent(this, WebActivity.class);
        i.putExtra(WebActivity.EXTRA_URL, "http://127.0.0.1:" + ServerService.PORT + "/");
        startActivity(i);
    }

    // ------------------------------------------------------------------
    private void runOnUiThreadSafely() {
        runOnUiThread(this::refreshState);
    }

    private void refreshState() {
        int state = ServerService.getState();
        String status;
        boolean toggleEnabled = true;
        String toggleText;
        switch (state) {
            case ServerService.STATE_STARTING:
                status = "状态: 启动中...";
                toggleText = "停止服务";
                break;
            case ServerService.STATE_RUNNING:
                status = "状态: 运行中";
                toggleText = "停止服务";
                break;
            case ServerService.STATE_ERROR:
                status = "状态: 错误 - " + ServerService.getLastError();
                toggleText = "启动服务";
                break;
            default:
                status = "状态: 已停止";
                toggleText = "启动服务";
                break;
        }
        statusText.setText(status);
        toggleButton.setText(toggleText);
        toggleButton.setEnabled(toggleEnabled);
        webButton.setEnabled(state == ServerService.STATE_RUNNING);

        String lan = getLanAddress();
        if (state == ServerService.STATE_RUNNING) {
            addressText.setText("本机: http://127.0.0.1:" + ServerService.PORT +
                    (lan.isEmpty() ? "" : "\n局域网: http://" + lan + ":" + ServerService.PORT));
        } else {
            addressText.setText("");
        }
    }

    private static String getLanAddress() {
        try {
            for (NetworkInterface ni : Collections.list(NetworkInterface.getNetworkInterfaces())) {
                if (!ni.isUp() || ni.isLoopback()) continue;
                for (java.net.InetAddress addr : Collections.list(ni.getInetAddresses())) {
                    if (addr.isLoopbackAddress()) continue;
                    String host = addr.getHostAddress();
                    if (host != null && host.indexOf(':') < 0) return host;
                }
            }
        } catch (Throwable ignored) {
        }
        return "";
    }

    private void scrollToBottomIfNeeded() {
        int scrollY = logScroll.getScrollY();
        int childH = logScroll.getChildAt(0) == null ? 0 : logScroll.getChildAt(0).getHeight();
        boolean nearBottom = childH - (scrollY + logScroll.getHeight()) < 400;
        if (nearBottom) scrollToBottomNow();
    }

    private boolean hasActiveSelection() {
        try {
            int start = logText.getSelectionStart();
            int end = logText.getSelectionEnd();
            return start >= 0 && end > start;
        } catch (Throwable ignored) {
            return false;
        }
    }

    private void copyAllLogs() {
        String text = logText.getText() == null ? "" : logText.getText().toString();
        if (text.isEmpty()) {
            Toast.makeText(this, "暂无日志", Toast.LENGTH_SHORT).show();
            return;
        }
        ClipboardManager cm = (ClipboardManager) getSystemService(CLIPBOARD_SERVICE);
        cm.setPrimaryClip(ClipData.newPlainText("ChatGPT2API 日志", text));
        Toast.makeText(this, "已复制全部日志(" + text.length() + " 字符)", Toast.LENGTH_SHORT).show();
    }

    private void scrollToBottomNow() {
        logScroll.post(() -> logScroll.fullScroll(View.FOCUS_DOWN));
    }

    private void requestNotificationPermission() {
        if (Build.VERSION.SDK_INT >= 33) {
            if (checkSelfPermission(Manifest.permission.POST_NOTIFICATIONS)
                    != PackageManager.PERMISSION_GRANTED) {
                requestPermissions(new String[]{Manifest.permission.POST_NOTIFICATIONS}, 1);
            }
        }
    }
}
