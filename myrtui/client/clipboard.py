"""系统剪贴板通道（v0.13 写 / v0.14 读）：OSC 52 之外的**兜底**路径。

Textual 的 ``App.copy_to_clipboard`` 走 OSC 52（``ESC ]52;c;<base64> BEL``），
在 iTerm2 / WezTerm / kitty / Windows Terminal 上有效，但 **macOS
Terminal.app 不支持**（Textual 自己的 docstring 也标了这条：*"This does not
work on macOS Terminal"*）→ 于是再补一条系统命令通道：

- macOS：``pbcopy`` / ``pbpaste``
- Linux/Wayland：``wl-copy`` / ``wl-paste``；Linux/X11：``xclip`` / ``xsel``
- Windows：``clip.exe`` / ``powershell Set-Clipboard``；读：``Get-Clipboard -Raw``

**写**（v0.13）：两条通道都发——OSC 52 零延迟（但可能被终端忽略），系统命令
可靠（但要起子进程）。判定以系统命令的返回码为准（它写的是真·系统剪贴板）；
系统命令不可用时，OSC 52 仍可能生效，所以**不**把「系统命令失败」当成
「复制失败」。

**读**（v0.14；v0.35 起**优先**）：``Ctrl-a v`` 粘贴时先读系统剪贴板，
读不到（无命令 / 失败）才回落到内部缓冲。

**SSH 会话**（v0.35）：远端机器的系统剪贴板对本地用户无意义 → 复制只走
OSC 52，``Ctrl-a v`` 直接提示用终端自己的粘贴快捷键（``is_remote_session``）。

探测结果**进程内缓存**：``shutil.which`` 只在第一次调用时跑（每个候选一次
``which``，量级毫秒），之后复制/粘贴路径零额外开销。
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

log = logging.getLogger("myrtui.clipboard")


def is_remote_session() -> bool:
    """当前前端是否跑在 SSH 会话里（v0.35）。

    远端会话的「系统剪贴板」是**远端机器**的剪贴板，对本地终端用户没有
    意义（写进去也拿不到）；此时复制只走 OSC 52，粘贴也不再读远端剪贴板。
    任一 SSH 环境变量在场即视为远端。
    """
    return any(
        os.environ.get(key)
        for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")
    )

# 复制命令的超时（秒）：pbcopy/xclip 都是「读 stdin → 写剪贴板 → 退出」，
# 但个别实现会挂住等 EOF → 超时兜底，不让复制动作卡住 UI。
COPY_TIMEOUT = 5.0

# 读命令的超时（秒）：更短——读剪贴板是**交互中**的动作，且 ``xclip -o``
# 遇到挂死的剪贴板属主会一直等（属主进程死了但没注销 selection）。
READ_TIMEOUT = 2.0

# 写通道的探测结果缓存：(_probed, command)
_probed = False
_command: list[str] | None = None

# 读通道的探测结果缓存：(_read_probed, command)
_read_probed = False
_read_command: tuple[list[str], bool] | None = None


def _candidates() -> list[list[str]]:
    """按平台给出**写**候选命令（按优先级）。

    Wayland 优先：``WAYLAND_DISPLAY`` 在场时先试 ``wl-copy``——X11 环境下
    装了 wl-copy 也能跑，但会写错剪贴板（wl-clipboard 与 X11 剪贴板是两套）。
    """
    if sys.platform == "darwin":
        return [["pbcopy"]]
    if sys.platform == "win32":
        return [
            ["clip.exe"],
            ["powershell", "-NoProfile", "-Command", "Set-Clipboard"],
        ]
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    if wayland:
        return [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    return [
        ["xclip", "-selection", "clipboard"],
        ["xsel", "--clipboard", "--input"],
        ["wl-copy"],
    ]


def system_clipboard_command() -> list[str] | None:
    """探测可用的系统剪贴板命令（进程内只探一次）。

    Returns:
        完整命令 argv（含参数），探测不到返回 ``None``。
    """
    global _probed, _command
    if _probed:
        return _command
    _probed = True
    for candidate in _candidates():
        exe = candidate[0]
        path = shutil.which(exe)
        if path is None:
            continue
        _command = [path, *candidate[1:]]
        log.debug("系统剪贴板命令: %s", _command)
        break
    else:
        log.info("未找到系统剪贴板命令（候选: %s）", _candidates())
    return _command


def copy_to_system_clipboard(text: str) -> bool:
    """把文本写进**系统**剪贴板。

    Args:
        text: 要复制的文本（UTF-8）。

    Returns:
        True = 系统剪贴板写入成功；False = 没有可用命令 / 命令失败。
        False **不代表** OSC 52 也失败（见模块 docstring）。
    """
    if not text:
        return False
    command = system_clipboard_command()
    if command is None:
        return False
    try:
        proc = subprocess.run(
            command,
            input=text.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=COPY_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("剪贴板命令失败: %s (%s)", command, exc)
        return False
    if proc.returncode != 0:
        log.warning("剪贴板命令返回码非 0: %s (%s)", command, proc.returncode)
        return False
    return True


# ---------- 读通道（v0.14） ----------

def _read_candidates() -> list[tuple[list[str], bool]]:
    """按平台给出**读**候选命令（按优先级）。

    第二个元素 = 读出后是否**剥掉一个尾部换行**：只有 PowerShell 那条需要
    （``Get-Clipboard -Raw`` 往 stdout 写字符串时，PowerShell 会**多补一个**
    换行 → 不剥掉的话，粘贴会凭空多一次回车）。其余命令都原样输出内容。

    Wayland 优先（与写通道同理由：两套剪贴板不能混）。
    """
    if sys.platform == "darwin":
        return [(["pbpaste"], False)]
    if sys.platform == "win32":
        return [
            (["powershell", "-NoProfile", "-Command", "Get-Clipboard -Raw"], True),
        ]
    wayland = bool(os.environ.get("WAYLAND_DISPLAY"))
    if wayland:
        return [
            (["wl-paste", "--no-newline"], False),
            (["xclip", "-selection", "clipboard", "-o"], False),
            (["xsel", "--clipboard", "--output"], False),
        ]
    return [
        (["xclip", "-selection", "clipboard", "-o"], False),
        (["xsel", "--clipboard", "--output"], False),
        (["wl-paste", "--no-newline"], False),
    ]


def system_clipboard_read_command() -> tuple[list[str], bool] | None:
    """探测可用的系统剪贴板**读**命令（进程内只探一次）。

    Returns:
        ``(argv, 剥尾换行?)``；探测不到返回 ``None``。
    """
    global _read_probed, _read_command
    if _read_probed:
        return _read_command
    _read_probed = True
    for candidate, strip_newline in _read_candidates():
        exe = candidate[0]
        path = shutil.which(exe)
        if path is None:
            continue
        _read_command = ([path, *candidate[1:]], strip_newline)
        log.debug("系统剪贴板读命令: %s", _read_command[0])
        break
    else:
        log.info("未找到系统剪贴板读命令（候选: %s）", _read_candidates())
    return _read_command


def read_system_clipboard() -> str | None:
    """读**系统**剪贴板。

    Returns:
        剪贴板文本；命令不可用 / 超时 / 返回码非 0 → ``None``。
        **空剪贴板返回 ``""``**（不是 ``None``）——「读到了但是空的」与
        「压根读不到」要分得开（调用方据此给不同提示）。
    """
    command = system_clipboard_read_command()
    if command is None:
        return None
    argv, strip_newline = command
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=READ_TIMEOUT,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("读剪贴板失败: %s (%s)", argv, exc)
        return None
    if proc.returncode != 0:
        log.warning("读剪贴板返回码非 0: %s (%s)", argv, proc.returncode)
        return None
    # 自己按 UTF-8 解码（不靠 locale）：剪贴板内容是用户放的文本，UTF-8 是
    # 事实标准；解不出來的字节用替换字符兜底，绝不让一条坏字节毁掉整次粘贴。
    text = proc.stdout.decode("utf-8", errors="replace")
    if strip_newline:
        if text.endswith("\r\n"):
            text = text[:-2]
        elif text.endswith("\n"):
            text = text[:-1]
    return text

