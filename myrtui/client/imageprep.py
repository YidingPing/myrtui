"""图片 → 可显示的位图（纯逻辑，Pillow 可选）。

Markdown 预览只处理**独立成段**的 ``![alt](path)``（行内图片仍由文本流显示
alt）；单文件图片预览复用同一套路径校验 / 解码 / alpha 预合成：

- 路径相对 Markdown 文件所在目录；接受绝对路径与 ``file://``；
- **拒绝远程 URL**（``http://`` / ``https://`` / ``data:``）——不联网；
- 只允许普通文件；单文件 ≤10 MB、像素 ≤4000×4000；
- 解码成功后按终端单元格尺寸预缩放（宽度 ≤ 正文宽、高度 ≤ 20 行），并把
  alpha **预合成到主题背景色**（Tabby 的 Sixel 透明语义不可靠，见
  ``docs-internal/docs/V37-SIXEL-GRAPHICS-PLAN.md`` §3.4 F4）。

没有 Pillow 时全部回落 alt 文本——绝不抛异常、绝不吞掉前后段落。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from markdown_it import MarkdownIt

from myrtui.i18n import t

log = logging.getLogger("myrtui.imageprep")

MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 4000
MAX_IMAGES_PER_DOC = 32
MAX_ROWS = 20

REMOTE_SCHEMES = ("http", "https", "data", "ftp", "mailto")


@dataclass(frozen=True)
class Prepared:
    """一张图片的准备结果（``image`` 为 ``None`` 时显示 ``alt``）。"""

    source: str
    alt: str
    image: Any | None = None
    width_cells: int = 0
    height_cells: int = 0
    note: str | None = None
    format: str = ""
    """Pillow 识别的格式（``PNG`` / ``JPEG`` …）；解码失败时为空。"""
    original_size: tuple[int, int] = (0, 0)
    """解码（含 EXIF 方向校正）后的像素尺寸。"""

    @property
    def ok(self) -> bool:
        return self.image is not None


def _parser() -> MarkdownIt:
    return MarkdownIt("gfm-like")


def extract_standalone_images(text: str) -> list[tuple[str, str]]:
    """抽出「整段只有一个图片」的 ``(src, alt)``（保持文档顺序）。"""
    if "](" not in text:
        return []
    tokens = _parser().parse(text)
    found: list[tuple[str, str]] = []
    for index, token in enumerate(tokens):
        if token.type != "paragraph_open":
            continue
        if index + 2 >= len(tokens):
            continue
        inline = tokens[index + 1]
        closing = tokens[index + 2]
        if inline.type != "inline" or closing.type != "paragraph_close":
            continue
        children = [
            child
            for child in (inline.children or [])
            if not (child.type == "text" and not child.content.strip())
        ]
        if len(children) != 1 or children[0].type != "image":
            continue
        image = children[0]
        src = image.attrGet("src") or ""
        if not src:
            continue
        found.append((src, image.content or ""))
    return found


def resolve_image(src: str, base_dir: str | Path) -> tuple[Path | None, str | None]:
    """``src`` → 本地路径；拒绝远程 / 不存在 / 非普通文件。"""
    raw = (src or "").strip()
    if not raw:
        return None, t("img.empty_path")

    parsed = urlparse(raw)
    if parsed.scheme:
        if parsed.scheme.lower() in REMOTE_SCHEMES:
            return None, t("img.remote")
        if parsed.scheme.lower() != "file":
            return None, t("img.unsupported_scheme", scheme=parsed.scheme)
        raw = unquote(parsed.path)

    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = Path(base_dir) / candidate
    try:
        candidate = candidate.resolve()
    except OSError:
        return None, t("img.unresolvable")

    try:
        stat = candidate.stat()
    except OSError:
        return None, t("img.missing")
    if not candidate.is_file():
        return None, t("img.not_regular")
    if stat.st_size <= 0:
        return None, t("img.empty")
    if stat.st_size > MAX_IMAGE_BYTES:
        return None, t("img.too_large")
    return candidate, None


def _rgb(color: str) -> tuple[int, int, int]:
    value = (color or "").strip().lstrip("#")
    if len(value) == 3:
        value = "".join(char * 2 for char in value)
    if len(value) != 6:
        return (30, 30, 30)
    try:
        return (int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16))
    except ValueError:
        return (30, 30, 30)


def prepare_one(
    src: str,
    alt: str,
    *,
    base_dir: str | Path,
    background: str,
    max_width_px: int,
    max_height_px: int,
    cell: tuple[int, int],
) -> Prepared:
    """解析 + 解码 + 缩放 + 预合成；失败时 ``image=None`` + ``note``。"""
    path, reason = resolve_image(src, base_dir)
    if path is None:
        return Prepared(source=src, alt=alt or src, note=reason)

    try:
        from PIL import Image  # noqa: PLC0415 —— 可选依赖
    except Exception:  # noqa: BLE001
        return Prepared(source=src, alt=alt or src, note=t("img.no_pillow"))

    try:
        from PIL import ImageOps  # noqa: PLC0415 —— 可选依赖

        with Image.open(path) as raw:
            raw.load()
            if raw.width * raw.height > MAX_IMAGE_PIXELS * MAX_IMAGE_PIXELS:
                return Prepared(source=src, alt=alt or src, note=t("img.too_many_pixels"))
            format_name = (raw.format or "").upper()
            # 手机照片常靠 EXIF 方向标记；不校正会横躺 / 倒置。
            image = ImageOps.exif_transpose(raw).convert("RGBA")
            original_size = image.size
    except Exception:  # noqa: BLE001 —— 坏文件 / 不支持的格式
        log.debug("图片解码失败：%s", path, exc_info=True)
        return Prepared(source=src, alt=alt or src, note=t("img.decode_failed"))

    target_w = max(1, min(int(max_width_px), image.width))
    target_h = max(1, int(max_height_px))
    scale = min(target_w / image.width, target_h / image.height, 1.0)
    if scale < 1.0:
        image = image.resize(
            (max(1, int(image.width * scale)), max(1, int(image.height * scale))),
            Image.LANCZOS,
        )

    base = Image.new("RGBA", image.size, _rgb(background) + (255,))
    flattened = Image.alpha_composite(base, image).convert("RGB")

    cell_w = max(1, int(cell[0]))
    cell_h = max(1, int(cell[1]))
    width_cells = max(1, -(-flattened.width // cell_w))
    height_cells = max(1, -(-flattened.height // cell_h))
    return Prepared(
        source=src,
        alt=alt or src,
        image=flattened,
        width_cells=width_cells,
        height_cells=height_cells,
        format=format_name,
        original_size=original_size,
    )


def prepare_document(
    text: str,
    *,
    base_dir: str | Path,
    background: str,
    max_width_px: int,
    max_height_px: int,
    cell: tuple[int, int] = (10, 20),
    limit: int = MAX_IMAGES_PER_DOC,
    prepare: Any | None = None,
) -> dict[str, Prepared]:
    """worker 专用：把整篇的独立图片预先准备好（键 = 原始 ``src``）。"""
    items = extract_standalone_images(text)
    if not items:
        return {}
    worker = prepare or prepare_one
    prepared: dict[str, Prepared] = {}
    for src, alt in items[:limit]:
        if src in prepared:
            continue
        try:
            prepared[src] = worker(
                src,
                alt,
                base_dir=base_dir,
                background=background,
                max_width_px=max_width_px,
                max_height_px=max_height_px,
                cell=cell,
            )
        except Exception:  # noqa: BLE001 —— D5：单图失败只影响该条
            log.debug("图片准备失败：%s", src, exc_info=True)
            prepared[src] = Prepared(source=src, alt=alt or src, note=t("img.prepare_failed"))
    return prepared
