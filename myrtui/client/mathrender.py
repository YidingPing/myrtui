"""数学公式的 termtex 渲染后端（纯逻辑，不 import Textual）。

v0.36 恢复：终端数学只有一条增强路线——外部命令 ``termtex``（Go，
Apache-2.0，字符网格 2D 排版）：

- **块级**：``termtex -width <n>``，堆叠分式 / 矩阵 / cases；
- **行内**：``termtex -inline``，输出合并成单行（行内永不出多行）；
- **没有 termtex**：:class:`PlainRenderer` 原样显示 LaTeX 源码——不空白、
  不崩溃、不伪造 Unicode 字形。

两条硬约束不变：

1. **B1（PERF-B0B1 §B1）**：子进程只允许出现在 :func:`prewarm_document`
   ——调用方必须把它放进 ``asyncio.to_thread``。事件循环上的
   :func:`render_block_cached` / :func:`render_inline_cached` 只做缓存查表
   或纯字符串返回，**绝不 spawn**。
2. **D5（绝不丢内容）**：每条公式独立尝试；termtex 超时/非零退出/空输出
   都逐条回落 LaTeX 原文，失败公式同进程内不再重试（N3）。
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from collections import OrderedDict
from typing import Protocol

from myrtui.client.mathtext import MathText, is_probably_math, math_parser_factory

log = logging.getLogger("myrtui.mathrender")

ENV_NAME = "MYRTUI_MATH_BACKEND"
"""``auto``（默认）/ ``plain`` / ``termtex``；``builtin`` 是 ``plain`` 的
历史别名。非法值告警后回落 ``auto``。"""

CACHE_SIZE = 512
TIMEOUT_SECONDS = 1.5

INLINE_KEY_WIDTH = 0
"""行内公式的缓存键宽度（块级宽度 >= ``MIN_WIDTH``，不会撞键）。"""

WIDTH_MARGIN = 8
"""预览区左右 padding（4）+ 滚动条/安全余量（4）。"""

MIN_WIDTH = 20
MAX_WIDTH = 120

SANITY_SOURCE = "1+1"
SANITY_EXPECTED = "1+1"
"""探测口径守门：输出变形（上游漂移）即整体回落。

真实 termtex 按 TeX atom 间距规则输出 ``1 + 1``（不是 ``1+1``），比较前
把空白全部压掉再比（2026-10-25 真机实测：留空白比较会让探测永远失败）。
"""

_BLOCK_TOKEN_TYPES = ("math_block", "math_block_label")
_INLINE_TOKEN_TYPES = ("math_inline", "math_inline_double")

# ``\tag{...}`` / ``\tag*{...}``：termtex 不支持（README 只声明
# ``\notag`` / ``\label{}`` 被忽略），调用前剥掉、由 MathBlock 后注入
# ``(tag)``——与 v0.22 口径一致（V23 方案 §5.4 / Q2）。
_TAG = re.compile(r"\\tag\*?\s*\{([^{}]*)\}")

# termtex 不认的装饰命令（真机实测会被当成普通文本输出）：
# - 旧式字体开关 ``\rm`` / ``\bf`` …（会原样打出 "rm"）；
# - 颜色命令（会原样打出 "textcolor red"）。
# 这些在纯文本输出里本来也没有意义，发给 termtex 前直接剥掉。
_FONT_SWITCHES = re.compile(
    r"\\(?:rm|bf|it|sf|tt|cal|bfseries|mdseries|rmfamily|sffamily|"
    r"ttfamily|upshape|itshape|slshape|scshape)(?![A-Za-z])"
)
_COLOR_ARG = re.compile(r"\\(?:textcolor|colorbox)\s*\{[^{}]*\}\s*")
_COLOR_SWITCH = re.compile(r"\\color\s*\{[^{}]*\}")

# ``\boxed`` / ``\fbox`` / ``\framebox``：termtex 不支持，会退化成
# ``boxed E = mc²``。改用 ``\left[ … \right]`` 表达「框」的语义。
_BOX_COMMAND = re.compile(r"\\(?:boxed|fbox|framebox)(?![A-Za-z])")

# 行内会输出两行的堆叠命令（``\overset{a}{b}`` 实测 → "a\nb"）。
# 行内合并会把上下标压成同一行、语义变糊，改为可读的 TeX 上下标形式：
# ``\overset{a}{b}`` → ``b^{a}``、``\underset{a}{b}`` → ``b_{a}``。
_STACK_COMMAND = re.compile(r"\\(overset|underset|stackrel)(?![A-Za-z])")

_SPACES = re.compile(r"[ \t]{2,}")


def _squash(text: str) -> str:
    return "".join(text.split())


def _read_balanced(source: str, index: int) -> tuple[str, int]:
    """``index`` 必须指向 ``{``；返回组内文本与结束位置（支持嵌套/转义）。"""
    depth = 0
    start = index + 1
    while index < len(source):
        char = source[index]
        if char == "\\":
            index += 2
            continue
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index], index + 1
        index += 1
    return source[start:], len(source)


def _rewrite_boxes(source: str) -> str:
    """``\\boxed{X}`` / ``\\fbox{X}`` → ``\\left[ X \\right]``（保持嵌套）。"""
    pieces: list[str] = []
    index = 0
    while True:
        match = _BOX_COMMAND.search(source, index)
        if match is None:
            pieces.append(source[index:])
            break
        pieces.append(source[index : match.start()])
        cursor = match.end()
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor >= len(source) or source[cursor] != "{":
            pieces.append(match.group(0))
            index = match.end()
            continue
        body, index = _read_balanced(source, cursor)
        pieces.append(r"\left[ " + _rewrite_boxes(body) + r" \right]")
    return "".join(pieces)


def _rewrite_stacks(source: str) -> str:
    """行内堆叠命令 → 可读上下标（保持嵌套）。"""
    pieces: list[str] = []
    index = 0
    while True:
        match = _STACK_COMMAND.search(source, index)
        if match is None:
            pieces.append(source[index:])
            break
        pieces.append(source[index : match.start()])
        cursor = match.end()
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor >= len(source) or source[cursor] != "{":
            pieces.append(match.group(0))
            index = match.end()
            continue
        script, cursor = _read_balanced(source, cursor)
        while cursor < len(source) and source[cursor].isspace():
            cursor += 1
        if cursor >= len(source) or source[cursor] != "{":
            # 缺第二个参数：原样保留（绝不吞内容）
            pieces.append(source[match.start() : cursor])
            index = cursor
            continue
        base, index = _read_balanced(source, cursor)
        script = _rewrite_stacks(script)
        base = _rewrite_stacks(base)
        marker = "_{" if match.group(1) == "underset" else "^{"
        # termtex 的 parser 要求上下标有「base」；`=^{def}` 会报
        # "missing base before ^"（真机实测）。统一包成 `(=)^{def}`，
        # 关系符/运算符也能安全降级（渲染为 `(=)ᵈᵉᶠ`）。
        pieces.append(f"({base}){marker}{script}}}")
    return "".join(pieces)


def normalize_source(source: str) -> str:
    """termtex 调用前的兼容层（依赖 2026-10-25 真机实测结论）。

    - ``\\rm`` 等旧式字体开关、``\\color``/``\\textcolor``：termtex 会把命令名
      原样打印成文本（实测 ``x^{\\rm GCRF}`` → 上标区出现字面 "rm GCRF"）→ 剥掉；
    - ``\\boxed{X}`` → ``\\left[ X \\right]``：termtex 不支持框命令，会把
      ``\\boxed`` 变成字面 "boxed"（实测）→ 改用可渲染的方括号；
    - ``\\tag`` 已由 :func:`split_tag` 在上层剥掉。
    """
    text = _COLOR_ARG.sub(" ", source)
    text = _COLOR_SWITCH.sub(" ", text)
    text = _FONT_SWITCHES.sub(" ", text)
    text = _SPACES.sub(" ", text)
    return _rewrite_boxes(text)


def normalize_inline_source(source: str) -> str:
    """行内兼容层：在 :func:`normalize_source` 之上处理堆叠命令。"""
    return _rewrite_stacks(normalize_source(source))


class MathRenderError(RuntimeError):
    """单条公式的后端渲染失败（调用方据此回落 LaTeX 原文）。"""


class MathRenderer(Protocol):
    name: str

    def render_block(self, latex: str, *, width: int = 0) -> str: ...

    def render_inline(self, latex: str) -> str: ...


class PlainRenderer:
    """无 termtex 时的兜底：LaTeX 原文（绝不丢内容 / 不伪造字形）。"""

    name = "plain"

    def render_block(self, latex: str, *, width: int = 0) -> str:
        return latex

    def render_inline(self, latex: str) -> str:
        return latex


class TermtexRenderer:
    """termtex 子进程封装（表达式走 stdin，规避 argv ``-`` 歧义）。"""

    name = "termtex"

    def __init__(self, path: str, *, timeout: float = TIMEOUT_SECONDS) -> None:
        self._path = path
        self._timeout = timeout

    @classmethod
    def probe(cls) -> "TermtexRenderer | None":
        """``which`` + sanity render；失败返回 ``None``（不自动安装）。"""
        path = shutil.which("termtex")
        if not path:
            return None
        renderer = cls(path)
        try:
            output = renderer._run(SANITY_SOURCE)
        except Exception:  # noqa: BLE001 —— 探测失败只意味着回落原文
            log.warning(
                "termtex 探测失败（sanity render 异常），公式将显示 LaTeX 原文",
                exc_info=True,
            )
            return None
        if _squash(output) != _squash(SANITY_EXPECTED):
            log.warning(
                "termtex 探测失败（sanity 输出 %r，期望 %r），公式将显示原文",
                output,
                SANITY_EXPECTED,
            )
            return None
        log.debug("termtex 可用：%s", path)
        return renderer

    def _run(self, latex: str, *, width: int = 0, inline: bool = False) -> str:
        command = [self._path]
        if inline:
            command.append("-inline")
        if width > 0:
            command += ["-width", str(width)]
        try:
            completed = subprocess.run(  # noqa: S603 —— 固定可执行 + stdin
                command,
                input=latex.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self._timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise MathRenderError(f"termtex 超时（{self._timeout}s）") from exc
        if completed.returncode != 0:
            detail = completed.stderr.decode("utf-8", "replace").strip()
            raise MathRenderError(
                f"termtex 退出码 {completed.returncode}: {detail[:200]}"
            )
        return completed.stdout.decode("utf-8", "replace")

    def render_block(self, latex: str, *, width: int = 0) -> str:
        text = _clean_output(
            self._run(normalize_source(latex), width=width)
        )
        if not text.strip():
            raise MathRenderError("termtex 输出为空")
        return text

    def render_inline(self, latex: str) -> str:
        text = _clean_output(self._run(normalize_inline_source(latex), inline=True))
        # 行内永不出多行：逐行 strip 后用单个空格合并（plan §2.1）。
        merged = " ".join(part.strip() for part in text.split("\n") if part.strip())
        if not merged:
            raise MathRenderError("termtex -inline 输出为空")
        return merged


class _Cache:
    """进程级 LRU：键 ``(latex, width)``；另记失败过的键（N3）。"""

    def __init__(self, size: int = CACHE_SIZE) -> None:
        self._size = size
        self._data: OrderedDict[tuple[str, int], str] = OrderedDict()
        self._failed: set[tuple[str, int]] = set()
        self._lock = threading.Lock()

    def get(self, key: tuple[str, int]) -> str | None:
        with self._lock:
            value = self._data.get(key)
            if value is None:
                return None
            self._data.move_to_end(key)
            return value

    def put(self, key: tuple[str, int], value: str) -> None:
        with self._lock:
            self._data[key] = value
            self._data.move_to_end(key)
            while len(self._data) > self._size:  # pragma: no branch —— 容量护栏
                self._data.popitem(last=False)

    def failed(self, key: tuple[str, int]) -> bool:
        with self._lock:
            return key in self._failed

    def mark_failed(self, key: tuple[str, int]) -> None:
        with self._lock:
            self._failed.add(key)

    def clear(self) -> None:
        with self._lock:
            self._data.clear()
            self._failed.clear()


_CACHE = _Cache()

_probe_lock = threading.Lock()
_probed = False
_probe_result: TermtexRenderer | None = None


def probe_termtex() -> TermtexRenderer | None:
    """进程内只探测一次（``which`` + sanity render）。"""
    global _probed, _probe_result
    with _probe_lock:
        if not _probed:
            _probe_result = TermtexRenderer.probe()
            _probed = True
        return _probe_result


def select_backend(env: str | None = None) -> tuple[MathRenderer, str | None]:
    """解析 ``MYRTUI_MATH_BACKEND`` → ``(renderer, warning)``。

    - ``plain``（历史别名 ``builtin``）：强制 LaTeX 原文，不探测；
    - ``auto``：探测到 termtex 就用，否则原文（静默）；
    - ``termtex``：指定外部后端，找不到时告警 + 原文；
    - 非法值：告警 + 回落 ``auto``（同 ``MYRTUI_IDLE_INTERVAL`` 惯例）。
    """
    raw = os.environ.get(ENV_NAME, "") if env is None else env
    mode = (raw or "").strip().lower()
    warning: str | None = None
    if mode not in ("", "auto", "plain", "builtin", "termtex"):
        warning = f"{ENV_NAME}={raw!r} 非法，已回落 auto"
        log.warning(warning)
        mode = "auto"
    if mode == "":
        mode = "auto"

    if mode in ("plain", "builtin"):
        return PlainRenderer(), warning

    renderer = probe_termtex()
    if renderer is not None:
        return renderer, warning
    if mode == "termtex":
        warning = warning or (
            f"{ENV_NAME}=termtex，但未找到可用的 termtex，"
            "公式将显示 LaTeX 原文"
        )
        log.warning(warning)
    return PlainRenderer(), warning


def clamp_width(app_width: int) -> int:
    """终端宽度 → termtex 目标宽度（``-width``）。

    ``MIN_WIDTH`` 取 20 而不是 40：极窄终端下硬垫到 40 会让输出超出
    MathBlock 内宽、被 ``text-wrap`` 硬换行，反而打散对齐（V23 Q3）。
    """
    try:
        available = int(app_width) - WIDTH_MARGIN
    except (TypeError, ValueError):
        available = MIN_WIDTH
    return max(MIN_WIDTH, min(available, MAX_WIDTH))


def _clean_output(text: str) -> str:
    """rstrip 每行、去首尾空行、压缩连续空行（§5.4：不美化）。"""
    lines = [
        line.rstrip()
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    while lines and not lines[-1]:
        lines.pop()
    while lines and not lines[0]:
        lines.pop(0)
    cleaned: list[str] = []
    for line in lines:
        if not line and cleaned and not cleaned[-1]:
            continue
        cleaned.append(line)
    return "\n".join(cleaned)


def split_tag(latex: str) -> tuple[str, str | None]:
    """剥掉 ``\\tag{...}`` → ``(body, tag 文本)``。"""
    match = _TAG.search(latex)
    if match is None:
        return latex, None
    body = f"{latex[: match.start()]} {latex[match.end() :]}".strip()
    tag = match.group(1).strip()
    return body, (tag or None)


def render_block_cached(latex: str, *, width: int) -> MathText:
    """块级公式渲染（**禁止 spawn**，任何线程/事件循环上安全）。

    缓存命中（预渲染写入）→ termtex 二维文本；miss → LaTeX 原文
    （resize 后宽度变了即 miss，见 D6 已知限制 N4）。
    """
    body, tag = split_tag(latex)
    if not body:
        # 只有 ``\tag{1}``：沿用「遇空保原文」语义，别把源文吞掉。
        return MathText(latex, tag)
    cached = _CACHE.get((body, width))
    text = cached if cached is not None else PlainRenderer().render_block(body)
    return MathText(text, tag)


def render_inline_cached(latex: str) -> str:
    """行内公式渲染（**禁止 spawn**）：缓存命中用 termtex，miss 用原文。"""
    cached = _CACHE.get((latex, INLINE_KEY_WIDTH))
    if cached is not None:
        return cached
    return PlainRenderer().render_inline(latex)


def sole_inline_math(children) -> str | None:
    """段落里**只有**一个行内公式（允许两侧空白）→ 公式源；否则 ``None``。

    独立成段的 ``$x^2$`` 在阅读上就是块级公式（Tabby 的行内位图不可行），
    预览层据此升级成 :class:`MathBlock`，预渲染也用同一规则保持同源。
    """
    if not children:
        return None
    meaningful = [
        child
        for child in children
        if not (child.type == "text" and not child.content.strip())
    ]
    if len(meaningful) != 1:
        return None
    child = meaningful[0]
    if child.type not in _INLINE_TOKEN_TYPES:
        return None
    source = (child.content or "").strip()
    if not source or not is_probably_math(source):
        return None
    return source


def _math_sources(text: str) -> tuple[list[str], list[str]]:
    """从 markdown 源里抽取块级 / 行内公式正文（token 流与预览同源）。"""
    if "$" not in text:
        return [], []
    tokens = math_parser_factory().parse(text)
    blocks: list[str] = []
    inlines: list[str] = []
    for token in tokens:
        if token.type in _BLOCK_TOKEN_TYPES and token.content:
            blocks.append(token.content)
        if token.type == "inline" and token.children:
            for child in token.children:
                if (
                    child.type in _INLINE_TOKEN_TYPES
                    and child.content
                    and is_probably_math(child.content)
                ):
                    inlines.append(child.content)
    return blocks, inlines


def block_sources(text: str) -> list[str]:
    """文档里的块级公式正文（与预览 token 流同源，供位图预渲染用）。"""
    blocks, _inlines = _math_sources(text)
    return blocks


def prewarm_document(text: str, width: int) -> str | None:
    """worker 专用：探测后端 + 把整篇文档的公式渲染进缓存。

    返回要提示用户的告警（无则 ``None``）。单条失败只标记该公式
    不再重试，不影响其余公式（D5 / N3）。
    """
    if "$" not in text:
        # 没有公式的文档连探测都不做：不给普通 md 预览加子进程
        return None
    renderer, warning = select_backend()
    if not isinstance(renderer, TermtexRenderer):
        return warning
    blocks, inlines = _math_sources(text)
    if not blocks and not inlines:
        return warning

    rendered = 0
    for source in blocks:
        body, _tag = split_tag(source)
        key = (body, width)
        if not body or _CACHE.failed(key) or _CACHE.get(key) is not None:
            continue
        try:
            output = renderer.render_block(body, width=width)
        except Exception:  # noqa: BLE001 —— D5：坏一条不毁整篇
            _CACHE.mark_failed(key)
            log.debug(
                "termtex 块级渲染失败，显示原文：%r", body[:80], exc_info=True
            )
            continue
        _CACHE.put(key, output)
        rendered += 1

    for source in inlines:
        key = (source, INLINE_KEY_WIDTH)
        if _CACHE.failed(key) or _CACHE.get(key) is not None:
            continue
        try:
            output = renderer.render_inline(source)
        except Exception:  # noqa: BLE001 —— 同上：逐条回落
            _CACHE.mark_failed(key)
            log.debug(
                "termtex 行内渲染失败，显示原文：%r", source[:80], exc_info=True
            )
            continue
        _CACHE.put(key, output)
        rendered += 1

    log.debug(
        "math prewarm: %d/%d formulas via termtex",
        rendered,
        len(blocks) + len(inlines),
    )
    return warning


def reset_state() -> None:
    """测试钩子：清探测结果 + 缓存 + 失败标记（生产不调用）。"""
    global _probed, _probe_result
    with _probe_lock:
        _probed = False
        _probe_result = None
    _CACHE.clear()
