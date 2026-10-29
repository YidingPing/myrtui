"""myrtui v0.36 测试：Markdown 全屏预览 + termtex Unicode 公式。

覆盖：

- ``fileview`` 读盘 / 解码 / 二进制 / 截断 / 缺失；
- ``mathtext`` 货币闸门 / dollarmath / ``\\(...\\)`` 归一化 / inline token；
- ``mathrender`` termtex 探测 / 缓存 / 逐条回落 / 宽度 clamp；
- ``mdpreview`` 渲染 / 公式块 / 链接拦截 / 焦点 / 重载 / 双重大小闸门；
- 菜单 ``preview`` 项文案；
- 侧栏快捷键文案防漂移（P4）。
"""
from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from textual.widgets import OptionList, Static

import myrtui.fileview as fileview
import myrtui.client.mathrender as mathrender
from myrtui.client import mdpreview
from myrtui.client.app import TerminalApp
from myrtui.client.context_menu import ContextMenuScreen
from myrtui.client.file_tree import FileTree
from myrtui.client.mdpreview import (
    MARKDOWN_PREVIEW_MAX_BYTES,
    MathBlock,
    MarkdownPreviewScreen,
)
from myrtui.client.mathtext import (
    inline_math_tokens,
    is_probably_math,
    math_parser_factory,
    normalize_math_delimiters,
    polish_inline_text,
)
from myrtui.client.shortcuts import SECTIONS
from myrtui.tmux_backend import CaptureResult, PaneInfo


@pytest.fixture(autouse=True)
def _isolate_math(monkeypatch):
    """不探测开发机上的真 termtex；每个用例从干净缓存开始。"""
    mathrender.reset_state()
    monkeypatch.setattr(mathrender, "probe_termtex", lambda: None)
    yield
    mathrender.reset_state()


# ---------- fileview ----------


def test_fileview_reads_utf8_and_normalizes_newlines(tmp_path):
    target = tmp_path / "a.md"
    target.write_bytes("# 标题\r\n\r\n正文\r\n".encode("utf-8"))
    preview = fileview.read_for_preview(target)
    assert preview.error is None
    assert preview.binary is False
    assert preview.text == "# 标题\n\n正文\n"
    assert preview.lines == 3
    assert preview.encoding == "utf-8"


def test_fileview_strips_bom(tmp_path):
    target = tmp_path / "bom.md"
    target.write_bytes(b"\xef\xbb\xbfhello")
    preview = fileview.read_for_preview(target)
    assert preview.text == "hello"
    assert preview.encoding == "utf-8-sig"


def test_fileview_decodes_invalid_utf8_with_replacement(tmp_path):
    target = tmp_path / "bad.md"
    target.write_bytes(b"abc\xffdef")
    preview = fileview.read_for_preview(target)
    assert preview.error is None
    assert "abc" in preview.text
    assert "\ufffd" in preview.text
    assert preview.encoding.endswith("replace") or preview.encoding == "utf-8-replace"


def test_fileview_detects_binary(tmp_path):
    target = tmp_path / "bin.dat"
    target.write_bytes(b"abc\x00def")
    preview = fileview.read_for_preview(target)
    assert preview.binary is True
    assert preview.text == ""
    assert preview.error is None


def test_fileview_truncates_at_complete_line(tmp_path):
    target = tmp_path / "big.md"
    target.write_text("a" * (fileview.MAX_PREVIEW_BYTES + 100), encoding="utf-8")
    preview = fileview.read_for_preview(target)
    assert preview.truncated is True
    assert len(preview.text.encode("utf-8")) <= fileview.MAX_PREVIEW_BYTES


def test_fileview_missing_and_directory(tmp_path):
    missing = fileview.read_for_preview(tmp_path / "nope.md")
    assert missing.error is not None and "不存在" in missing.error

    directory = fileview.read_for_preview(tmp_path)
    assert directory.error is not None and "普通文件" in directory.error


# ---------- mathtext ----------


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("x^2", True),
        (r"\alpha", True),
        ("a + b", True),
        ("5 and", False),
        ("", False),
        ("   ", False),
    ],
)
def test_is_probably_math(source, expected):
    assert is_probably_math(source) is expected


def test_parser_recognises_dollar_math():
    tokens = math_parser_factory().parse("$x^2$ and\n\n$$\na+b\n$$\n")
    inline = [
        child
        for token in tokens
        if token.type == "inline" and token.children
        for child in token.children
        if child.type == "math_inline"
    ]
    blocks = [token for token in tokens if token.type == "math_block"]
    assert [child.content for child in inline] == ["x^2"]
    assert len(blocks) == 1 and "a+b" in blocks[0].content


def test_parser_ignores_math_in_code_fence():
    tokens = math_parser_factory().parse("```\n$x$\n```\n")
    math = [
        child
        for token in tokens
        if token.type == "inline" and token.children
        for child in token.children
        if child.type.startswith("math_")
    ]
    assert math == []


def test_normalize_latex_native_delimiters():
    assert normalize_math_delimiters(r"a \(x^2\) b") == "a $x^2$ b"
    assert (
        normalize_math_delimiters("\\[\n\\frac{a}{b}\n\\]")
        == "$$\n\\frac{a}{b}\n$$"
    )


def test_normalize_skips_code_and_escaped_backslashes():
    fence = "```\n\\(x\\)\n```\n\\(y\\)"
    assert normalize_math_delimiters(fence) == "```\n\\(x\\)\n```\n$y$"

    inline = "`\\(x\\)` and \\(y\\)"
    assert normalize_math_delimiters(inline) == "`\\(x\\)` and $y$"

    escaped = r"\\(x\\)"
    assert normalize_math_delimiters(escaped) == escaped


def test_normalize_unclosed_delimiter_is_kept():
    source = r"a \(x + y"
    assert normalize_math_delimiters(source) == source


def test_inline_math_tokens_keep_currency_literal():
    token = SimpleNamespace(content="5 and ", markup="$")
    tokens = inline_math_tokens(token)
    assert [t.type for t in tokens] == ["text"]
    assert tokens[0].content == "$5 and $"


def test_inline_math_tokens_use_injected_renderer():
    token = SimpleNamespace(content="x^2", markup="$")
    tokens = inline_math_tokens(token, lambda _source: "x²")
    assert [t.type for t in tokens] == ["text"]
    assert tokens[0].content == "x²"


def test_inline_math_tokens_polish_termtex_braces():
    token = SimpleNamespace(content=r"\sigma_{\mathrm{floor}}", markup="$")
    tokens = inline_math_tokens(token, lambda _source: "σ_{floor} + x^{2}")
    assert tokens[0].content == "σ_floor + x^2"


def test_inline_math_tokens_keep_raw_latex_untouched():
    # 没有渲染器（回落 LaTeX 原文）时不做收尾：原文要原样可见
    token = SimpleNamespace(content=r"\sigma_{\mathrm{floor}}", markup="$")
    tokens = inline_math_tokens(token)
    assert tokens[0].content == r"\sigma_{\mathrm{floor}}"


def test_polish_inline_text_keeps_ambiguous_braces():
    # 组内有运算符时去掉括号会歧义（a_{i+j} ≠ a_i+j）
    assert polish_inline_text("a_{i+j}") == "a_{i+j}"
    assert polish_inline_text("σ_{a b}") == "σ_{a b}"
    assert polish_inline_text("E  =  mc^2") == "E = mc^2"


# ---------- 行内 / 块级公式的抽取（不再把独立行内升级为块级） ----------


def test_block_sources_keep_sole_inline_as_inline():
    text = "段落里的 $x^2$ 不升级\n\n$y^2+z^2$\n\n$$\nw^2\n$$\n"
    assert mathrender.block_sources(text) == ["\nw^2\n"]
    blocks, inlines = mathrender._math_sources(text)
    assert blocks == ["\nw^2\n"]
    assert inlines == ["x^2", "y^2+z^2"]


def test_sole_inline_math_requires_only_math():
    tokens = math_parser_factory().parse("$x^2$ 与文字")
    inline = [tok for tok in tokens if tok.type == "inline"][0]
    assert mathrender.sole_inline_math(inline.children) is None

    tokens = math_parser_factory().parse("$x^2$")
    inline = [tok for tok in tokens if tok.type == "inline"][0]
    assert mathrender.sole_inline_math(inline.children) == "x^2"


# ---------- mathrender ----------


class _FakeTermtex(mathrender.TermtexRenderer):
    name = "termtex"

    def __init__(self, *, block: str = "a\n-\nb", inline: str = "x²") -> None:
        self.block = block
        self.inline = inline
        self.block_calls: list[tuple[str, int]] = []
        self.inline_calls: list[str] = []

    def render_block(self, latex: str, *, width: int = 0) -> str:
        self.block_calls.append((latex, width))
        return self.block

    def render_inline(self, latex: str) -> str:
        self.inline_calls.append(latex)
        return self.inline


def test_clamp_width_bounds():
    assert mathrender.clamp_width(1000) == mathrender.MAX_WIDTH
    assert mathrender.clamp_width(20) == mathrender.MIN_WIDTH
    assert mathrender.clamp_width(object()) == mathrender.MIN_WIDTH


def test_split_tag():
    assert mathrender.split_tag(r"x=1 \tag{2}") == (r"x=1", "2")
    assert mathrender.split_tag(r"x=1") == (r"x=1", None)


def test_normalize_source_removes_unsupported_commands():
    assert "rm" not in mathrender.normalize_source(r"x^{\rm GCRF}")
    assert "red" not in mathrender.normalize_source(r"\textcolor{red}{x}")
    assert mathrender.normalize_source(r"\boxed{x}") == r"\left[ x \right]"


def test_prewarm_uses_termtex_and_caches(monkeypatch):
    fake = _FakeTermtex()
    monkeypatch.setattr(
        mathrender, "select_backend", lambda env=None: (fake, None)
    )
    text = "inline $x^2$\n\n$$\na+b\n$$\n"
    warning = mathrender.prewarm_document(text, 40)
    assert warning is None
    assert fake.inline_calls == ["x^2"]
    assert [call[0] for call in fake.block_calls] == ["\na+b\n"]

    rendered = mathrender.render_block_cached("\na+b\n", width=40)
    assert rendered.text == fake.block


def test_prewarm_isolates_failed_formula(monkeypatch):
    class _Boom(_FakeTermtex):
        def render_block(self, latex, *, width=0):
            self.block_calls.append((latex, width))
            if "bad" in latex:
                raise mathrender.MathRenderError("boom")
            return self.block

    fake = _Boom()
    monkeypatch.setattr(
        mathrender, "select_backend", lambda env=None: (fake, None)
    )
    text = "$$\nbad\n$$\n\n$$\ngood\n$$\n"
    mathrender.prewarm_document(text, 40)

    bad = mathrender.render_block_cached("\nbad\n", width=40)
    good = mathrender.render_block_cached("\ngood\n", width=40)
    assert bad.text == "\nbad\n"
    assert good.text == fake.block
    # 失败记 once：再次 prewarm 不重试 bad（good 已缓存，也不再 spawn）
    mathrender.prewarm_document(text, 40)
    assert [call[0] for call in fake.block_calls] == ["\nbad\n", "\ngood\n"]


def test_prewarm_without_dollar_does_not_probe(monkeypatch):
    calls = []
    monkeypatch.setattr(
        mathrender,
        "select_backend",
        lambda env=None: calls.append(env) or (mathrender.PlainRenderer(), None),
    )
    assert mathrender.prewarm_document("plain text", 40) is None
    assert calls == []


def test_select_backend_plain_does_not_spawn(monkeypatch):
    def _boom():
        raise AssertionError("plain 模式不得探测 termtex")

    monkeypatch.setattr(mathrender, "probe_termtex", _boom)
    renderer, warning = mathrender.select_backend("plain")
    assert isinstance(renderer, mathrender.PlainRenderer)
    assert warning is None


def test_select_backend_invalid_env_warns(monkeypatch):
    renderer, warning = mathrender.select_backend("bogus")
    assert isinstance(renderer, mathrender.PlainRenderer)
    assert warning is not None and "非法" in warning


# ---------- App / 预览 ----------


class _FakeBackend:
    def __init__(self, *, command: str = "zsh"):
        self.panes = [
            PaneInfo(
                pane_id="p0",
                dead=False,
                command=command,
                alternate_on=False,
                cursor_x=0,
                cursor_y=0,
                cursor_flag=False,
                history_size=100,
                height=24,
                width=80,
                window_id="w0",
                window_index=0,
                mouse_standard=False,
                mouse_button=False,
                mouse_any=False,
                mouse_sgr=True,
                mouse_all=False,
            )
        ]
        self.commands: list[tuple[str, str]] = []
        self.sent: list[tuple] = []

    def list_panes(self):
        return list(self.panes)

    def poll(self, offsets):
        return list(self.panes), {
            p.pane_id: CaptureResult(text="", offset=0) for p in self.panes
        }

    def resize_window(self, window_id, cols, rows):
        pass

    def run_command(self, pane_id, command):
        self.commands.append((pane_id, command))

    def send_text(self, pane_id, text):
        self.sent.append(("text", text))

    def send_keys(self, pane_id, *keys):
        self.sent.append(("keys", keys))

    def send_ctrl_a(self, pane_id):
        self.sent.append(("ctrl_a", pane_id))


def _project(tmp_path: Path) -> str:
    (tmp_path / "notes.md").write_text(
        "# 标题\n\n行内 $x^2$。\n\n$$\n\\frac{a}{b}\n$$\n",
        encoding="utf-8",
    )
    (tmp_path / "plain.txt").write_text("plain\n", encoding="utf-8")
    (tmp_path / "big.md").write_text(
        "# big\n" + "x" * (MARKDOWN_PREVIEW_MAX_BYTES + 10), encoding="utf-8"
    )
    (tmp_path / "bin.md").write_bytes(b"# x\0y")
    return str(tmp_path)


def _app(tmp_path: Path) -> TerminalApp:
    return TerminalApp(_FakeBackend(), _project(tmp_path))


def test_menu_preview_label(tmp_path, monkeypatch):
    app = _app(tmp_path)
    absolute = str((tmp_path / "notes.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app.on_file_tree_path_context_requested(
                SimpleNamespace(
                    path="notes.md",
                    absolute=absolute,
                    is_dir=False,
                    screen_x=10,
                    screen_y=10,
                )
            )
            await pilot.pause(0.1)
            menu = app.screen.query_one(OptionList)
            ids = [option.id for option in menu.options]
            labels = [str(option.prompt) for option in menu.options]
            assert ids == ["view", "preview", "copy_rel", "copy_abs"]
            assert labels[1] == "预览 Markdown"

    asyncio.run(scenario())


def test_preview_action_pushes_screen_without_pane(tmp_path):
    app = _app(tmp_path)
    absolute = str((tmp_path / "notes.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "notes.md", absolute, False)
            await pilot.pause(0.5)
            assert isinstance(app.screen, MarkdownPreviewScreen)
            assert app._log_target == "notes.md"
            assert app.backend.commands == []

    asyncio.run(scenario())


def test_preview_works_while_pane_is_busy(tmp_path):
    """预览不查 _pane_problem：pane 跑 opencode 也能打开。"""
    app = TerminalApp(_FakeBackend(command="opencode"), _project(tmp_path))
    absolute = str((tmp_path / "notes.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "notes.md", absolute, False)
            await pilot.pause(0.5)
            assert isinstance(app.screen, MarkdownPreviewScreen)

    asyncio.run(scenario())


def test_preview_renders_markdown_and_header_footer(tmp_path):
    app = _app(tmp_path)
    absolute = str((tmp_path / "notes.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "notes.md", absolute, False)
            await pilot.pause(0.6)
            screen = app.screen
            assert isinstance(screen, MarkdownPreviewScreen)
            assert "notes.md" in str(screen.query_one("#mdpreview-header", Static).content)
            assert "Esc" in str(screen.query_one("#mdpreview-footer", Static).content)
            markdown = screen.query_one("#mdpreview-md")
            assert markdown.display is True
            assert screen.query_one("#mdpreview-placeholder", Static).display is False

    asyncio.run(scenario())


def test_math_block_uses_cached_termtex_text(tmp_path, monkeypatch):
    app = _app(tmp_path)
    absolute = str((tmp_path / "notes.md").resolve())
    source = Path(absolute).read_text(encoding="utf-8")
    normalized = normalize_math_delimiters(source)
    block = next(
        token
        for token in math_parser_factory().parse(normalized)
        if token.type == "math_block"
    )

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            width = mathrender.clamp_width(app.size.width)
            mathrender._CACHE.put((block.content, width), "a\n─\nb")
            app._context_menu_closed("preview", "notes.md", absolute, False)
            await pilot.pause(0.6)
            blocks = app.screen.query(MathBlock)
            assert blocks
            assert "─" in str(blocks.first().query_one(Static).content)

    asyncio.run(scenario())


def test_math_block_falls_back_to_latex_without_cache(tmp_path):
    app = _app(tmp_path)
    absolute = str((tmp_path / "notes.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "notes.md", absolute, False)
            await pilot.pause(0.6)
            blocks = app.screen.query(MathBlock)
            assert blocks
            assert "\\frac{a}{b}" in str(blocks.first().query_one(Static).content)

    asyncio.run(scenario())


async def _wait_until(pilot, predicate, timeout: float = 3.0, step: float = 0.05) -> bool:
    """轮询直到 ``predicate()`` 为真。

    预览的渲染走 worker，布局完成时机在慢机器 / xdist 上差别很大；固定
    ``pause(0.6)`` 属于赌博（CI py3.12 实测偶发），改用**条件等待**。
    """
    waited = 0.0
    while waited < timeout:
        if predicate():
            return True
        await pilot.pause(step)
        waited += step
    return predicate()


async def _scroll_preview(pilot, scroll, target: int = 30, timeout: float = 6.0) -> int:
    """把预览滚到 ``target``，返回**实际**落点。

    ``scroll_to`` 在内容尚未布局完（``max_scroll_y == 0``）时会被 clamp 到 0，
    而且不会自己恢复——所以必须**每轮重新下发**，只观察是不行的（CI py3.10/
    3.11/3.12 实测：0.6s 时布局常常还没完成）。到不了 target（内容比预期短）
    就退到能滚的最远处，用实际值作为期望——用例关心的是「重载前后一致」。
    """
    waited = 0.0
    while waited < timeout:
        scroll.scroll_to(y=target, animate=False)
        await pilot.pause(0.05)
        if int(scroll.scroll_y) == target:
            return target
        waited += 0.05
    scroll.scroll_end(animate=False)
    await pilot.pause(0.05)
    return int(scroll.scroll_y)


def test_reload_keeps_scroll_position(tmp_path):
    app = _app(tmp_path)
    long = tmp_path / "long.md"
    long.write_text(
        "\n".join(f"## H{i}\n\npara {i}" for i in range(200)), encoding="utf-8"
    )
    absolute = str(long.resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "long.md", absolute, False)
            await pilot.pause(0.6)
            screen = app.screen
            scroll = screen.query_one("#mdpreview-scroll")
            expected = await _scroll_preview(pilot, scroll)
            assert expected > 0, "预览内容不可滚动——渲染或布局有问题"

            # 改盘上内容：重载后正文里必须出现这一行，用来区分「重载前」与「重载后」
            # （否则 scroll_y 一直是同一个值，断言可能直接通过而没真的验证恢复逻辑）。
            long.write_text(
                long.read_text(encoding="utf-8") + "\n## RELOAD-MARK\n",
                encoding="utf-8",
            )
            await pilot.press("r")

            assert await _wait_until(
                pilot, lambda: "RELOAD-MARK" in screen._text, timeout=5.0
            ), "重载没有读到新内容"
            assert await _wait_until(
                pilot, lambda: int(scroll.scroll_y) == expected, timeout=5.0
            ), f"重载后滚动位置丢失：期望 {expected}，实际 {scroll.scroll_y}"
            assert isinstance(app.screen, MarkdownPreviewScreen)

    asyncio.run(scenario())


def test_link_click_does_not_navigate(tmp_path):
    app = _app(tmp_path)
    absolute = str((tmp_path / "notes.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "notes.md", absolute, False)
            await pilot.pause(0.5)
            screen = app.screen
            calls = []

            event = SimpleNamespace(
                prevent_default=lambda: calls.append("prevent"),
                stop=lambda: calls.append("stop"),
            )
            screen.on_markdown_link_clicked(event)
            assert calls == ["prevent", "stop"]
            assert "不打开链接" in str(
                screen.query_one("#mdpreview-footer", Static).content
            )

    asyncio.run(scenario())


def test_escape_closes_and_returns_focus(tmp_path):
    app = _app(tmp_path)
    absolute = str((tmp_path / "notes.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "notes.md", absolute, False)
            await pilot.pause(0.5)
            await pilot.press("escape")
            await pilot.pause(0.2)
            assert not isinstance(app.screen, MarkdownPreviewScreen)
            assert app.focused is app.query_one(FileTree)

    asyncio.run(scenario())


def test_large_markdown_is_blocked_before_push(tmp_path):
    app = _app(tmp_path)
    absolute = str((tmp_path / "big.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "big.md", absolute, False)
            await pilot.pause(0.2)
            assert not isinstance(app.screen, MarkdownPreviewScreen)
            assert app.backend.commands == []

    asyncio.run(scenario())


def test_screen_guard_blocks_oversize_file(tmp_path):
    app = _app(tmp_path)
    absolute = str((tmp_path / "big.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            screen = MarkdownPreviewScreen(absolute, relative="big.md")
            app.push_screen(screen)
            await pilot.pause(0.4)
            placeholder = screen.query_one("#mdpreview-placeholder", Static)
            assert placeholder.display is True
            assert "过大" in str(placeholder.content)
            assert screen.query_one("#mdpreview-md").display is False
            await pilot.press("escape")
            await pilot.pause(0.1)

    asyncio.run(scenario())


def test_binary_and_missing_show_message(tmp_path):
    app = _app(tmp_path)
    binary = str((tmp_path / "bin.md").resolve())
    missing = str((tmp_path / "gone.md").resolve())

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app._context_menu_closed("preview", "bin.md", binary, False)
            await pilot.pause(0.4)
            text = str(
                app.screen.query_one("#mdpreview-placeholder", Static).content
            )
            assert "二进制" in text
            await pilot.press("escape")
            await pilot.pause(0.1)

            app._context_menu_closed("preview", "gone.md", missing, False)
            await pilot.pause(0.4)
            text = str(
                app.screen.query_one("#mdpreview-placeholder", Static).content
            )
            assert "不存在" in text

    asyncio.run(scenario())


def test_sidebar_shortcut_text_is_synced():
    sidebar = next(section for section in SECTIONS if section.title == "侧栏")
    row = next(row for row in sidebar.rows if row.keys == "右键")
    assert row.desc == "打开 / 预览 / 复制路径"
