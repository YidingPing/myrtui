"""共享测试夹具。

``app_root``：App 级用例的「项目根」。

**不要再用 ``tempfile.gettempdir()`` 当 App 根**——本机系统临时目录会不断积累
条目（实测 28,143 个），而 ``FileTree`` 挂载时要把根目录的每一项都渲染一遍
（``render_label`` → ``_node_relative`` → ``Path.resolve()``，28k 条目 ≈ 1.9s，
空目录只需 0.06s）。169 次 ``App.run_test`` 叠加后就是数分钟的纯浪费，而且
耗时随机器使用线性恶化、本地与 CI 不一致。

``tmp_path`` 里的几个文件不是为了好看：文件树至少要几行，``test_mouse`` 的
``offset=(1, 2)`` 点击、``test_v11`` 的树区域几何断言才成立。根目录刻意**不是
git 仓库**，与原 ``$TMPDIR`` 语义一致（侧栏显示 ``GIT -``）。
"""
from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_ssh_env(monkeypatch):
    """每个用例默认**不是** SSH 会话、且用 v0.35 的默认鼠标行为。

    ``myrtui.client.clipboard.is_remote_session`` 会读 ``SSH_*`` 环境变量；
    开发机 / CI 可能本身就跑在 SSH 里，会把「本地双通道复制 + 系统剪贴板优先」
    的用例全部变成远端语义。``MYRTUI_COPY_ON_SELECT`` / ``MYRTUI_DRAG_SELECT``
    同理：用户 shell 里的全局设置不能让测试语义漂移。需要测 SSH / 逃生舱语义的
    用例在用例体内 ``monkeypatch.setenv(...)`` 即可（autouse 先执行，体内覆盖生效）。
    """
    for key in (
        "SSH_CONNECTION",
        "SSH_CLIENT",
        "SSH_TTY",
        "MYRTUI_COPY_ON_SELECT",
        "MYRTUI_DRAG_SELECT",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture(autouse=True)
def _pin_ui_language(request, monkeypatch, tmp_path):
    """把 UI 语言钉成 ``zh_CN``，并把用户配置隔离掉。

    两个理由：

    1. 存量测试里有 295 条含汉字的断言、其中 33 条逐字命中 UI 文案；把语言
       钉成 ``zh_CN`` 就能让它们零改动继续通过（新用例才要求走 ``t(key)``）。
    2. 开发机的 ``LANG`` / ``~/.local/share/myrtui/config.json`` 不能让测试语义
       漂移——``MYRTUI_CONFIG`` 指到临时路径，配置文件不存在即「没配」。

    语言探测类用例打 ``@pytest.mark.no_lang_pin`` 豁免（否则 ``MYRTUI_LANG``
    会被先设成 ``zh_CN``，``test_default_is_en`` 必然红）。
    """
    from myrtui import i18n

    monkeypatch.setenv(i18n.CONFIG_ENV, str(tmp_path / "config.json"))
    if request.node.get_closest_marker("no_lang_pin"):
        monkeypatch.delenv(i18n.LANG_ENV, raising=False)
    else:
        monkeypatch.setenv(i18n.LANG_ENV, "zh_CN")
    i18n.reset_lang()
    yield
    i18n.reset_lang()


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "no_lang_pin: 不让 autouse 夹具钉 MYRTUI_LANG（语言探测类用例）"
    )
    config.addinivalue_line(
        "markers",
        "real_tools: 需要真实外部工具链（Chrome / pdflatex / pdftocairo）；"
        "默认 skip，MYRTUI_REAL_TOOLS=1 才跑",
    )


def pytest_collection_modifyitems(config, items):
    """真机冒烟默认跳过。

    CI 的 macOS runner 与开发机同机，Chrome 在并行负载下会偶发超时 / 空页
    （实测两次：test_katex_real_pipeline 空结果、test_real_pages 90s 超时），
    那是工具链抖动。`AGENTS.md` 的口径是这类用例保持手工。
    """
    if os.environ.get("MYRTUI_REAL_TOOLS") == "1":
        return
    skip = pytest.mark.skip(
        reason="真机工具链冒烟（Chrome / pdflatex）：设 MYRTUI_REAL_TOOLS=1 才跑"
    )
    for item in items:
        if "real_tools" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def app_root(tmp_path) -> str:
    """小而确定的项目根（每个用例独立，避免跨用例共享状态）。"""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("x\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    return str(tmp_path)
