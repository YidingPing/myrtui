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

两条硬约束（都是「满屏 +」的真因，2026-11 真机实测）：

- **tmux 里绝不用 Sixel**。tmux 收到 pane 里的 sixel 后不会转发，而是画一行
  ``SIXEL IMAGE (宽x高)`` 再铺满 ``+``（``tmux`` 二进制里的
  ``SIXEL IMAGE (%ux%u)`` / ``screen_write_sixelimage``）。给 client 加
  ``terminal-features ',*:sixel'``、甚至让终端在 DA 里声明 sixel，tmux 3.8
  依旧只给这堵 ``+`` 墙 —— 所以判据只能是「在复用器里就关掉 sixel」。
  更坑的是 tmux 会**对 pane 谎报** sixel（DA 回 ``ESC[?1;2;4c``），
  探测结果因此是 ``sixel=True``，`textual-image` 于是走 sixel 分支。
- **TGP 只有实现 ``U=1`` 的终端才能画**。``textual-image`` 的 TGP 不是
  ``a=T`` 直接显示，而是「静默传图 + 虚拟放置 + 写 ``U+10EEEE`` 占位符」；
  只答了 ``a=q`` 的 ``;OK`` 却不实现占位符的终端，会把这串占位符当文字打出来
  （满屏字符网格，字体不同就长成 ``+``）。kitty 0.50+ 支持特性查询
  ``a=q,e=T,U=1``：回执带 ``U=1`` 才算支持，带 ``e=T`` 不带 ``U=1`` 算不支持，
  只有裸 ``;OK`` 则是「问不出来」（老 kitty / 半截实现）——那时只信
  ``xterm-kitty`` / ``xterm-ghostty``，其余一律当不支持。
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Callable

from myrtui import termenv
from myrtui.i18n import t

log = logging.getLogger("myrtui.graphics")

ENV_NAME = "MYRTUI_GRAPHICS"
ENV_CELL = "MYRTUI_CELL_PX"
MODES = ("auto", "sixel", "tgp", "unicode", "off")

DEFAULT_CELL_WIDTH = 10
DEFAULT_CELL_HEIGHT = 20
"""探测不到像素尺寸时的兜底（VT340 口径，同 textual-image）。"""

TMUX_PASSTHROUGH_CMD = "tmux set -g allow-passthrough on"
"""给用户的可执行命令：让 kitty 的 ``ESC Ptmux;`` 能穿过外层 tmux。"""

FEATURE_QUERY_TIMEOUT = 0.4
"""kitty 特性查询的回执超时。正常终端毫秒级；0.4 s 只兜「问了不回」。"""

_PLACEHOLDER_TERMS = ("xterm-kitty", "xterm-ghostty")
"""不做特性查询也能确认支持 ``U=1`` 的终端。

kitty 0.28+ 就有占位符、0.50 才有 ``e=T`` 查询，所以老 kitty 只能靠 TERM 认；
Ghostty 实现了完整 kitty 图形协议（含占位符）。WezTerm / Konsole 会答 ``;OK``
但画不出 ``textual-image`` 的占位符（见 textual-image 源码注释），故意不列。
"""


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

    tmux: bool = False
    """是否跑在 tmux / screen 里（决定 Sixel 能不能用、提示怎么写）。"""

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


# ---------- tmux / 占位符：两条「+ 墙」的判据 ----------


def inside_tmux(env: Mapping[str, str] | None = None) -> bool:
    """当前进程是否跑在别人的 tmux / screen 里。

    判据复用 :func:`myrtui.termenv.outer_multiplexer`：``$TMUX`` 最硬，但 UU
    会主动 unset 它，所以还要看 ``$TERM_PROGRAM`` / ``$TERM``。
    """
    return termenv.outer_multiplexer(os.environ if env is None else env) is not None


def tmux_passthrough(env: Mapping[str, str] | None = None) -> bool | None:
    """外层 tmux 的 ``allow-passthrough`` 是否开启；问不到返回 ``None``。

    只问 ``$TMUX`` 指的那个 server（不猜 socket）；``$TMUX`` 被 unset 时直接
    ``None``——这种情况交给特性查询本身判断（查得到就说明穿得过去）。
    """
    socket = termenv.parse_tmux_env((os.environ if env is None else env).get("TMUX"))
    if socket is None:
        return None
    try:
        proc = subprocess.run(
            ["tmux", "-S", socket, "show", "-gv", "allow-passthrough"],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip().lower() in ("on", "all")


def trusted_placeholder_term(env: Mapping[str, str] | None = None) -> bool:
    """终端家族本身就能确认「会画 ``U=1`` 占位符」（查不到特性时用）。"""
    values = os.environ if env is None else env
    term = (values.get("TERM") or "").strip().lower()
    if term in _PLACEHOLDER_TERMS:
        return True
    if values.get("KITTY_WINDOW_ID"):
        return True
    return (values.get("TERM_PROGRAM") or "").strip().lower() == "ghostty"


def parse_placeholder_reply(reply: str | None) -> bool | None:
    """kitty 特性查询回执 → 是否支持 Unicode 占位符（``U=1``）。

    - ``\\x1b_Gi=31,e=T,C=1,U=1;OK\\x1b\\\\`` → ``True``
    - ``\\x1b_Gi=31,e=T;OK\\x1b\\\\``（认 ``e`` 但不带 ``U``）→ ``False``
    - ``\\x1b_Gi=31;OK\\x1b\\\\``（老终端 / 半截实现）→ ``None``（问不出来）
    """
    if not reply:
        return None
    for match in re.finditer(r"\x1b_G([^\x1b]*)\x1b\\", reply):
        keys = match.group(1).split(";", 1)[0]
        if "e=T" not in keys:
            continue
        return "U=1" in keys
    return None


def placeholder_support(timeout: float = FEATURE_QUERY_TIMEOUT) -> bool | None:
    """问终端：你画不画 ``U+10EEEE`` 占位符（kitty 特性查询 ``e=T,U=1``）？

    返回 ``True`` / ``False``；**问不出来**（非 tty、超时、依赖缺失、老终端）
    返回 ``None``。最坏耗时 ``timeout``，正常毫秒级。

    注意：``textual-image`` 的 TGP 探测只问「支持图形协议吗」，答 ``;OK`` 的
    终端可能根本画不了占位符（Konsole / WezTerm / 手机 app 都有实例）。
    """
    try:
        from textual_image._terminal import (  # noqa: PLC0415 —— 可选依赖
            capture_mode,
            prepare_terminal_sequence,
            read,
        )
    except Exception:  # noqa: BLE001 —— 无位图依赖时整段跳过
        return None
    if not (sys.__stdout__ and sys.__stdin__):
        return None
    try:
        if not (sys.__stdout__.isatty() and sys.__stdin__.isatty()):
            return None
    except Exception:  # noqa: BLE001 —— 极端环境（fd 已关）
        return None

    query = prepare_terminal_sequence(
        f"\x1b_Gi={int(time.time() * 1000) & 0xFFFF},a=q,e=T,U=1\x1b\\"
    )
    reply = ""
    deadline = time.monotonic() + max(0.05, timeout)
    try:
        fd = sys.__stdin__.buffer.fileno()
        with capture_mode():
            sys.__stdout__.write(query)
            sys.__stdout__.flush()
            while time.monotonic() < deadline:
                if parse_placeholder_reply(reply) is not None:
                    break
                reply += read(fd, 1, max(0.01, deadline - time.monotonic()))
    except Exception:  # noqa: BLE001 —— 超时 / fd 竞态只意味着「不知道」
        log.debug("kitty 特性查询失败", exc_info=True)
    return parse_placeholder_reply(reply)


def detect(
    env: str | None = None,
    *,
    probe: Callable[[], object] | None = None,
    tty: bool | None = None,
    loader: Callable[[], Callable[[], object] | None] | None = None,
    tmux: bool | None = None,
    placeholders: Callable[[], bool | None] | None = None,
    passthrough: Callable[[], bool | None] | None = None,
) -> tuple[Capabilities, str | None]:
    """探测能力 → ``(capabilities, warning)``。

    ``probe`` / ``tty`` / ``loader`` / ``tmux`` / ``placeholders`` /
    ``passthrough`` 全部供测试注入；生产只用默认值。
    """
    mode, warning = _normalize_mode(
        os.environ.get(ENV_NAME) if env is None else env
    )
    in_tmux = inside_tmux() if tmux is None else tmux

    if mode in ("off", "unicode"):
        # 显式关闭不探测终端；仍记录依赖是否可导入，便于提示 / 日志区分
        # 「用户关掉的」与「依赖没装」。
        if probe is None:
            probe = (loader or _load_probe)()
        return (
            Capabilities(mode=mode, available=probe is not None, tmux=in_tmux),
            warning,
        )

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
            return Capabilities(mode="unicode", tmux=in_tmux), warning
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
        return (
            Capabilities(available=available, mode="unicode", tmux=in_tmux),
            warning,
        )

    try:
        result = probe()
    except Exception:  # noqa: BLE001 —— 探测失败只意味着回落
        log.warning("终端位图探测失败，回落到 Unicode", exc_info=True)
        return (
            Capabilities(available=available, mode="unicode", tmux=in_tmux),
            warning,
        )

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

    # tmux 会把 pane 里的 sixel 换成 ``SIXEL IMAGE (WxH)`` + 一整屏 ``+``，且
    # 无论 client 怎么声明都转不出去（实测 tmux 3.8）——所以 auto 下直接丢掉。
    if sixel and in_tmux:
        if mode == "sixel":
            warning = warning or t("gfx.tmux_sixel_forced", env=ENV_NAME)
        else:
            sixel = False
            warning = warning or t("gfx.tmux_sixel_blocked")

    if tgp:
        # textual-image 的 TGP 走 Unicode 占位符：终端不实现 ``U=1`` 就是满屏
        # 字符。查不到（老终端 / 半截实现）时只信 kitty / Ghostty。
        support = (placeholders or placeholder_support)()
        if support is False or (support is None and not trusted_placeholder_term()):
            if mode == "tgp":
                warning = warning or t("gfx.tgp_unverified", env=ENV_NAME)
            else:
                tgp = False
                warning = warning or t("gfx.tgp_no_placeholders")
        elif in_tmux and (passthrough or tmux_passthrough)() is False:
            if mode == "tgp":
                warning = warning or t("gfx.tmux_passthrough_forced", env=ENV_NAME)
            else:
                tgp = False
                warning = warning or t(
                    "gfx.tmux_passthrough_off", env=ENV_NAME, cmd=TMUX_PASSTHROUGH_CMD
                )

    resolved = mode if (sixel or tgp) else "unicode"
    capabilities = Capabilities(
        sixel=sixel,
        tgp=tgp,
        cell_width=cell_width,
        cell_height=cell_height,
        available=True,
        mode=resolved,
        tmux=in_tmux,
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
    suffix = " (tmux)" if capabilities.tmux else ""
    if not capabilities.bitmaps:
        text = f"graphics={capabilities.mode}"
        if capabilities.mode != "off" and not capabilities.available:
            text += t("gfx.missing_dep_short")
        return text + suffix
    return (
        f"graphics={capabilities.protocol} "
        f"cell={capabilities.cell_width}x{capabilities.cell_height}{suffix}"
    )


INSTALL_HINT = "pipx inject myrtui textual-image pillow"


def blocked_reason(capabilities: Capabilities, env: str | None = None) -> str:
    """位图不可用时的可执行提示。

    四种原因必须分开说，否则用户会去查终端（v0.3.0 的真实踩坑）：
    ``MYRTUI_GRAPHICS`` 显式关掉、缺 Python 依赖、**外层 tmux 挡住了位图**、
    终端真的没有 Sixel/TGP。
    """
    raw = os.environ.get(ENV_NAME) if env is None else env
    mode = (raw or "").strip().lower() or "auto"
    if mode in ("off", "unicode"):
        return t("gfx.mode_off", env=ENV_NAME, mode=mode)
    if not capabilities.available:
        return t("gfx.missing_dep_long", hint=INSTALL_HINT)
    if capabilities.tmux:
        return t("gfx.tmux_blocked", env=ENV_NAME, cmd=TMUX_PASSTHROUGH_CMD)
    return t("gfx.no_protocol")
