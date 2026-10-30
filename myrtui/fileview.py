"""文件预览的纯逻辑层：读盘 / 解码 / 二进制判定 / 截断。

**不 import Textual**——``read_for_preview`` 只依赖标准库，单元测试不需要
App / 事件循环。UI 侧（``client/mdpreview.py``）只消费这里的 ``Preview``。

口径（延续 v0.21 / v0.36）：

- 最多读 ``MAX_PREVIEW_BYTES``（1 MB）——避免误点 100 MB 日志把 UI 冻住；
  Markdown 预览层另有 512 KiB 的入口闸门（见 ``client/mdpreview.py``）；
- 前 ``BINARY_SNIFF_BYTES``（8 KiB）含 NUL → 二进制，不做渲染；
- 解码顺序 UTF-8-SIG → UTF-8 → locale → UTF-8 ``errors="replace"``；
- 换行统一成 ``\\n``；
- 超限时截到最后一个完整行，``truncated=True``。
"""
from __future__ import annotations

import locale
import stat
from dataclasses import dataclass
from pathlib import Path

from myrtui.i18n import t

MAX_PREVIEW_BYTES = 1_000_000
BINARY_SNIFF_BYTES = 8 * 1024


@dataclass(frozen=True)
class Preview:
    """一次预览读盘的结果（不可变，缺失字段用默认值表达「没有」）。"""

    text: str
    """解码 + 换行归一后的文本；``error`` 非 None 或二进制时为空串。"""

    encoding: str
    """实际使用的编码：``utf-8`` / ``utf-8-sig`` / locale / ``utf-8-replace``。"""

    lines: int
    """行数（空文件 = 0；无尾换行不虚增一行）。"""

    size: int
    """磁盘上的原始大小（字节）；截断不影响它。"""

    truncated: bool
    """是否因超过 ``MAX_PREVIEW_BYTES`` 而截断。"""

    binary: bool
    """是否判定为二进制（前 8 KiB 含 NUL）。"""

    error: str | None
    """人类可读的读取失败原因；非 None 时 ``text`` 为空。"""


def _empty(
    *,
    encoding: str = "utf-8",
    size: int = 0,
    truncated: bool = False,
    binary: bool = False,
    error: str | None = None,
) -> Preview:
    return Preview(
        text="",
        encoding=encoding,
        lines=0,
        size=size,
        truncated=truncated,
        binary=binary,
        error=error,
    )


def _decode(data: bytes) -> tuple[str, str]:
    """按 UTF-8-SIG → UTF-8 → locale → replace 的顺序解码。"""
    if data.startswith(b"\xef\xbb\xbf"):
        try:
            return data.decode("utf-8-sig"), "utf-8-sig"
        except UnicodeDecodeError:
            pass  # 有 BOM 但内容非法 → 继续走后面的兜底
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass
    fallback = locale.getpreferredencoding(False) or "utf-8"
    if fallback.lower().replace("-", "") not in ("utf8", "utf8sig"):
        try:
            return data.decode(fallback), fallback
        except (UnicodeDecodeError, LookupError):
            pass
    return data.decode("utf-8", errors="replace"), "utf-8-replace"


def _trim_incomplete_utf8(data: bytes) -> bytes:
    """截断后去掉尾部不完整的 UTF-8 序列（最多 3 字节）。

    1 MB 硬截断可能把一个多字节字符切成两半；不修剪的话，严格 UTF-8 解码
    会失败 → 退化到 ``utf-8-replace``，明明是真 UTF-8 文件却报「换了编码」。
    """
    try:
        data.decode("utf-8")
        return data
    except UnicodeDecodeError:
        pass
    for cut in range(1, 4):
        candidate = data[:-cut]
        try:
            candidate.decode("utf-8")
            return candidate
        except UnicodeDecodeError:
            continue
    return data


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _count_lines(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def _error_text(exc: OSError) -> str:
    """``OSError`` → 人类可读文案（Permission denied 等用 strerror）。"""
    reason = exc.strerror or str(exc)
    return t("file.read_failed", reason=reason)


def read_for_preview(path: str | Path) -> Preview:
    """读一个文件并生成 ``Preview``（任何失败都转成 ``error``，不抛）。"""
    target = Path(path)
    try:
        info = target.stat()
    except FileNotFoundError:
        return _empty(error=t("file.gone"))
    except OSError as exc:
        return _empty(error=_error_text(exc))
    if not stat.S_ISREG(info.st_mode):
        return _empty(size=info.st_size, error=t("file.not_regular"))

    size = info.st_size
    try:
        with target.open("rb") as handle:
            data = handle.read(MAX_PREVIEW_BYTES + 1)
    except FileNotFoundError:
        return _empty(size=size, error=t("file.gone"))
    except OSError as exc:
        return _empty(size=size, error=_error_text(exc))

    truncated = len(data) > MAX_PREVIEW_BYTES
    if truncated:
        data = data[:MAX_PREVIEW_BYTES]
        # 截到最后一个完整行；整段没有换行（单行大文件）就按字节上限截
        cut = data.rfind(b"\n")
        if cut >= 0:
            data = data[: cut + 1]

    binary = b"\x00" in data[:BINARY_SNIFF_BYTES]
    if binary:
        return _empty(size=size, truncated=truncated, binary=True)

    if truncated:
        data = _trim_incomplete_utf8(data)
    text, encoding = _decode(data)
    text = _normalize_newlines(text)
    return Preview(
        text=text,
        encoding=encoding,
        lines=_count_lines(text),
        size=size,
        truncated=truncated,
        binary=False,
        error=None,
    )
