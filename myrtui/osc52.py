"""OSC 52 提取：把 pane 内程序的复制请求从控制通道里捡出来。

为什么需要这一层：pane 里的程序（opencode / codex / vim 的 ``"+y``）复制时
发的是一条 OSC 52 转义序列，它写进的是 **pane 的 stdout**，不是用户的终端。
在真终端里这条序列会被终端模拟器直接执行；在 myrtui 里中间隔着一个 tmux
server 和一条控制通道，它要么被 tmux 存进 paste buffer（裸形态，且我们从不
读 buffer），要么被直接丢弃（``ESC Ptmux;`` 包裹形态——opencode 在 ``$TMUX``
在场时**总是**发这种，见其 ``clipboard.ts``）。实测 tmux 3.8：包裹形态连
buffer 都不建，但两种形态的原始字节**都**会出现在 ``%output`` 里——所以本模块
对着 ``%output`` 的负载做提取，是唯一全覆盖的出口。

本模块只做字节搬运，不解释屏幕内容（AGENTS 的「不做终端仿真」仍然成立）。
"""
from __future__ import annotations

import base64
import binascii
import logging
import re

log = logging.getLogger("myrtui.osc52")

# 单条复制的体积上限（解码后）。剪贴板内容来自 pane 内程序，属于不可信输入：
# 超限整条丢弃，绝不让一次 ``cat 大文件 | base64`` 把事件队列撑爆。
MAX_BYTES = 256 * 1024

# ``%output`` 里控制字符与反斜杠是**八进制转义**的（实测 tmux 3.8：
# ESC → ``\033``、``\`` → ``\134``），其余字节原样。先解转义再找序列，
# 否则 ``ESC ]52;`` 会被拆成 ``\033]52;`` 而匹配不到。
_OCTAL_RE = re.compile(r"\\([0-7]{3}|.)", re.DOTALL)

# OSC 52：``ESC ] 52 ; <选择区> ; <base64>`` + BEL 或 ST（``ESC \``）。
# 只认终止符之前的部分；``?`` 是「读剪贴板」请求，绝大多数终端不实现，忽略。
_OSC52 = "\x1b]52;"
_TERMINATORS = ("\x07", "\x1b\\")

# base64 负载的合法性预筛：控制通道是行协议，内容里混进换行 / 引号 / 分号
# 都不该被容忍；不合法的直接当「不是 OSC 52」丢弃，不抛异常。
_B64_RE = re.compile(r"[A-Za-z0-9+/=\s]*\Z")


def unescape(payload: str) -> str:
    """还原 ``%output`` 负载的八进制转义（非 ``\\NNN`` 的转义原样保留）。"""
    if "\\" not in payload:
        return payload

    def _sub(match: re.Match[str]) -> str:
        token = match.group(1)
        if len(token) == 3 and token.isdigit():
            code = int(token, 8)
            if code <= 0xFF:
                return chr(code)
        return token

    return _OCTAL_RE.sub(_sub, payload)


def decode_body(body: str) -> str | None:
    """base64 → 文本；查询请求 / 非法 base64 / 超限一律 ``None``。"""
    body = body.strip()
    if not body or body == "?" or not _B64_RE.match(body):
        return None
    if len(body) > MAX_BYTES * 2:
        log.warning("OSC 52 负载过大（%d 字节 base64），丢弃", len(body))
        return None
    try:
        raw = base64.b64decode(body, validate=False)
    except (binascii.Error, ValueError):
        return None
    if len(raw) > MAX_BYTES:
        log.warning("OSC 52 解码后过大（%d 字节），丢弃", len(raw))
        return None
    # 解码不出文本就没有可粘贴的内容（图片类复制走别的通道）。
    return raw.decode("utf-8", errors="replace")


def extract(payload: str) -> list[str]:
    """从一帧 ``%output`` 负载里取出全部 OSC 52 文本（已解转义）。

    兼容两种形态：

    - 裸 ``ESC ]52;c;<b64> BEL``（或 ``ESC \\`` 结尾）；
    - ``ESC Ptmux; ESC ]52;c;<b64> ESC \\`` —— 内层序列在负载里是**逐字节**
      原样的，所以不需要先剥 DCS 外壳：从 ``ESC ]52;`` 起匹配、以共用的
      ``ESC \\`` 收尾即可。内层 ESC 被程序重复成 ``ESC ESC`` 时同样成立
      （匹配落在第二个 ESC 上）。
    """
    data = unescape(payload)
    if _OSC52 not in data:
        return []
    found: list[str] = []
    index = 0
    while True:
        start = data.find(_OSC52, index)
        if start < 0:
            break
        cursor = start + len(_OSC52)
        sep = data.find(";", cursor)
        if sep < 0:
            break
        selection = data[cursor:sep]
        body_start = sep + 1
        ends = [(data.find(mark, body_start), mark) for mark in _TERMINATORS]
        ends = [item for item in ends if item[0] >= 0]
        if not ends:
            break
        end, terminator = min(ends, key=lambda item: item[0])
        index = end + len(terminator)
        if selection not in ("", "c"):
            continue  # 主选择区（"p"）等：不碰
        body = data[body_start:end]
        # 内层 ESC 被重复成 ``ESC ESC`` 时，终止符匹配落在第二个 ESC 上，
        # body 末尾会多出一个 ESC —— base64 里不可能出现，剥掉即可。
        if body.endswith("\x1b"):
            body = body.rstrip("\x1b")
        text = decode_body(body)
        if text:
            found.append(text)
    return found
