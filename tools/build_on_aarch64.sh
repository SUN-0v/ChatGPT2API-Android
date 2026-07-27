#!/usr/bin/env bash
# 在 aarch64 Debian/Ubuntu 上一键搭建构建环境并构建 ChatGPT2API-Android APK。
#
# 工具链: Temurin JDK 17 + Gradle 8.9 + Android SDK 34;
#         aapt2/zipalign(x86_64 ELF, AGP 仅有的原生组件) 经 qemu-x86_64 + binfmt_misc 透明执行。
# 幂等: 已完成的步骤自动跳过, 可重复运行。需要 root(apt/mount/dpkg 多架构)。
#
# 用法: sudo tools/build_on_aarch64.sh
# 产物: app/build/outputs/apk/release/app-release.apk(已用仓库 release.keystore 签名)
set -euo pipefail

# ---- 可配置项(默认值已验证) ---------------------------------------------------
PREFIX="${PREFIX:-/opt}"
JDK_DIR="${JDK_DIR:-$PREFIX/jdk17}"
GRADLE_DIR="${GRADLE_DIR:-$PREFIX/gradle}"
export ANDROID_HOME="${ANDROID_HOME:-$PREFIX/android-sdk}"
export GRADLE_USER_HOME="${GRADLE_USER_HOME:-$PREFIX/gradle-home}"
BUILD_DIR="${BUILD_DIR:-$PREFIX/build-chatgpt2api}"
GRADLE_VERSION=8.9
JDK_URL="https://api.adoptium.net/v3/binary/latest/17/ga/linux/aarch64/jdk/hotspot/normal/eclipse"
GRADLE_URL="https://services.gradle.org/distributions/gradle-${GRADLE_VERSION}-bin.zip"
CMDTOOLS_URL="https://dl.google.com/android/repository/commandlinetools-linux-11076708_latest.zip"

PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUT_DIR="$PROJECT_DIR/app/build/outputs/apk/release"

log() { echo -e "\n==> $*"; }

[ "$(id -u)" = "0" ] || { echo "错误: 需要 root 运行(apt/mount/dpkg 多架构)"; exit 1; }
[ "$(uname -m)" = "aarch64" ] || { echo "错误: 本脚本仅用于 aarch64; x86_64 请直接用常规 gradle 流程"; exit 1; }

# ---- 1/6 系统依赖(qemu-user-static / amd64 运行库) ---------------------------
# 部分网络环境 Debian 镜像 80 端口不通, 先把 apt 源切到 HTTPS
log "1/6 安装系统依赖"
sed -i 's|URIs: http://|URIs: https://|g' /etc/apt/sources.list.d/*.sources 2>/dev/null || true
sed -i 's|^deb http://|deb https://|g' /etc/apt/sources.list 2>/dev/null || true
apt-get update -qq
apt-get install -y -qq qemu-user-static binfmt-support unzip zip file curl ca-certificates
if ! dpkg --print-foreign-architectures | grep -q '^amd64$'; then
    dpkg --add-architecture amd64
    apt-get update -qq
fi
# x86_64 版 aapt2 依赖的动态库(含解释器 /lib64/ld-linux-x86-64.so.2)
apt-get install -y -qq libc6:amd64 libstdc++6:amd64 zlib1g:amd64

# ---- 2/6 qemu-x86_64 + binfmt_misc 透明执行 ----------------------------------
log "2/6 配置 qemu-x86_64 + binfmt_misc"
mountpoint -q /proc/sys/fs/binfmt_misc || mount -t binfmt_misc none /proc/sys/fs/binfmt_misc
if [ ! -f /proc/sys/fs/binfmt_misc/qemu-x86_64 ]; then
    echo ':qemu-x86_64:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\x3e\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:/usr/bin/qemu-x86_64-static:OCF' \
        > /proc/sys/fs/binfmt_misc/register
fi

# ---- 3/6 JDK 17 + Gradle + Android SDK ---------------------------------------
log "3/6 准备 Temurin JDK 17 / Gradle $GRADLE_VERSION / Android SDK 34"
if [ ! -x "$JDK_DIR/bin/java" ]; then
    mkdir -p "$JDK_DIR"
    curl -fSL "$JDK_URL" | tar -xz -C "$JDK_DIR" --strip-components=1
fi
if [ ! -x "$GRADLE_DIR/bin/gradle" ]; then
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    curl -fSL -o "$TMP/g.zip" "$GRADLE_URL"
    unzip -q "$TMP/g.zip" -d "$TMP"
    mv "$TMP/gradle-$GRADLE_VERSION" "$GRADLE_DIR"
    rm -rf "$TMP"; trap - EXIT
fi
SDKMGR="$ANDROID_HOME/cmdline-tools/latest/bin/sdkmanager"
if [ ! -x "$SDKMGR" ]; then
    mkdir -p "$ANDROID_HOME/cmdline-tools"
    TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
    curl -fSL -o "$TMP/t.zip" "$CMDTOOLS_URL"
    unzip -q "$TMP/t.zip" -d "$TMP"
    mv "$TMP/cmdline-tools" "$ANDROID_HOME/cmdline-tools/latest"
    rm -rf "$TMP"; trap - EXIT
fi
export JAVA_HOME="$JDK_DIR" PATH="$JDK_DIR/bin:$PATH"
yes | "$SDKMGR" --licenses > /dev/null 2>&1 || true
[ -d "$ANDROID_HOME/platforms/android-34" ]   || "$SDKMGR" "platforms;android-34"
[ -d "$ANDROID_HOME/build-tools/34.0.0" ]     || "$SDKMGR" "build-tools;34.0.0"
[ -d "$ANDROID_HOME/platform-tools" ]         || "$SDKMGR" "platform-tools"

# ---- 4/6 验证 aapt2 可经 qemu 执行 -------------------------------------------
log "4/6 验证 aapt2(x86_64, qemu 透明执行)"
"$ANDROID_HOME/build-tools/34.0.0/aapt2" version

# ---- 5/6 同步源码到支持 POSIX 锁的文件系统 -----------------------------------
# 坑: 部分容器/挂载盘(overlayfs/网盘)不支持 fcntl 锁, Gradle 直接报
# "java.io.IOException: No locks available"。/opt、/tmp 等本地文件系统正常。
log "5/6 同步源码 -> $BUILD_DIR"
if [ "$(cd "$BUILD_DIR" 2>/dev/null && pwd)" != "$PROJECT_DIR" ]; then
    mkdir -p "$BUILD_DIR" "$GRADLE_USER_HOME"
    ( cd "$PROJECT_DIR" && tar -cf - --exclude=.git --exclude=app/build --exclude=.gradle . ) \
        | tar -xf - -C "$BUILD_DIR"
fi

# ---- 6/6 构建 ----------------------------------------------------------------
log "6/6 gradle assembleRelease"
( cd "$BUILD_DIR" && "$GRADLE_DIR/bin/gradle" assembleRelease --no-daemon --console=plain )
mkdir -p "$OUT_DIR"
cp "$BUILD_DIR/app/build/outputs/apk/release/app-release.apk" "$OUT_DIR/app-release.apk"

log "构建完成: $OUT_DIR/app-release.apk"
"$ANDROID_HOME/build-tools/34.0.0/aapt2" dump badging "$OUT_DIR/app-release.apk" 2>/dev/null | head -1 || true
