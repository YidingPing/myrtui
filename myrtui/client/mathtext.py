"""Markdown 数学公式的 parser / token 助手（纯逻辑，不 import Textual）。

v0.36 恢复 v0.27 的最小面，并补上 LaTeX 原生定界符归一化：

- :class:`MathText`（``text`` + ``tag`` 数据对象）；
- :func:`math_parser_factory`（GFM-like + dollarmath，Markdown token 流与
  termtex 预渲染共用）；
- :func:`is_probably_math`（``$5 and $10`` 货币闸门）；
- :func:`inline_math_tokens`（行内 token → 纯文本 token，渲染函数可注入）；
- :func:`polish_inline_text`（termtex 行内产物的收尾：去 ``_{...}`` 括号等）；
- :func:`normalize_math_delimiters`（``\\(...\\)`` / ``\\[...\\]`` →
  ``$...$`` / ``$$...$$``，跳过代码）。

终端数学只有一条增强路线：外部 ``termtex``；没有 termtex 时公式按 LaTeX
原文显示（``mathrender.PlainRenderer``），绝不空白 / 崩溃 / 伪造字形。
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Callable

from markdown_it import MarkdownIt
from markdown_it.token import Token as MarkdownToken
from mdit_py_plugins.dollarmath import dollarmath_plugin


@dataclass(frozen=True)
class MathText:
    """一次块级公式渲染的结果。"""

    text: str
    tag: str | None = None


def is_probably_math(source: str) -> bool:
    """区分 ``$GM_\\odot$`` 与 ``$5 and $10`` 这类货币/散文。

    ``dollarmath`` 默认允许 ``$`` 内侧有空格；这对 ``$ x $`` 友好，但也会把
    ``$5 and $10`` 误判成公式。这里用保守规则：没有命令/运算符、且内部有
    空格的，按散文还原成字面 ``$...$``。
    """
    text = source.strip()
    if not text:
        return False
    if any(char in text for char in "\\^_=+*/<>{}[]()"):
        return True
    return not any(char.isspace() for char in text)


def math_parser_factory() -> MarkdownIt:
    """给 Textual ``Markdown`` 用的 parser：GFM + dollar math。"""
    parser = MarkdownIt("gfm-like")
    parser.use(dollarmath_plugin, allow_space=True, allow_digits=True)
    return parser


def _plain_inline(source: str) -> str:
    """默认行内渲染：LaTeX 原文（无 termtex 时绝不丢内容）。"""
    return source


_BRACED_SUBSUP = re.compile(r"([_^])\{([A-Za-z0-9]+)\}")
_MULTI_SPACE = re.compile(r"[ \t]{2,}")


def polish_inline_text(text: str) -> str:
    """行内公式的「可读性收尾」。

    ``termtex -inline`` 会把下标原样保留成 ``σ_{floor}``——在终端里看起来像
    LaTeX 源码而不是数学。这里只做**无歧义**的收尾：

    - 单层 ``_{abc}`` / ``^{abc}`` → ``_abc`` / ``^abc``（仅当组内是纯字母
      数字；``a_{i+j}`` 这类保留括号，去掉会歧义）；
    - 连续空格压成一个（termtex 偶尔多吐空格）。

    不动上下标本身、不动 Unicode 字形，保证「不伪造」也不丢信息。
    """
    polished = _BRACED_SUBSUP.sub(r"\1\2", text)
    polished = _MULTI_SPACE.sub(" ", polished)
    return polished.strip()


def inline_math_tokens(
    token: MarkdownToken,
    render: Callable[[str], str] | None = None,
) -> list[MarkdownToken]:
    """``math_inline`` → 纯文本 token；散文/货币则还原成字面 ``$...$``。

    v0.36 实测反馈：行内公式包成 ``em_open`` / ``em_close`` 会整段变斜体，
    和正文风格冲突、反而不易读。这里只输出普通 ``text`` token，让公式与
    正文同色同字体；上下标 / 希腊字母由 termtex 的 Unicode 字形表达。

    ``render`` 由调用方注入（预览层传 ``mathrender.render_inline_cached``）。
    默认按 LaTeX 原文输出，保证纯逻辑单测与「没有 termtex」时行为一致。
    """
    source = token.content
    markup = token.markup or "$"
    if not is_probably_math(source):
        literal = MarkdownToken("text", "", 0)
        literal.content = f"{markup}{source}{markup}"
        return [literal]

    provided = render is not None
    rendered = (render or _plain_inline)(source)
    # 行内永不出多行：换行合并成空格。
    rendered = rendered.replace("\r\n", "\n").replace("\r", "\n")
    rendered = " ".join(part.strip() for part in rendered.split("\n")).strip()
    if provided:
        # 只对「已渲染成 Unicode」的产物收尾；原文回落时保持 LaTeX 不动
        rendered = polish_inline_text(rendered)
    if not rendered:
        literal = MarkdownToken("text", "", 0)
        literal.content = f"{markup}{source}{markup}"
        return [literal]

    text = MarkdownToken("text", "", 0)
    text.content = rendered
    return [text]


# ---------------------------------------------------------------------------
# LaTeX 原生定界符归一化（v0.36 新增）
# ---------------------------------------------------------------------------

_FENCE = re.compile(r" {0,3}(`{3,}|~{3,})")


def _escaped(text: str, index: int) -> bool:
    """``text[index]`` 是否被奇数个反斜杠转义。"""
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _find_closing(text: str, start: int, delimiter: str) -> int | None:
    """从 ``start`` 起找未转义的 ``\\)`` / ``\\]``；找不到返回 ``None``。"""
    cursor = start
    while True:
        found = text.find(delimiter, cursor)
        if found < 0:
            return None
        if not _escaped(text, found):
            return found
        cursor = found + 1


def _line_end(text: str, index: int) -> int:
    """返回 ``index`` 所在行的换行后位置（无换行则返回 ``len(text)``）。"""
    found = text.find("\n", index)
    return len(text) if found < 0 else found + 1


def normalize_math_delimiters(text: str) -> str:
    """把 ``\\(...\\)`` / ``\\[...\\]`` 归一化成 ``$...$`` / ``$$...$$``。

    只替换定界符，不解析 / 改写公式内容：

    - fenced code（``` / ~~~）整段跳过；
    - 行内 code（`` `...` ``）整段跳过；
    - ``\\\\(`` / ``\\\\[``（转义后的反斜杠）不误伤；
    - 找不到闭合定界符时原样保留，绝不吞内容。

    背景：``termtex -md`` 只认 ``$`` / ``$$``，而 LLM / LaTeX 导出的笔记大量
    使用 ``\\(...\\)`` / ``\\[...\\]``。归一化后同一份文本同时喂给 dollarmath
    和 termtex，口径一致。
    """
    if "\\(" not in text and "\\[" not in text:
        return text

    out: list[str] = []
    index = 0
    length = len(text)
    fence: tuple[str, int] | None = None

    while index < length:
        line_start = index == 0 or text[index - 1] == "\n"

        if fence is not None:
            if line_start:
                match = _FENCE.match(text, index)
                if (
                    match is not None
                    and match.group(1)[0] == fence[0]
                    and len(match.group(1)) >= fence[1]
                ):
                    end = _line_end(text, index)
                    out.append(text[index:end])
                    index = end
                    fence = None
                    continue
            end = _line_end(text, index)
            out.append(text[index:end])
            index = end
            continue

        if line_start:
            match = _FENCE.match(text, index)
            if match is not None:
                end = _line_end(text, index)
                out.append(text[index:end])
                marker = match.group(1)
                fence = (marker[0], len(marker))
                index = end
                continue

        char = text[index]

        # 行内 code：整段（含闭合反引号）原样复制。
        if char == "`":
            run_end = index
            while run_end < length and text[run_end] == "`":
                run_end += 1
            run = text[index:run_end]
            closing = text.find(run, run_end)
            if closing < 0:
                out.append(run)
                index = run_end
                continue
            end = closing + len(run)
            out.append(text[index:end])
            index = end
            continue

        if char == "\\":
            following = text[index + 1] if index + 1 < length else ""
            # 转义反斜杠：复制 ``\\``，后面的字符按普通字符处理。
            if following == "\\":
                out.append(text[index : index + 2])
                index += 2
                continue
            if following in "([":
                delimiter = "\\)" if following == "(" else "\\]"
                closing = _find_closing(text, index + 2, delimiter)
                if closing is not None:
                    marker = "$" if following == "(" else "$$"
                    out.append(marker)
                    out.append(text[index + 2 : closing])
                    out.append(marker)
                    index = closing + 2
                    continue

        out.append(char)
        index += 1

    return "".join(out)
