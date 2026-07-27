package com.chatgpt2api.server;

import android.content.Context;
import android.net.Uri;
import android.os.Handler;
import android.os.Looper;
import android.webkit.CookieManager;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;

import org.json.JSONObject;

import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;

/**
 * Cloudflare clearance 获取器(供 Python 经 Chaquopy 调用)。
 *
 * 背景: chatgpt.com 受 Cloudflare 防护, 按 TLS/HTTP2 指纹校验客户端,
 * httpx 无指纹伪装 -> 直接 403(managed challenge)。
 * 本类用真实 WebView(Chromium 内核, 指纹/JS 引擎均为真)加载目标页,
 * 等待 CF 自动放行并签发 cf_clearance Cookie, 然后把 Cookie 与 WebView 的
 * 真实 User-Agent 交还 Python, 后续 httpx 请求凭 clearance 通过 CF。
 *
 * 线程模型: fetch() 在 Python 线程阻塞; WebView 创建/轮询/销毁均在主线程。
 */
public final class CfClearanceHelper {
    private static final String TAG = "CfClearance";
    private static final Object FETCH_LOCK = new Object();
    private static final long POLL_INTERVAL_MS = 1000;

    private static volatile Context appContext;

    private CfClearanceHelper() {}

    /** 在 ServerService.onCreate 中调用一次。 */
    public static void init(Context ctx) {
        if (ctx != null) appContext = ctx.getApplicationContext();
    }

    /**
     * Python 调用入口: 用 WebView 加载 url, 等待拿到 cf_clearance。
     *
     * @param url       目标地址(如 https://chatgpt.com/)
     * @param timeoutMs 等待 CF 放行的上限
     * @return JSON 字符串: {"ok":true,"ua":"...","cookie":"..."} 或 {"ok":false,"error":"..."}
     */
    public static String fetch(String url, long timeoutMs) {
        synchronized (FETCH_LOCK) {
            return doFetch(url, timeoutMs);
        }
    }

    private static String doFetch(final String url, long timeoutMs) {
        final Context ctx = appContext;
        if (ctx == null) return error("helper not initialized");
        if (url == null || url.isEmpty()) return error("empty url");

        final Handler main = new Handler(Looper.getMainLooper());
        final CountDownLatch done = new CountDownLatch(1);
        final String[] result = new String[1];
        final WebView[] holder = new WebView[1];

        main.post(() -> {
            try {
                WebView wv = new WebView(ctx);
                holder[0] = wv;
                WebSettings s = wv.getSettings();
                s.setJavaScriptEnabled(true);
                s.setDomStorageEnabled(true);
                s.setAllowFileAccess(false);
                s.setAllowContentAccess(false);
                final String ua = s.getUserAgentString();
                final CookieManager cm = CookieManager.getInstance();
                cm.setAcceptCookie(true);
                // 关键: CookieManager 是应用级共享存储, WebView 销毁后 Cookie 仍保留。
                // 若不清掉, 轮询器第 1 次检查就会读到上一次遗留的(可能已被 CF 拒绝的)
                // cf_clearance 并立即返回, 导致 403 刷新重试永远拿到同一个坏 Cookie。
                clearCookiesForUrl(cm, url);
                final long deadline = System.currentTimeMillis() + timeoutMs;
                LogBus.i(TAG, "WebView 加载 " + url + " , 等待 Cloudflare 放行...");

                wv.setWebViewClient(new WebViewClient());
                final Runnable poller = new Runnable() {
                    @Override
                    public void run() {
                        try {
                            String cookie = cm.getCookie(url);
                            if (cookie != null && cookie.contains("cf_clearance=")) {
                                LogBus.i(TAG, "已取得 cf_clearance");
                                cm.flush();
                                result[0] = ok(ua, cookie);
                                done.countDown();
                                return;
                            }
                            if (System.currentTimeMillis() >= deadline) {
                                LogBus.e(TAG, "等待 cf_clearance 超时(可能要求交互验证或网络被拦截)");
                                result[0] = error("timeout waiting for cf_clearance");
                                done.countDown();
                                return;
                            }
                            main.postDelayed(this, POLL_INTERVAL_MS);
                        } catch (Throwable t) {
                            result[0] = error(String.valueOf(t));
                            done.countDown();
                        }
                    }
                };
                main.postDelayed(poller, POLL_INTERVAL_MS);
                wv.loadUrl(url);
            } catch (Throwable t) {
                LogBus.e(TAG, "WebView 初始化失败: " + t);
                result[0] = error(String.valueOf(t));
                done.countDown();
            }
        });

        boolean finished = false;
        try {
            finished = done.await(timeoutMs + 10000, TimeUnit.MILLISECONDS);
        } catch (InterruptedException e) {
            result[0] = error("interrupted");
        }
        // 无论成败, 销毁 WebView(必须在主线程)
        main.post(() -> {
            try {
                if (holder[0] != null) holder[0].destroy();
            } catch (Throwable ignored) {
            }
        });
        if (!finished && result[0] == null) return error("internal timeout");
        return result[0] != null ? result[0] : error("no result");
    }

    /**
     * 仅使目标站点的 Cookie 过期(CookieManager 没有按域删除 API,
     * removeAllCookies 会误伤 WebActivity 管理面板 127.0.0.1 的登录态)。
     * cf_clearance 按 Domain=.chatgpt.com 签发, 需同时覆盖 host-only 与 domain 两种形态。
     */
    private static void clearCookiesForUrl(CookieManager cm, String url) {
        try {
            String existing = cm.getCookie(url);
            if (existing == null || existing.isEmpty()) return;
            String host = Uri.parse(url).getHost();
            for (String part : existing.split(";")) {
                String name = part.trim();
                int eq = name.indexOf('=');
                if (eq > 0) name = name.substring(0, eq).trim();
                if (name.isEmpty()) continue;
                String expired = name + "=; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/";
                cm.setCookie(url, expired);
                if (host != null && !host.isEmpty()) {
                    cm.setCookie(url, name + "=; Domain=" + host + "; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/");
                    cm.setCookie(url, name + "=; Domain=." + host + "; Expires=Thu, 01 Jan 1970 00:00:00 GMT; Path=/");
                }
            }
            cm.flush();
        } catch (Throwable t) {
            LogBus.e(TAG, "清理目标站 Cookie 失败: " + t);
        }
    }

    private static String ok(String ua, String cookie) {
        try {
            return new JSONObject()
                    .put("ok", true)
                    .put("ua", ua == null ? "" : ua)
                    .put("cookie", cookie == null ? "" : cookie)
                    .toString();
        } catch (Throwable t) {
            return error(String.valueOf(t));
        }
    }

    private static String error(String msg) {
        try {
            return new JSONObject()
                    .put("ok", false)
                    .put("error", msg == null ? "" : msg)
                    .toString();
        } catch (Throwable t) {
            return "{\"ok\":false,\"error\":\"json\"}";
        }
    }
}
