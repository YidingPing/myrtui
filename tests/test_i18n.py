"""Phase 2（UI 国际化）的守卫（GITHUB-PUBLISH-PLAN §4.3）。

十条用例各自守一件事：

- 两套词表的 key / 占位符 / 非空值一致（改文案最容易漏其中一条）；
- 语言解析：默认 ``en``、``zh*`` 识别、缺 key 原样返回；
- 源码里**只剩**该留的汉字（docstring / 日志实参 / 资源块 / 正则数据），
  以及**没有模块级 t()**（模块级求值会固化语言，测试钉不住）；
- 控件的文案字面量**要么是 t()，要么是纯符号**（0.5.1 补：汉字守卫只看一个方向，
  `Button("Kill")` 里一个汉字都没有，于是漏到 0.5.0 的 PyPI 上）；
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
from textual.widgets import Button, Label

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


# ------------------------------------------------- 英文裸串（0.5.1）

#: 首个位置参数是「用户可见文案」的控件构造函数。
TEXT_WIDGETS = {"Button", "Checkbox", "Label", "Static", "TopBarButton"}

#: 允许的非常量文案：纯符号（` + `、` ? `）、空串。汉字不在这个豁免里——
#: 它们归上一条守卫管，两条一起才能盖住「中文没走词表」和「英文没走词表」。
SYMBOL_ONLY = re.compile(r"^[\s\W_]*$")


def _bare_english_widget_text() -> list[str]:
    """控件构造的第一个位置参数若是字面量，必须是 t() 或纯符号。

    0.5.1 之前只有「源码里不许有游离汉字」那条守卫，它只看一个方向：
    ``Button("Kill")`` 是英文，AST 里一个汉字都没有，于是一路漏到 PyPI 上的
    0.5.0（zh_CN 用户看得见）。反向的漏必须靠这条补。
    """
    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if "locales" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            if ast.unparse(node.func).rsplit(".", 1)[-1] not in TEXT_WIDGETS:
                continue
            if not node.args:
                continue
            first = node.args[0]
            if not (isinstance(first, ast.Constant) and isinstance(first.value, str)):
                continue  # t() / f-string / 变量：动态拼的不在扫描范围
            if SYMBOL_ONLY.match(first.value):
                continue
            offenders.append(f"{path.relative_to(ROOT)}:{first.lineno}: {first.value[:40]!r}")
    return offenders


def test_no_bare_english_widget_text() -> None:
    """英文 UI 文案同样必须走 t()——汉字守卫只看一个方向。"""
    offenders = _bare_english_widget_text()
    assert not offenders, (
        "以下控件文案是字面量，应当迁到 locales（纯符号有白名单）：\n  "
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
    """英文比中文宽：80 列下顶栏与浮层都不能被截断（§4.4）。

    0.5.1 起顶栏按钮进词表。旧注释写的是「Detach / Kill 刻意保持英文短词：
    顶栏只有一行，中文更长」——**实测相反**：`cell_len(" Detach ")` 是 8，
    `cell_len(" 分离 ")` 是 6；`" Kill "` 与 `" 结束 "` 同为 6。中文只会更窄，
    所以那条「宽度不允许中文」的理由不成立（BACKLOG: i18n-hardcoded-topbar）。
    """
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
        wanted = {"en": ("Detach", "Kill"), "zh_CN": ("分离", "结束")}[lang]
        for button_id, needle in zip(("#btn-detach", "#btn-kill"), wanted):
            text, width = buttons[button_id]
            assert needle in text, f"{lang}: {button_id} 文案不对：{text!r}"
            assert width >= cell_len(text), f"{lang}: {button_id} 被裁切"

        brand, brand_width, brand_right = got["brand"]  # type: ignore[misc]
        assert brand_width >= cell_len(brand), f"{lang}: 品牌被裁切"
        assert brand_right <= 80, f"{lang}: 品牌越界"
        assert not got["over"], f"{lang}: 浮层超宽 {got['over']}"
        assert got["visible"], f"{lang}: 浮层没打开"
        assert got["panel_right"] <= 80, f"{lang}: 浮层越界"


def test_kill_confirm_buttons_follow_language(monkeypatch) -> None:
    """关会话的确认框（正文 + 两个按钮）整体跟语言走。

    0.5.1：按钮曾经是字面量 Kill / Cancel，正文里还夹着英文 `Kill tmux server`。
    正文也一起断言，否则「按钮翻译了、正文没翻」这种半截改动照样能绿。
    """
    cases = (
        ("en", "Kill the tmux server? Every shell will end.", ("Kill", "Cancel")),
        ("zh_CN", "结束 tmux 服务？全部 shell 将结束。", ("结束", "取消")),
    )
    for lang, message, wanted in cases:
        monkeypatch.setenv(i18n.LANG_ENV, lang)
        i18n.reset_lang()

        async def scenario() -> tuple[str, str, str]:
            app = TerminalApp(_FakeBackend(1), tempfile.mkdtemp(prefix="myrtui-i18n-"))
            async with app.run_test(size=(80, 24)) as pilot:
                app.push_screen(app_module.KillConfirm(i18n.t("app.kill_confirm")))
                await pilot.pause(0.2)
                return (
                    str(app.screen.query_one(Label).render()),
                    str(app.screen.query_one("#yes", Button).label),
                    str(app.screen.query_one("#no", Button).label),
                )

        got = asyncio.run(scenario())
        assert got == (message, *wanted), f"{lang}: 确认框没跟语言走：{got}"

    i18n.reset_lang()
