# ChatGPT2API Android 移植

将 [RemotePinee/ChatGPT2API](https://github.com/RemotePinee/ChatGPT2API)(Python FastAPI 后端 + Next.js 前端)移植为单 APK,在手机上直接运行服务端。

## 界面与功能

主界面严格保持极简:

- **启动服务 / 停止服务** 按钮(单键切换,带前台服务保活)
- **打开网页界面** 按钮(内嵌 WebView 打开 `http://127.0.0.1:3000`,即原版管理面板)
- **实时运行日志**:Java 与 Python 双侧日志逐行滚动显示(时间戳 + 来源标签),自动跟随滚动,同时落盘到 `Android/data/com.chatgpt2api.server/files/logs/server.log`(2MB 滚动),便于排查问题
- 状态栏显示运行状态与 本机/局域网 访问地址

端口固定 `3000`。默认密钥与桌面版一致(`config.json` 初始为 `chatgpt2api`),可在网页"设置"中修改;所有数据(账号、配置、图片、日志)持久化在应用私有目录,重启不丢。

## 架构

```
┌─ APK ────────────────────────────────────────────────┐
│ Java 层                                              │
│   MainActivity    状态/按钮/日志视图                  │
│   ServerService   前台服务:解压后端 -> 启动 Python    │
│   WebActivity     内嵌 WebView                        │
│   LogBus          日志总线(环形缓冲 + 文件 + 界面分发) │
│ Python 层(Chaquopy 内嵌 CPython 3.13)                │
│   bootstrap.py    sys.path/stdout 重定向到 LogBus     │
│   android_entry.py uvicorn 启停(should_exit 优雅停止) │
│ assets/backend.zip(首启解压到 filesDir/backend-<指纹>)│
│   api/ services/ utils/ scripts/ config.json VERSION │
│   web_dist/       Next.js 静态导出(原版 Web 面板)     │
│   curl_cffi/ tiktoken.py pybase64.py  <- 移植 shim   │
└──────────────────────────────────────────────────────┘
```

## 移植层修改(对上游的补丁)

Chaquopy 对部分原生包没有 Android 构建,移植层做了以下适配(由 `tools/prepare_backend.py` 自动应用,全部为显式替换,上游更新后若命中失败会立即报错):

| 依赖 | 问题 | 处理 |
|---|---|---|
| `pydantic-core` | 无 Android 构建,Rust 无法 pip 编译 | 降级 **pydantic v1**(纯 Python),`api/*.py` 中 `ConfigDict`→`class Config`、`model_dump()`→`dict()` |
| `curl_cffi` | 依赖 libcurl-impersonate,无 Android 构建 | `port/shims/curl_cffi/`:基于 **httpx** 实现项目用到的 `requests` 子集(Session/流式/cookie domain/代理/verify) |
| `tiktoken` | 无 Android 构建 | `port/shims/tiktoken.py`:近似分词计数(仅用于 token 统计) |
| `pybase64` | 无 Android 构建 | `port/shims/pybase64.py`:转发标准库 `base64` |
| `psycopg2-binary` | 无 Android 构建 | 不打包;存储后端用默认 `json`(或 `sqlite`)即可 |
| GitPython | **import 时即探测 git 二进制**, Android 上直接 `ImportError: Bad git executable` 拖垮启动 | `GIT_PYTHON_REFRESH=quiet`(bootstrap/android_entry 启动前设置)+ `factory.py` 容错补丁(git 后端仅选用时才报清晰错误) |
| FastAPI/uvicorn | — | 固定已验证版本(fastapi 0.125.0 + pydantic 1.10.26 + uvicorn 0.51.0) |

### Cloudflare 放行(对话链路)

`chatgpt.com` 按 TLS/HTTP2 指纹校验客户端,httpx 无 `impersonate` 能力,直连会被 403(managed challenge)。本移植采用 **WebView clearance 桥**(FlareSolverr 同款模式):

1. Java `CfClearanceHelper` 用真实 WebView(Chromium 内核,指纹/JS 引擎均为真)加载 `chatgpt.com`,等待 Cloudflare 自动放行并签发 `cf_clearance`;
2. Python `cf_bypass` 取回 clearance Cookie 与 WebView 真实 UA,注入 httpx 会话并移除与 Android UA 矛盾的 Windows/Edge 高熵头;
3. 后续请求凭 clearance 通过 CF;过期(15 分钟)或被 403/503 拒绝时自动刷新重试。刷新前会先定向清空目标站残留 Cookie(CookieManager 为应用级共享存储),确保轮询拿到的是 CF 本次新签发的 clearance,而非已被拒绝的旧值。

4. 首页预热(bootstrap)被 403/503 拒绝时**降级而非报错**:部分网络下上游只拦截 HTML 导航页,而 `/backend-api/*` 接口仍可直连;此时回退默认 PoW 脚本,由后续真实请求判定可用性。

注意:clearance 与 UA、出口 IP 绑定。WebView 走系统代理/VPN,httpx 默认读不到 Android 系统代理——App 会在未手动配置代理时**自动跟随系统代理**保持两侧出口一致;使用 VPN(TUN 模式)时无需任何配置。**若设备出口 IP 被 Cloudflare 风控(表现为 clearance 获取超时),请在设置中配置代理**。首次对话会比桌面版慢数秒(WebView 放行耗时)。

### 已知限制

- 仅打包 `arm64-v8a`(覆盖绝大多数现代手机);`postgres`/`git` 存储后端不可用(无对应原生库/无 git 二进制)。

## 重新构建

```bash
# 1. 拉取上游并准备后端(打补丁 + shim + 前端产物)
git clone --depth 1 https://github.com/RemotePinee/ChatGPT2API.git upstream
cd upstream/web && npm install && npm run build && cd ../..
python3 tools/prepare_backend.py upstream app/src/main/assets/backend.zip upstream/web/out

# 2. 构建 APK(需要 JDK17 + Android SDK platform-34/build-tools;签名用仓库内 release.keystore)
gradle assembleRelease
# 产物: app/build/outputs/apk/release/app-release.apk
```

**aarch64(ARM64)Linux 构建**: 一键脚本 `sudo tools/build_on_aarch64.sh`(幂等,
自动完成 Temurin JDK 17 + Gradle 8.9 + Android SDK 34 部署, aapt2 经 qemu-x86_64 + binfmt_misc 透明执行)。
原理与手动步骤见 [docs/BUILD_AARCH64.md](docs/BUILD_AARCH64.md)。

签名密钥 `release.keystore`(口令均为 `chatgpt2api`)仅用于 sideload 自签,便于覆盖安装升级。

## 环境要求

- Android 8.0+(API 26),arm64 设备
- 首次启动解压后端约 1-3 秒;数据目录 `files/backend-<指纹>/data/`
