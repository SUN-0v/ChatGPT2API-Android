"""Chaquopy 引导模块: 由 Java 调用, 负责 sys.path、日志重定向, 然后启动后端。"""

from __future__ import annotations

import os
import sys
import threading
import traceback

# 见 android_entry.py 注释: 必须在任何 GitPython import 之前设置
os.environ.setdefault("GIT_PYTHON_REFRESH", "quiet")

_backend_dir = None
_logbus = None
_redirected = False


def _get_logbus():
    global _logbus
    if _logbus is None:
        from com.chatgpt2api.server import LogBus  # Java 类(Chaquopy import hook)

        _logbus = LogBus
    return _logbus


class _SinkStream:
    """把 stdout/stderr 的行转发到 Java LogBus。"""

    def __init__(self):
        self._buf = ""
        self._lock = threading.Lock()

    def write(self, data):
        if not data:
            return 0
        with self._lock:
            self._buf += str(data)
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                self._emit(line)
        return len(data)

    def flush(self):
        with self._lock:
            if self._buf.strip():
                self._emit(self._buf)
            self._buf = ""

    def _emit(self, line):
        line = line.rstrip("\r")
        if not line.strip():
            return
        try:
            _get_logbus().fromPython(line)
        except Exception:
            # 日志通道自身故障时静默, 避免递归
            pass

    def isatty(self):
        return False


def _redirect_streams():
    global _redirected
    if _redirected:
        return
    _redirected = True
    sys.stdout = _SinkStream()
    sys.stderr = _SinkStream()
    # root logger 也接到 stderr -> LogBus(uvicorn 会自行配置其 logger)
    import logging

    if not logging.getLogger().handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        logging.getLogger().addHandler(handler)
        logging.getLogger().setLevel(logging.INFO)


def start(backend_dir: str, port: int) -> int:
    """阻塞运行, 直到服务停止。在 Java 的独立线程中调用。"""
    global _backend_dir
    try:
        _redirect_streams()
        print(f"[bootstrap] Python {sys.version.split()[0]} 初始化完成")
        _backend_dir = backend_dir
        if backend_dir not in sys.path:
            sys.path.insert(0, backend_dir)
        os.chdir(backend_dir)
        print(f"[bootstrap] 工作目录: {backend_dir}")
        print("[bootstrap] Android 版: httpx 无 TLS 指纹伪装,")
        print("[bootstrap] chatgpt.com 的 Cloudflare 校验走 WebView clearance 桥(cf_bypass)。")
        import android_entry

        return int(android_entry.run(int(port)))
    except BaseException:
        traceback.print_exc()
        return 1


def stop() -> None:
    try:
        import android_entry

        android_entry.stop()
    except BaseException:
        traceback.print_exc()
