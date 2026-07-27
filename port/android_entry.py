"""Android 端服务入口(由 Chaquopy bootstrap 调用)。

在调用线程内运行 uvicorn; stop() 通过 should_exit 让 uvicorn 优雅退出。
"""

from __future__ import annotations

import os
import sys
import threading

# Android 无 git 可执行文件: GitPython 在 import 时会探测 git binary 并直接
# 抛 ImportError, 导致整个后端起不来。quiet 模式让 import 静默通过,
# git 存储后端(本就用不了)仅在实际选用时才报错。
os.environ.setdefault("GIT_PYTHON_REFRESH", "quiet")

_server = None
_lock = threading.Lock()


def run(port: int = 3000, host: str = "0.0.0.0") -> int:
    """阻塞式启动服务, 返回退出码。由 Java 在独立线程调用。"""
    global _server
    import uvicorn
    from api import create_app

    app = create_app()
    config = uvicorn.Config(
        app,
        host=host,
        port=int(port),
        access_log=True,
        log_level="info",
        loop="asyncio",
        http="h11",
    )
    server = uvicorn.Server(config)
    with _lock:
        _server = server
    print(f"[android] ChatGPT2API 服务启动: http://{host}:{port}", flush=True)
    try:
        server.run()
    except BaseException as exc:  # 保证异常进入日志
        print(f"[android] 服务异常退出: {exc.__class__.__name__}: {exc}", file=sys.stderr, flush=True)
        import traceback

        traceback.print_exc()
        return 1
    finally:
        with _lock:
            _server = None
    print("[android] ChatGPT2API 服务已停止", flush=True)
    return 0


def stop() -> None:
    """请求服务停止(幂等)。"""
    with _lock:
        server = _server
    if server is not None:
        print("[android] 收到停止请求, 正在关闭服务...", flush=True)
        server.should_exit = True
