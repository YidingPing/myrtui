"""Phase 2（UI 国际化）的守卫（GITHUB-PUBLISH-PLAN §4.3）。

九条用例各自守一件事：

- 两套词表的 key / 占位符 / 非空值一致（改文案最容易漏其中一条）；
- 语言解析：默认 ``en``、``zh*`` 识别、缺 key 原样返回；
- 源码里**只剩**该留的汉字（docstring / 日志实参 / 资源块 / 正则数据），
  以及**没有模块级 t()**（模块级求值会固化语言，测试钉不住）；
- 80 列下顶栏与快捷键浮层两套语言都不溢出。

语言探测类用例打 ``no_lang_pin`` 豁免——conftest 的 autouse 夹具会把
``MYRTUI_LANG`` 钉成 ``zh_CN``，不豁免则 ``test_default_is_en`` 必红。
"""
from __future__ import annotations

import ast
import asyncio
import re
import tempfile
from pathlib import Path

import pytest
from rich.cells import cell_len

from myrtui import i18n
from myrtui.client import app as app_module
from myrtui.client.app import TerminalApp
from myrtui.client.shortcuts import ShortcutHelp, SECTIONS, render_shortcut_help
from myrtui.client.topbar import TopBar, TopBarButton
from myrtui.locales import en, zh_CN
from myrtui.tmux_backend import CaptureResult, PaneInfo

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "myrtui"

HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
FORMAT_FIELD = re.compile(r"\{([^{}]*)\}")

# 资源块（CSS / JS / HTML 模板）的赋值名：这些不是 UI 文案，是渲染资产
ASSET_NAME = re.compile(r"^(?:CSS|STYLES?)$|(?:_|^)(?:CSS|JS|HTML|TEMPLATE)$")
MARKUP_MARKERS = ("<style", "<script", "<!doctype")


# ---------------------------------------------------------------- 词表


def test_keys_match() -> None:
    assert set(en.MESSAGES) == set(zh_CN.MESSAGES)


def test_no_empty_values() -> None:
    for lang, table in (("en", en.MESSAGES), ("zh_CN", zh_CN.MESSAGES)):
        empty = [key for key, value in table.items() if not value.strip()]
        assert not empty, f"{lang} 有空文案：{empty}"
    # en 是「给外部看的默认语言」：残留汉字说明漏翻
    leftover = [key for key, value in en.MESSAGES.items() if HAN.search(value)]
    assert not leftover, f"en 词表里残留中文：{leftover}"


def test_placeholders_match() -> None:
    for key, text in en.MESSAGES.items():
        want = {m.group(1) for m in FORMAT_FIELD.finditer(text)}
        got = {m.group(1) for m in FORMAT_FIELD.finditer(zh_CN.MESSAGES[key])}
        assert want == got, f"{key}: en {sorted(want)} != zh_CN {sorted(got)}"


# ---------------------------------------------------------------- 语言解析


@pytest.mark.no_lang_pin
def test_default_is_en(monkeypatch) -> None:
    for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(var, raising=False)
    i18n.reset_lang()
    assert i18n.resolve_lang() == "en"


@pytest.mark.no_lang_pin
def test_zh_prefix_detection(monkeypatch) -> None:
    monkeypatch.setenv("LANG", "zh_CN.UTF-8")
    i18n.reset_lang()
    assert i18n.resolve_lang() == "zh_CN"

    monkeypatch.setenv("LANG", "C")
    i18n.reset_lang()
    assert i18n.resolve_lang() == "en"

    monkeypatch.setenv(i18n.LANG_ENV, "zh_CN")
    monkeypatch.setenv("LANG", "C")
    i18n.reset_lang()
    assert i18n.resolve_lang() == "zh_CN", "MYRTUI_LANG 的优先级要高于 LANG"


def test_missing_key_falls_back() -> None:
    assert i18n.t("nope.not.a.key") == "nope.not.a.key"
    assert i18n.t("nope.not.a.key", count=2) == "nope.not.a.key"


# ---------------------------------------------------------------- 源码约束


def _han_offenders() -> list[str]:
    """按位置豁免后，源码里剩下的汉字字面量（应为空）。"""
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "locales" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed: set[int] = set()

        def allow_subtree(node: ast.AST) -> None:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Constant):
                    allowed.add(id(sub))

        for node in ast.walk(tree):
            # ① bare-string Expr：docstring 与 dataclass 字段属性文档
            if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                allowed.add(id(node.value))
            # ② 日志 / print 的实参
            if isinstance(node, ast.Call):
                name = ast.unparse(node.func)
                if name.startswith(("log.", "logger.")) or name == "print" or name == "re.compile":
                    for arg in node.args:
                        allow_subtree(arg)
        # ③ 资源块：CSS / JS / HTML 模板（赋值名或含标记的 f-string）
        for node in ast.walk(tree):
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                if any(
                    isinstance(target, ast.Name) and ASSET_NAME.search(target.id)
                    for target in targets
                ):
                    allow_subtree(node.value)
                elif isinstance(node.value, ast.JoinedStr) and any(
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and any(marker in part.value.lower() for marker in MARKUP_MARKERS)
                    for part in node.value.values
                ):
                    allow_subtree(node.value)
            if isinstance(node, ast.Return) and isinstance(node.value, ast.JoinedStr):
                if any(
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and any(marker in part.value.lower() for marker in MARKUP_MARKERS)
                    for part in node.value.values
                ):
                    allow_subtree(node.value)

        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in allowed or not HAN.search(node.value):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value[:60]!r}")
    return offenders


def test_ast_han_only_in_allowed_positions() -> None:
    """汉字只允许出现在 docstring / 日志实参 / 资源块 / locales 里。

    新写的界面文案必须走 ``t()``；这条不让「顺手写死一句中文」溜过去。
    """
    offenders = _han_offenders()
    assert not offenders, "以下汉字字面量应当迁到 locales：\n  " + "\n  ".join(offenders)


def _module_level_t(path: Path) -> list[str]:
    """模块级 / 类体里的赋值右值出现 ``t(...)`` → 语言在 import 时被固化。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []

    def check(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
                for sub in ast.walk(node.value):
                    if isinstance(sub, ast.Call) and getattr(sub.func, "id", None) == "t":
                        found.append(f"{path.relative_to(ROOT)}:{sub.lineno}")
        for node in body:
            if isinstance(node, ast.ClassDef):
                check(node.body)

    check(tree.body)
    return found


def test_no_module_level_t_call() -> None:
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "locales" in path.parts:
            continue
        offenders.extend(_module_level_t(path))
    assert not offenders, (
        "模块级/类体赋值里不能调 t()（import 时求值，语言会被固化）：\n  "
        + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------- 布局（两套语言）


class _FakeBackend:
    def __init__(self, count: int = 4):
        self.count = count

    def list_panes(self):
        return [
            PaneInfo(
                pane_id=f"p{i}",
                dead=False,
                command="sh",
                alternate_on=False,
                cursor_x=0,
                cursor_y=0,
                cursor_flag=False,
                history_size=100,
                height=24,
                width=80,
                window_id=f"w{i}",
                window_index=i,
                mouse_standard=False,
                mouse_button=False,
                mouse_any=False,
                mouse_sgr=True,
                mouse_all=False,
            )
            for i in range(self.count)
        ]

    def poll(self, offsets):
        panes = self.list_panes()
        return panes, {p.pane_id: CaptureResult(text="ok\n", offset=0) for p in panes}

    def resize_window(self, window_id, cols, rows):
        pass

    def send_keys(self, *args, **kwargs):
        pass

    def send_text(self, *args, **kwargs):
        pass

    def send_mouse(self, *args, **kwargs):
        pass

    def send_ctrl_a(self, *args, **kwargs):
        pass

    def paste(self, *args, **kwargs):
        pass


def _render_at_80(monkeypatch, lang: str) -> dict:
    """80×24 下跑一遍，采集顶栏各控件文本/宽度与浮层正文。"""
    monkeypatch.setenv(i18n.LANG_ENV, lang)
    i18n.reset_lang()
    out: dict[str, object] = {}

    async def scenario() -> None:
        app = TerminalApp(_FakeBackend(4), tempfile.mkdtemp(prefix="myrtui-i18n-"))
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause(0.4)
            app._show_shortcut_help()
            await pilot.pause(0.2)
            panel = app.query_one("#shortcut-help", ShortcutHelp)
            body = panel.query_one("#shortcut-help-body").render().plain
            brand = app.query_one("#topbar-brand")
            out.update(
                body=body,
                buttons={
                    f"#{widget.id}": (str(widget.content), widget.size.width)
                    for widget in app.query(TopBarButton)
                    if widget.id
                },
                brand=(str(brand.content), brand.size.width, brand.region.right),
                over=[
                    line
                    for line in body.splitlines()
                    if line and cell_len(line) > panel.content_size.width
                ],
                panel_right=panel.region.right,
                visible=panel.display,
            )

    asyncio.run(scenario())
    i18n.reset_lang()
    return out


def test_topbar_fits_80_cols(monkeypatch) -> None:
    """英文比中文宽：80 列下顶栏与浮层都不能被截断（§4.4）。"""
    for lang, needles in (
        ("en", ("Ctrl-a prefix", "every other key goes straight to the shell")),
        ("zh_CN", ("Ctrl-a 前缀", "其余按键直接透传 shell")),
    ):
        got = _render_at_80(monkeypatch, lang)
        body = str(got["body"])
        for needle in needles:
            assert needle in body, f"{lang}: 浮层里缺少 {needle!r}"

        buttons = got["buttons"]
        assert isinstance(buttons, dict)
        # Detach / Kill 刻意保持英文短词：顶栏只有一行，中文更长
        for button_id, needle in (("#btn-detach", "Detach"), ("#btn-kill", "Kill")):
            text, width = buttons[button_id]
            assert needle in text, f"{lang}: {button_id} 文案丢了"
            assert width >= cell_len(text), f"{lang}: {button_id} 被裁切"

        brand, brand_width, brand_right = got["brand"]  # type: ignore[misc]
        assert brand_width >= cell_len(brand), f"{lang}: 品牌被裁切"
        assert brand_right <= 80, f"{lang}: 品牌越界"
        assert not got["over"], f"{lang}: 浮层超宽 {got['over']}"
        assert got["visible"], f"{lang}: 浮层没打开"
        assert got["panel_right"] <= 80, f"{lang}: 浮层越界"
