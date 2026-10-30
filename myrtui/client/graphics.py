"""终端位图能力探测（纯逻辑，不 import Textual）。

v0.37 的位图链路只在 Textual 预览层生效；pane 是 ``capture-pane`` 文本抓屏，
永远没有位图。本模块只回答两个问题：

1. 当前终端能不能画 Sixel / Kitty TGP；
2. 单元格像素尺寸是多少（决定图片宽高换算）。

约束：

- **必须在 ``App.run()`` 之前调用**：``textual-image`` 的探测要读 stdin，
  Textual 起来后会抢走输入；
- 没装 ``textual-image`` / 非 tty / 探测异常 → 全部 ``False``（回落 Unicode），
  绝不抛出；
- ``MYRTUI_GRAPHICS`` 可强制：``auto``（默认）/ ``sixel`` / ``tgp`` /
  ``unicode`` / ``off``；非法值告警后回落 ``auto``。
- v0.3.1：``available``（textual-image 是否可导入）与 ``bitmaps``（终端是否有
  Sixel/TGP）分开报告；``blocked_reason`` 负责给用户可执行的提示，别再一律
  甩锅给终端。
"""
from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass
from typing import Callable

from myrtui.i18n import t

log = logging.getLogger("myrtui.graphics")

ENV_NAME = "MYRTUI_GRAPHICS"
ENV_CELL = "MYRTUI_CELL_PX"
MODES = ("auto", "sixel", "tgp", "unicode", "off")

DEFAULT_CELL_WIDTH = 10
DEFAULT_CELL_HEIGHT = 20
"""探测不到像素尺寸时的兜底（VT340 口径，同 textual-image）。"""


@dataclass(frozen=True)
class Capabilities:
    """一次探测的结果（不可变，可直接注入测试）。"""

    sixel: bool = False
    tgp: bool = False
    cell_width: int = DEFAULT_CELL_WIDTH
    cell_height: int = DEFAULT_CELL_HEIGHT
    available: bool = False
    """``textual-image`` 是否可导入（与终端能力分开记，便于提示文案）。"""

    mode: str = "off"
    """实际生效的模式：``auto`` / ``sixel`` / ``tgp`` / ``unicode`` / ``off``。"""

    @property
    def bitmaps(self) -> bool:
        """是否有可用的位图协议（Sixel 或 TGP）。"""
        return bool(self.sixel or self.tgp)

    @property
    def protocol(self) -> str:
        return "sixel" if self.sixel else ("tgp" if self.tgp else "")

    def cells_for(self, width_px: int, height_px: int) -> tuple[int, int]:
        """像素尺寸 → 单元格数（向上取整，至少 1）。"""
        cw = max(1, int(self.cell_width or DEFAULT_CELL_WIDTH))
        ch = max(1, int(self.cell_height or DEFAULT_CELL_HEIGHT))
        return (
            max(1, -(-int(width_px) // cw)),
            max(1, -(-int(height_px) // ch)),
        )


DISABLED = Capabilities()


def _load_probe() -> Callable[[], object] | None:
    """返回 ``textual_image`` 的探测函数；不可用时返回 ``None``。

    只 import 顶层包，不 import Textual 的任何东西。
    """
    try:
        from textual_image._terminal import probe_terminal  # noqa: PLC0415
    except Exception:  # noqa: BLE001 —— 缺依赖 / 私有 API 漂移都只意味着无位图
        log.debug("textual-image 不可用，位图关闭", exc_info=True)
        return None
    return probe_terminal


def _normalize_mode(raw: str | None) -> tuple[str, str | None]:
    mode = (raw or "").strip().lower() or "auto"
    if mode not in MODES:
        warning = t("gfx.mode_invalid", env=ENV_NAME, raw=raw)
        log.warning(warning)
        return "auto", warning
    return mode, None


def cell_override() -> tuple[int, int] | None:
    """``MYRTUI_CELL_PX=10x20`` → 覆盖探测到的单元格像素尺寸。

    有的终端不回报 CSI 16 t，``textual-image`` 会退回 10×20；若真实 cell
    不同，公式/图片字号的换算会整体偏大或偏小。这个环境变量给手动兜底。
    """
    raw = os.environ.get(ENV_CELL) or ""
    parts = [part for part in raw.strip().lower().replace("×", "x").replace(",", "x").split("x") if part]
    if len(parts) != 2:
        return None
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if width <= 0 or height <= 0:
        return None
    return width, height


def _cell_from(value: object, attr: str, default: int) -> int:
    try:
        number = int(getattr(value, attr))
    except (AttributeError, TypeError, ValueError):
        return default
    return number if number > 0 else default


def detect(
    env: str | None = None,
    *,
    probe: Callable[[], object] | None = None,
    tty: bool | None = None,
    loader: Callable[[], Callable[[], object] | None] | None = None,
) -> tuple[Capabilities, str | None]:
    """探测能力 → ``(capabilities, warning)``。

    ``probe`` / ``tty`` / ``loader`` 供测试注入；生产只用默认值。
    """
    mode, warning = _normalize_mode(
        os.environ.get(ENV_NAME) if env is None else env
    )

    if mode in ("off", "unicode"):
        # 显式关闭不探测终端；仍记录依赖是否可导入，便于提示 / 日志区分
        # 「用户关掉的」与「依赖没装」。
        if probe is None:
            probe = (loader or _load_probe)()
        return Capabilities(mode=mode, available=probe is not None), warning

    if probe is None:
        load = loader or _load_probe
        probe = load()
        if probe is None:
            # textual-image 不在：两种客户端都给 warning；auto 只写 info
            # （不刷启动告警），显式要求 sixel/tgp 才 log.warning。
            forced = mode in ("sixel", "tgp")
            warning = warning or (
                t("gfx.forced_missing_dep", env=ENV_NAME, mode=mode)
                if forced
                else t("gfx.missing_dep")
            )
            if forced:
                log.warning(warning)
            else:
                log.info(warning)
            return Capabilities(mode="unicode"), warning
        available = True
    else:
        available = True

    if tty is None:
        try:
            tty = bool(sys.stdout.isatty())
        except Exception:  # noqa: BLE001 —— 极端环境（stdout 已关）
            tty = False
    if not tty:
        log.debug("stdout 不是 tty，跳过位图探测")
        return Capabilities(available=available, mode="unicode"), warning

    try:
        result = probe()
    except Exception:  # noqa: BLE001 —— 探测失败只意味着回落
        log.warning("终端位图探测失败，回落到 Unicode", exc_info=True)
        return Capabilities(available=available, mode="unicode"), warning

    sixel = bool(getattr(result, "sixel", False))
    tgp = bool(getattr(result, "tgp", False))
    cell = getattr(result, "cell_size", None)
    cell_width = _cell_from(cell, "width", DEFAULT_CELL_WIDTH)
    cell_height = _cell_from(cell, "height", DEFAULT_CELL_HEIGHT)
    override = cell_override()
    if override is not None:
        cell_width, cell_height = override

    if mode == "sixel":
        sixel, tgp = sixel, False
        if not sixel:
            warning = warning or t("gfx.sixel_unsupported", env=ENV_NAME)
    elif mode == "tgp":
        sixel, tgp = False, tgp
        if not tgp:
            warning = warning or t("gfx.tgp_unsupported", env=ENV_NAME)

    resolved = mode if (sixel or tgp) else "unicode"
    capabilities = Capabilities(
        sixel=sixel,
        tgp=tgp,
        cell_width=cell_width,
        cell_height=cell_height,
        available=True,
        mode=resolved,
    )
    if warning:
        log.warning(warning)
    return capabilities, warning


def detect_silent(env: str | None = None) -> Capabilities:
    """``detect`` 的便捷版：丢弃告警（启动路径用，告警已写日志）。"""
    capabilities, _warning = detect(env)
    return capabilities


def describe(capabilities: Capabilities) -> str:
    """一行诊断文本（写进预览 header / 日志）。"""
    if not capabilities.bitmaps:
        text = f"graphics={capabilities.mode}"
        if capabilities.mode != "off" and not capabilities.available:
            text += t("gfx.missing_dep_short")
        return text
    return (
        f"graphics={capabilities.protocol} "
        f"cell={capabilities.cell_width}x{capabilities.cell_height}"
    )


INSTALL_HINT = "pipx inject myrtui textual-image pillow"


def blocked_reason(capabilities: Capabilities, env: str | None = None) -> str:
    """位图不可用时的可执行提示。

    三种原因必须分开说，否则用户会去查终端（v0.3.0 的真实踩坑）：
    ``MYRTUI_GRAPHICS`` 显式关掉、缺 Python 依赖、终端没有 Sixel/TGP。
    """
    raw = os.environ.get(ENV_NAME) if env is None else env
    mode = (raw or "").strip().lower() or "auto"
    if mode in ("off", "unicode"):
        return t("gfx.mode_off", env=ENV_NAME, mode=mode)
    if not capabilities.available:
        return t("gfx.missing_dep_long", hint=INSTALL_HINT)
    return t("gfx.no_protocol")
