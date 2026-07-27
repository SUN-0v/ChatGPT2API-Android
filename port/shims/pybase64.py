"""pybase64 兼容 shim: 直接转发标准库 base64 的同名函数。

Chaquopy 无 pybase64 原生构建; 标准库实现功能一致, 仅性能略低。
"""

from base64 import (  # noqa: F401
    b64encode,
    b64decode,
    standard_b64encode,
    standard_b64decode,
    urlsafe_b64encode,
    urlsafe_b64decode,
    b32encode,
    b32decode,
    b16encode,
    b16decode,
    a85encode,
    a85decode,
    b85encode,
    b85decode,
)
