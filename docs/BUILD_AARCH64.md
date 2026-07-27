# aarch64 Linux 构建工作流

在 aarch64(ARM64)Debian/Ubuntu 上构建本项目 APK 的完整流程。已封装为一键脚本
[`tools/build_on_aarch64.sh`](../tools/build_on_aarch64.sh)(幂等,可重复运行),本文档说明其原理与手动步骤。

## 一键使用

```bash
sudo tools/build_on_aarch64.sh
# 产物: app/build/outputs/apk/release/app-release.apk(仓库 release.keystore 签名)
```

## 工具链组成

| 组件 | 版本 | 说明 |
|---|---|---|
| Temurin JDK | 17(aarch64) | AGP 8.7 要求 JDK 17 |
| Gradle | 8.9 | 与 AGP 8.7.3 匹配 |
| Android SDK | platform-34 + build-tools 34.0.0 | compileSdk 34 / targetSdk 34 |
| qemu-user-static + binfmt_misc | — | 透明执行 x86_64 的 aapt2/zipalign |
| libc6:amd64 等多架构库 | — | aapt2 是动态链接的 x86-64 ELF,依赖 `/lib64/ld-linux-x86-64.so.2` 及 libstdc++/zlib |

**为什么需要 qemu**:AGP 构建链路中只有 aapt2(资源编译)和 zipalign(对齐)是原生可执行文件,
Google 只发布 x86_64 Linux 版;其余(d8、apksigner 等)均为 Java。注册 binfmt_misc 后,
内核遇到 x86_64 ELF 自动交给 qemu-x86_64 执行,Gradle 无感知。

## 两个关键坑(脚本已处理)

1. **apt 镜像 80 端口不通** → 把 `/etc/apt/sources.list(.d/*)` 的 URI 从 http 改为 https。
2. **"java.io.IOException: No locks available"** → 部分容器/挂载盘(overlayfs、网盘)
   不支持 fcntl 锁,Gradle 守护进程直接崩溃。脚本把源码同步到 `$BUILD_DIR`
   (默认 `/opt/build-chatgpt2api`,本地 ext4/overlay 上层正常)再构建,
   并把 `GRADLE_USER_HOME` 也放到本地文件系统。

## 手动步骤(等价于脚本)

```bash
# 0. 系统依赖(apt 源必要时先切 https)
dpkg --add-architecture amd64 && apt-get update
apt-get install -y qemu-user-static libc6:amd64 libstdc++6:amd64 zlib1g:amd64

# 1. binfmt(Debian 的 qemu-user-static 一般自动注册; 没有则手动)
mount -t binfmt_misc none /proc/sys/fs/binfmt_misc
cat /proc/sys/fs/binfmt_misc/qemu-x86_64   # 确认 enabled

# 2. 工具链
curl -L https://api.adoptium.net/v3/binary/latest/17/ga/linux/aarch64/jdk/hotspot/normal/eclipse \
  | tar -xz -C /opt/jdk17 --strip-components=1
curl -LO https://services.gradle.org/distributions/gradle-8.9-bin.zip && unzip -q gradle-8.9-bin.zip -d /opt
# cmdline-tools 解压到 /opt/android-sdk/cmdline-tools/latest 后:
sdkmanager "platforms;android-34" "build-tools;34.0.0" "platform-tools"

# 3. 验证 x86_64 原生组件
/opt/android-sdk/build-tools/34.0.0/aapt2 version   # 应输出版本号而非 Exec format error

# 4. 构建(注意: 工程必须位于支持 POSIX 锁的文件系统)
export JAVA_HOME=/opt/jdk17 ANDROID_HOME=/opt/android-sdk
/opt/gradle-8.9/bin/gradle assembleRelease --no-daemon
```

## 产物验证清单

```bash
aapt2 dump badging app-release.apk | head -1     # versionCode/versionName
apksigner verify --print-certs app-release.apk   # 签名
# dex 中包含修复后的类/方法; assets/backend.zip 与 build/backend_stage 一致
```

参考环境: Debian 12 aarch64, 2C8G, 首次全量约 10 分钟(含下载), 增量构建约 70 秒。
