"""外层终端复用器探测：判断 myrtui 是不是跑在别人的 tmux / screen 里面。

为什么需要：myrtui 自己虽然用 tmux 管 pane，但**它也可以被套在另一层复用器
里**（UU 远程的终端就是一个自带 tmux 3.5a 的会话）。外层那层如果 `mouse on`，
它的 root 表会先处理鼠标——UU 的配置把 `MouseDrag1Pane` 改成无条件进 copy-mode，
于是按下能到 myrtui、**按住期间的 motion 全被 copy-mode 吃掉**，拖拽选字/转发
整体失效（0.7.0 真机实测：按住 7.5 秒、0 条 motion，松手时 `copy-pipe pbcopy`
把选区写进了系统剪贴板）。

myrtui 无法从内部改变外层复用器的绑定（也不该去改用户的终端配置），能做的是
**认出来并给一次可执行的提示**。本模块只做判断，不碰环境、不 import Textual。
"""
from __future__ import annotations

from collections.abc import Mapping


def outer_multiplexer(env: Mapping[str, str]) -> str | None:
    """返回外层复用器的名字（``"tmux"`` / ``"screen"``），认不出则 ``None``。

    判据按可靠度排序：

    1. ``$TMUX``（tmux 给 pane 里的进程设的 socket 路径）——最硬，但它也可能被
       配置主动 unset（UU 就这么干，好让用户在里面再开自己的 tmux）；
    2. ``$STY``（screen 的会话名）；
    3. ``$TERM_PROGRAM=tmux``——tmux 3.2+ 设它，不随 ``unset TMUX`` 消失；
    4. ``$TERM`` 以 ``tmux`` / ``screen`` 开头（tmux 的默认终端是
       ``tmux-256color`` / ``screen-256color``）。

    注意：**不是**在判断「我是不是 tmux 后端」——那是自己的 server，与本函数
    无关。
    """
    if env.get("TMUX"):
        return "tmux"
    if env.get("STY"):
        return "screen"
    program = (env.get("TERM_PROGRAM") or "").strip().lower()
    if program == "tmux":
        return "tmux"
    term = (env.get("TERM") or "").strip().lower()
    if term.startswith("tmux"):
        return "tmux"
    if term.startswith("screen"):
        return "screen"
    return None


# 提示的开关键：外层复用器在场是常态（UU 用户每次启动都在里面），一直弹一条
# 已经没用处的排障文案就是噪音 → 给一个显式关掉的入口。
HINT_ENV = "MYRTUI_OUTER_MUX_HINT"
_FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def hint_enabled(raw: str | None) -> bool:
    """``MYRTUI_OUTER_MUX_HINT`` → 是否显示启动提示（默认 **True**）。

    口径与 ``MYRTUI_CLIPBOARD_PASSTHROUGH`` 一致：只有明确的假值才关，
    手滑写错的值按默认（开）处理。
    """
    if raw is None or not raw.strip():
        return True
    return raw.strip().lower() not in _FALSE_VALUES
