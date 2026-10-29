"""install.sh 的静态回归：语法、交互确认、依赖代装入口与「安装源已换 PyPI」。

真正安装依赖需要网络 / 包管理器，不适合放进自动化；这里只锁定：
- POSIX sh 语法；
- 从 /dev/tty 读确认（`curl | sh` 的 stdin 不能用于交互）；
- MYRTUI_DEPS 三档开关；
- tmux / micro / ov / termtex 都走统一的询问路径；
- 安装源是 PyPI：不得再出现 Gitea / 内网地址 / token 逻辑（v0.4.4 迁移，
  见 myrtui-internal/docs/GITHUB-PUBLISH-PLAN.md §3.1）。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "install.sh"
TEXT = SCRIPT.read_text(encoding="utf-8")


def test_install_script_is_posix_shell_parsable() -> None:
    subprocess.run(["sh", "-n", str(SCRIPT)], check=True)


def test_no_bare_variable_before_multibyte_characters() -> None:
    """``$VAR（`` 会被 sh 解析成变量名 ``VAR（``（bash 允许 ≥0x80 的字节做名字，
    ``set -u`` 下直接 "unbound variable" 崩掉）。中文注释里一律用 ``${VAR}``。

    真实踩到：install.sh 打印「注意：PATH 上的 myrtui（$_path_bin）…」时整脚本退出。
    """
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(TEXT.splitlines(), 1)
        if re.search(r"\$[A-Za-z_][A-Za-z0-9_]*[^\x00-\x7f]", line)
    ]
    assert not offenders, "变量后面紧跟非 ASCII 字符时必须写成 ${VAR}：\n" + "\n".join(offenders)


def test_dependency_consent_uses_dev_tty() -> None:
    assert "/dev/tty" in TEXT
    assert "ask_yes()" in TEXT
    assert 'MYRTUI_DEPS="${MYRTUI_DEPS:-ask}"' in TEXT
    assert "non-interactive, skipping" in TEXT


def test_all_recommended_tools_have_install_paths() -> None:
    assert "ensure_tool micro" in TEXT
    assert "ensure_tool ov" in TEXT
    assert "ensure_termtex" in TEXT
    assert "go install github.com/doug/termtex/cmd/termtex@latest" in TEXT


def test_tmux_install_is_guarded_by_consent() -> None:
    assert 'ask_yes "install tmux?"' in TEXT
    assert "pkg_install tmux" in TEXT


def test_verification_uses_the_installed_channel_binary() -> None:
    """升级校验必须看「刚装的那份」，不是 PATH 上第一个 myrtui。

    真实踩到：pipx 升级成功，但 PATH 里更靠前的是仓库 venv 的旧版
    （0.1.9）→ 打印「升级完成：0.1.9 → 0.1.9」+「安装成功：0.1.9」。
    """
    assert "installed_bin()" in TEXT
    assert 'mrtui_bin=$(installed_bin || true)' in TEXT
    assert "pipx environment --value PIPX_BIN_DIR" in TEXT
    assert "uv tool dir" in TEXT
    assert "myrtui on PATH" in TEXT


def test_install_source_is_pypi() -> None:
    """v0.4.4：安装源只有 PyPI，Gitea 的 API / 资产下载 / token 逻辑必须删干净。

    只扫**可执行行**：文件头注释允许保留迁移说明（「旧版的 Gitea Release API
    查询已删除」这种史料，删掉反而让后来者不知道为何是现在这样）。
    """
    assert 'SPEC="myrtui"' in TEXT
    assert 'SPEC="myrtui==${MYRTUI_VERSION}"' in TEXT

    code = "\n".join(
        line for line in TEXT.splitlines() if not line.lstrip().startswith("#")
    )
    for banned in (
        "MYRTUI_TOKEN",
        "MYRTUI_RELEASE_BASE",
        "MYRTUI_RELEASE_REPO",
        "MYRTUI_FORCE",
        "gitea",
        "GITEA",
        "browser_download_url",
        "api/v1/repos",
    ):
        assert banned not in code, f"install.sh 的可执行部分不应再出现 {banned!r}"

    # 私网地址（RFC1918）一律不许出现在安装路径上；正则写成通用形式，
    # 避免源码里留下一个"看起来像内网地址"的字面量。
    private = re.compile(
        r"\b(?:10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+)"
    )
    assert not private.search(code), "install.sh 不应包含私网地址"

    # 三条通道都必须用 PyPI 规格（uv / pipx / venv 各一次 + 首装两次）
    assert TEXT.count('"$SPEC"') >= 5


def test_recommended_deps_run_after_install() -> None:
    """推荐组件询问在安装成功之后（且只在一处调用）——brew / apt 失败不能挡住本体。"""
    assert TEXT.count("install_recommended_deps") == 2
    assert TEXT.rstrip().endswith("install_recommended_deps")
