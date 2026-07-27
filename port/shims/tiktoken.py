"""tiktoken 兼容 shim(Android 移植层)。

Chaquopy 无 tiktoken 原生构建。项目只用它做 token 计数(len(encoding.encode(text))),
这里提供近似实现: 按"词/标点/空白"切分估算, 误差对业务(额度/截断提示)可接受。
"""

from __future__ import annotations

import re
from typing import List

_TOKEN_RE = re.compile(r"\w+|[^\w\s]|\s+", re.UNICODE)


class _Encoding:
    def __init__(self, name: str):
        self.name = name

    def encode(self, text: str) -> List[int]:
        if not text:
            return []
        # 近似: 每个分段记 1 个 token; 长单词按 4 字符/个补足
        tokens: List[int] = []
        idx = 0
        for piece in _TOKEN_RE.findall(text):
            if piece.isspace():
                continue
            n = max(1, (len(piece) + 3) // 4) if piece.isalnum() else 1
            tokens.extend(range(idx, idx + n))
            idx += n
        return tokens

    def decode(self, tokens: List[int]) -> str:  # 仅为 API 完整, 项目未使用
        return ""


def get_encoding(name: str) -> _Encoding:
    return _Encoding(name)


def encoding_for_model(model: str) -> _Encoding:
    return _Encoding(f"approx-for-{model}")
