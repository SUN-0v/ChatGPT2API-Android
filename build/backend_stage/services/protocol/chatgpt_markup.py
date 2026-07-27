"""ChatGPT 网页版下发文本中私有区(PUA)标记的清洗。

ChatGPT 返回的文本里嵌有以 U+E200..U+E203 包裹的内部标记，浏览器 UI 渲染成
卡片/脚注；作为 OpenAI 兼容 API 透传则直接显示为 'entity[...]'、
'citeturn0search0' 等乱码。本模块负责剥离/转换这些标记，并配合流式增量解析。

两类常见标记：
- ``\\ue200entity[...]\\ue201``                实体卡片（电影、歌曲、人物等）
- ``\\ue200cite\\ue202<token>\\ue203...\\ue201`` 搜索引用脚注，URL 来自上游 metadata
"""
from __future__ import annotations

import json
import re
from typing import Any

OPEN = ""
CLOSE = ""
FIELD_SEP = ""
ITEM_SEP = ""

_BLOCK = re.compile(f"{OPEN}(.*?){CLOSE}", re.DOTALL)
_LONE_PUA = re.compile(f"[{OPEN}{CLOSE}{FIELD_SEP}{ITEM_SEP}]")
_TEXT_TAG = re.compile(r"</?Text(?:\s[^>]*)?>", re.IGNORECASE)
_RICH_TEXT_TAGS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"<Bold(?:\s[^>]*)?>(.*?)</Bold>", re.IGNORECASE | re.DOTALL), r"**\1**"),
    (re.compile(r"<Strong(?:\s[^>]*)?>(.*?)</Strong>", re.IGNORECASE | re.DOTALL), r"**\1**"),
    (re.compile(r"<Italic(?:\s[^>]*)?>(.*?)</Italic>", re.IGNORECASE | re.DOTALL), r"*\1*"),
    (re.compile(r"<Emphasis(?:\s[^>]*)?>(.*?)</Emphasis>", re.IGNORECASE | re.DOTALL), r"*\1*"),
    (re.compile(r"<Code(?:\s[^>]*)?>(.*?)</Code>", re.IGNORECASE | re.DOTALL), r"`\1`"),
    (re.compile(r"<Strikethrough(?:\s[^>]*)?>(.*?)</Strikethrough>", re.IGNORECASE | re.DOTALL), r"~~\1~~"),
)
_LINE_BREAK_TAG = re.compile(r"<(?:LineBreak|br)(?:\s[^>]*)?/?>", re.IGNORECASE)
_UNKNOWN_RICH_TEXT_TAG = re.compile(
    r"</?(?:Paragraph|List|OrderedList|UnorderedList|ListItem|Item)(?:\s[^>]*)?>",
    re.IGNORECASE,
)
_TRAILING_PARTIAL_TEXT_TAG = re.compile(r"<(?:/?(?:T(?:e(?:x(?:t)?)?)?)?)?$", re.IGNORECASE)


def collect_references(node: Any, references: dict[str, dict[str, Any]]) -> None:
    """递归扫描事件树，收集 ``matched_text`` 到引用元数据的映射。"""
    if isinstance(node, dict):
        matched = node.get("matched_text")
        if isinstance(matched, str) and OPEN in matched and matched not in references:
            items: list[dict[str, str]] = []
            for raw_item in node.get("items") or []:
                if not isinstance(raw_item, dict):
                    continue
                url = str(raw_item.get("url") or "").strip()
                title = str(raw_item.get("title") or "").strip()
                if url:
                    items.append({"url": url, "title": title})
            references[matched] = {
                "url": str(node.get("url") or "").strip(),
                "title": str(node.get("title") or "").strip(),
                "items": items,
            }
        for value in node.values():
            collect_references(value, references)
    elif isinstance(node, list):
        for value in node:
            collect_references(value, references)


def split_stable(text: str) -> str:
    """截出已闭合的稳定前缀；尾部未闭合的标记保留到下一帧再处理。"""
    last_open = text.rfind(OPEN)
    last_close = text.rfind(CLOSE)
    if last_open > last_close:
        text = text[:last_open]
    partial = _TRAILING_PARTIAL_TEXT_TAG.search(text)
    if partial:
        return text[:partial.start()]
    return text


def _render_rich_text_tags(text: str) -> str:
    """把上游富文本包装转成 Markdown，避免剥标签时丢掉粗体等语义。"""
    if "<" not in text:
        return text
    out = text
    for pattern, replacement in _RICH_TEXT_TAGS:
        out = pattern.sub(replacement, out)
    out = _LINE_BREAK_TAG.sub("\n", out)
    out = _TEXT_TAG.sub("", out)
    return _UNKNOWN_RICH_TEXT_TAG.sub("", out)


def _first_readable(values: list[Any]) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _render_entity(inner: str) -> str:
    body = inner[len("entity"):]
    try:
        arr = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        quoted = re.findall(r'"([^"]+)"', body)
        return _first_readable(quoted[1:] + quoted[:1])
    if isinstance(arr, list):
        return _first_readable(arr[1:] + arr[:1])
    if isinstance(arr, dict):
        return _first_readable([arr.get("name"), arr.get("title"), arr.get("text"), arr.get("label")])
    return ""


def _render_cite(
    block: str,
    references: dict[str, dict[str, Any]],
    cite_numbers: dict[str, int],
    cite_counter: list[int],
) -> str:
    info = references.get(block)
    if not info:
        return ""
    urls = [item["url"] for item in info.get("items") or [] if item.get("url")]
    if not urls and info.get("url"):
        urls = [info["url"]]
    if not urls:
        return ""
    if block not in cite_numbers:
        cite_counter[0] += 1
        cite_numbers[block] = cite_counter[0]
    return f"[[{cite_numbers[block]}]]({urls[0]})"


def sanitize(
    text: str,
    references: dict[str, dict[str, Any]],
    cite_numbers: dict[str, int],
    cite_counter: list[int],
) -> str:
    """对稳定前缀做替换并去掉孤立 PUA 字符；未闭合的标记不动。"""
    if not text:
        return text
    stable = split_stable(text)
    if OPEN not in stable:
        return _render_rich_text_tags(_LONE_PUA.sub("", stable))

    def replace(match: re.Match[str]) -> str:
        block = match.group(0)
        inner = match.group(1)
        if inner.startswith("entity"):
            return _render_entity(inner)
        if inner.startswith("cite" + FIELD_SEP):
            return _render_cite(block, references, cite_numbers, cite_counter)
        return ""

    return _render_rich_text_tags(_LONE_PUA.sub("", _BLOCK.sub(replace, stable)))
