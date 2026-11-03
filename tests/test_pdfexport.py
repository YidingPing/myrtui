"""A4 PDF 导出（``mdpreview`` 的 ``p`` / ``client/pdfexport.py``）。

覆盖四块：

- 打印模板：A4 纸、页边距、白底、分页规则（``p`` 不许 ``break-inside: avoid``）、
  长代码换行而不是被裁、背景色真的会打印；
- 正文同源：公式 / 表格与 ``v`` 屏走同一个 ``docpage.render_body``；
- 导出链路（fake chrome runner，**不 spawn 真 Chrome**）：落盘、覆盖、失败保护、
  无 Chrome、超时、产物不是 PDF、目录不可写；
- 预览层 ``p``：调用参数、成功/失败提示、空文档不导出。

真机验收（``pdfinfo`` 是 595.28×841.89 pts、公式与图片都进 PDF）不在这里，
见 ``docs-internal`` 的验收段。
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest
from textual.widgets import Static

from myrtui.client import katex, pdfexport
from myrtui.client.app import TerminalApp
from myrtui.client.mdpreview import MarkdownPreviewScreen
from myrtui.i18n import t
from myrtui.tmux_backend import CaptureResult, PaneInfo

FAKE_CHROME = katex.Chrome("/nonexistent/chrome")


# ---------------------------------------------------------------------------
# 打印模板
# ---------------------------------------------------------------------------


def test_print_html_is_a4_with_margins():
    html = pdfexport.build_print_html("# title\n", title="doc")
    assert "@page { size: A4; margin: 16mm 14mm; }" in html
    assert "<title>doc</title>" in html


def test_print_html_keeps_paragraphs_breakable_but_protects_blocks():
    """``p`` 上不许出现 ``break-inside: avoid``（A4 下会把长段整段推走）。"""
    html = pdfexport.build_print_html("text\n")
    paragraph_rule = next(
        line for line in html.splitlines() if line.startswith("p, ul, ol, dl")
    )
    assert "break-inside" not in paragraph_rule
    assert "orphans: 2" in html and "widows: 2" in html
    pre_rule = " ".join(html.splitlines()[html.splitlines().index("pre {"):])
    assert "break-inside: avoid" in pre_rule
    assert "break-inside: avoid" in "\n".join(
        line for line in html.splitlines() if line.startswith("table ")
    )


def test_print_html_wraps_long_code_lines_instead_of_clipping():
    html = pdfexport.build_print_html("```\nx\n```\n")
    assert "white-space: pre-wrap" in html
    assert "overflow: auto" not in html and "overflow-x" not in html
    assert "print-color-adjust: exact" in html


def test_print_html_escapes_title():
    html = pdfexport.build_print_html("x\n", title='a & <b> "c"')
    assert "<title>a &amp; &lt;b&gt; &quot;c&quot;</title>" in html


def test_print_html_uses_the_preview_body_pipeline(tmp_path):
    """公式 delimiter 与表格结构必须与 ``v`` 屏一致（同一份 token 流）。"""
    from myrtui.client import docpage

    text = "行内 $x^2$\n\n$$\ny=1\n$$\n\n|a|b|\n|-|-|\n|1|2|\n"
    html = pdfexport.build_print_html(text, base_dir=tmp_path, title="t")
    expected_body = docpage.render_body(text, base_dir=tmp_path)
    assert expected_body in html
    assert "\\(x^2\\)" in html and "\\[y=1\\]" in html
    assert "<table>" in html


def test_print_html_inlines_local_images_and_drops_remote(tmp_path):
    (tmp_path / "pic.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    html = pdfexport.build_print_html(
        "![a](pic.png)\n\n![b](https://example.com/x.png)\n",
        base_dir=tmp_path,
        title="t",
    )
    assert "data:image/png;base64," in html
    assert "example.com" not in html


def test_target_for_is_next_to_the_source(tmp_path):
    assert pdfexport.target_for(tmp_path / "notes.md") == tmp_path / "notes.pdf"
    assert pdfexport.target_for("README") == Path("README.pdf")


# ---------------------------------------------------------------------------
# 导出链路（fake runner）
# ---------------------------------------------------------------------------


def _runner(payload: bytes = b"%PDF-1.4\n1 0 obj\n", *, returncode: int = 0,
            stderr: bytes = b"", raises: Exception | None = None):
    """假 ``subprocess.run``：把 payload 写到 ``--print-to-pdf=`` 指向的位置。"""
    calls: list[list[str]] = []

    def run(command, **kwargs):
        calls.append(list(command))
        if raises is not None:
            raise raises
        target = next(arg for arg in command if arg.startswith("--print-to-pdf="))
        Path(target.split("=", 1)[1]).write_bytes(payload)
        return subprocess.CompletedProcess(command, returncode, b"", stderr)

    run.calls = calls  # type: ignore[attr-defined]
    return run


def _export(tmp_path: Path, runner, **kwargs):
    source = tmp_path / "notes.md"
    source.write_text("# notes\n", encoding="utf-8")
    return pdfexport.export_pdf(
        "# notes\n", source, chrome=FAKE_CHROME, runner=runner, **kwargs
    )


def test_export_writes_target_and_cleans_temp(tmp_path):
    runner = _runner()
    result = _export(tmp_path, runner, timeout=5.0)
    assert result.ok, result.error
    assert result.path == tmp_path / "notes.pdf"
    assert result.path.read_bytes().startswith(b"%PDF-")
    assert list(tmp_path.glob(".myrtui-pdf-*")) == []
    command = runner.calls[0]
    assert any(arg.startswith("--print-to-pdf=") for arg in command)
    assert "--no-pdf-header-footer" in command
    assert "--virtual-time-budget=3000" in command


def test_export_keeps_previous_file_when_chrome_fails(tmp_path):
    target = tmp_path / "notes.pdf"
    target.write_bytes(b"%PDF-old")
    runner = _runner(returncode=1, stderr=b"boom")
    result = _export(tmp_path, runner)
    assert not result.ok
    assert "boom" in (result.error or "")
    assert target.read_bytes() == b"%PDF-old"
    assert list(tmp_path.glob(".myrtui-pdf-*")) == []


def test_export_reports_missing_chrome(tmp_path, monkeypatch):
    monkeypatch.setattr(katex, "find_chrome", lambda *a, **k: None)
    source = tmp_path / "notes.md"
    source.write_text("# notes\n", encoding="utf-8")
    result = pdfexport.export_pdf("# notes\n", source)
    assert result.error == t("md.export_no_chrome")
    assert not (tmp_path / "notes.pdf").exists()


def test_export_rejects_non_pdf_output(tmp_path):
    result = _export(tmp_path, _runner(payload=b"not a pdf"))
    assert not result.ok
    assert t("math.chrome_not_pdf") in (result.error or "")
    assert not (tmp_path / "notes.pdf").exists()


def test_export_reports_timeout(tmp_path):
    runner = _runner(raises=subprocess.TimeoutExpired(["chrome"], 1.0))
    result = _export(tmp_path, runner, timeout=1.0)
    assert result.error == t("md.export_timeout", seconds=1)
    assert not (tmp_path / "notes.pdf").exists()


def test_export_reports_unwritable_directory(tmp_path):
    result = _export(tmp_path, _runner(), target=tmp_path / "nope" / "notes.pdf")
    assert not result.ok
    assert result.error  # 文案来自 OSError，不抛异常


def test_export_honours_custom_target(tmp_path):
    result = _export(tmp_path, _runner(), target=tmp_path / "out" / "doc.pdf")
    assert not result.ok  # 目录不存在：宁可报错，也不偷偷建目录
    (tmp_path / "out").mkdir()
    result = _export(tmp_path, _runner(), target=tmp_path / "out" / "doc.pdf")
    assert result.ok, result.error
    assert (tmp_path / "out" / "doc.pdf").exists()


# ---------------------------------------------------------------------------
# 预览层的 p
# ---------------------------------------------------------------------------


class _FakeBackend:
    def __init__(self) -> None:
        self.panes = [
            PaneInfo(
                pane_id="p0",
                dead=False,
                command="zsh",
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

    def list_panes(self):
        return list(self.panes)

    def poll(self, offsets):
        return list(self.panes), {
            pane.pane_id: CaptureResult(text="", offset=0) for pane in self.panes
        }

    def resize_window(self, window_id, cols, rows):
        pass

    def run_command(self, pane_id, command):
        pass

    def send_text(self, pane_id, text):
        pass

    def send_keys(self, pane_id, *keys):
        pass

    def send_ctrl_a(self, pane_id):
        pass


def _app(tmp_path: Path) -> TerminalApp:
    (tmp_path / "notes.md").write_text("# 标题\n\n正文\n", encoding="utf-8")
    return TerminalApp(_FakeBackend(), str(tmp_path))


async def _wait_until(pilot, predicate, timeout: float = 3.0, step: float = 0.05):
    waited = 0.0
    while waited < timeout:
        if predicate():
            return True
        await pilot.pause(step)
        waited += step
    return predicate()


def _footer(screen) -> str:
    return str(screen.query_one("#mdpreview-footer", Static).content)


def test_preview_p_exports_next_to_the_source(tmp_path, monkeypatch):
    """``p`` 用屏内正文导出，落点是源文件同目录的 ``.pdf``。"""
    app = _app(tmp_path)
    absolute = str((tmp_path / "notes.md").resolve())
    calls: list[tuple[str, str]] = []

    def fake_export(text, source, **kwargs):
        calls.append((text, str(source)))
        return pdfexport.ExportResult(tmp_path / "notes.pdf")

    monkeypatch.setattr(pdfexport, "export_pdf", fake_export)

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app.push_screen(MarkdownPreviewScreen(absolute, relative="notes.md"))
            await pilot.pause(0.3)
            screen = app.screen
            assert isinstance(screen, MarkdownPreviewScreen)
            await pilot.press("p")
            ok = await _wait_until(pilot, lambda: str(tmp_path / "notes.pdf") in _footer(screen))
            assert ok, _footer(screen)

    asyncio.run(scenario())
    assert calls and calls[0][1] == absolute
    assert "标题" in calls[0][0]


def test_preview_p_reports_failure(tmp_path, monkeypatch):
    app = _app(tmp_path)
    absolute = str((tmp_path / "notes.md").resolve())
    monkeypatch.setattr(
        pdfexport,
        "export_pdf",
        lambda *a, **k: pdfexport.ExportResult(
            tmp_path / "notes.pdf", error="no chrome here"
        ),
    )

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app.push_screen(MarkdownPreviewScreen(absolute, relative="notes.md"))
            await pilot.pause(0.3)
            screen = app.screen
            await pilot.press("p")
            ok = await _wait_until(
                pilot, lambda: "no chrome here" in _footer(screen)
            )
            assert ok, _footer(screen)

    asyncio.run(scenario())


def test_preview_p_refuses_empty_document(tmp_path, monkeypatch):
    """还没读到内容时 ``p`` 只提示，不 fork Chrome。"""
    app = _app(tmp_path)
    absolute = str((tmp_path / "notes.md").resolve())
    called: list[int] = []
    monkeypatch.setattr(
        pdfexport,
        "export_pdf",
        lambda *a, **k: called.append(1) or pdfexport.ExportResult(tmp_path / "x.pdf"),
    )

    async def scenario():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.3)
            app.push_screen(MarkdownPreviewScreen(absolute, relative="notes.md"))
            await pilot.pause(0.3)
            screen = app.screen
            screen._text = ""
            await pilot.press("p")
            ok = await _wait_until(pilot, lambda: t("md.not_ready") in _footer(screen))
            assert ok, _footer(screen)

    asyncio.run(scenario())
    assert called == []


def test_footer_mentions_the_export_key():
    """``p`` 必须出现在 footer 里：不加提示的键等于没有。"""
    assert "p " in t("md.footer")
