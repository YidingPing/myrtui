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

**跨帧重组**（0.9.1）：``%output`` 是 tmux 按 pty 读取块发的，**一帧只有约
1 KiB 输入字节**（实测 tmux 3.8 / macOS：1024）。一条长 OSC 52 会被切成几帧，
而第 2 帧起没有报文头——按帧解析等于把长复制整条丢掉。真机症状：约 750 字节
（十来行）以上的选区在 opencode 里拖拽复制后，剪贴板完全不变且没有任何提示。
所以 :class:`Stream` 按 pane 暂存半条序列，下一帧补完后再解码。
"""
from __future__ import annotations

import base64
import binascii
import logging
import re
import time

log = logging.getLogger("myrtui.osc52")

# 单条复制的体积上限（解码后）。剪贴板内容来自 pane 内程序，属于不可信输入：
# 超限整条丢弃，绝不让一次 ``cat 大文件 | base64`` 把事件队列撑爆。
MAX_BYTES = 256 * 1024

# ``%output`` 里控制字符与反斜杠是**八进制转义**的（实测 tmux 3.8：
# ESC → ``\033``、``\`` → ``\134``），其余字节原样。扫描器直接在 raw 文本上
# 匹配（见 _scan_raw）；``unescape`` 留给需要按字节看内容的调用方与测试。
_OCTAL_RE = re.compile(r"\\([0-7]{3}|.)", re.DOTALL)

# OSC 52：``ESC ] 52 ; <选择区> ; <base64>`` + BEL 或 ST（``ESC \``）。
# 只认终止符之前的部分；``?`` 是「读剪贴板」请求，绝大多数终端不实现，忽略。
# 这里写的是 **raw（转义后）形态**：``%output`` 里 ESC 是 ``\033``、``\`` 是
# ``\134``，扫描器直接在 raw 文本上工作（见 _scan_raw），跨帧拼接才不会把
# 一个转义 token 拼坏。
_RAW_START = "\\033]52;"
_RAW_ESC = "\\033"
_RAW_TERMINATORS = ("\\007", "\\033\\134")

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


def _scan_raw(raw: str) -> tuple[list[str], str]:
    """在**原始（转义后）**负载上扫描 → ``(完整序列, 没写完的尾巴)``。

    直接在 raw 上匹配而不是先解转义：tmux 的 ``%output`` 是逐字节八进制转义
    的（ESC → ``\\033``、``\\`` → ``\\134``），raw 化之后跨帧拼接永远不会把
    一个转义 token 拼坏；body 是 base64，本来就不含转义字符。

    尾巴是给 :class:`Stream` 跨帧拼接用的：

    - 从 ``\\033]52;`` 起：选择区 / 正文还没结束；
    - 报文头残片（``\\`` / ``\\0`` / ``\\03`` / ``\\033`` / ``\\033]`` /
      ``\\033]5`` / ``\\033]52``）：下一帧开头就是剩下的部分；
    - 空串：这一帧干净收尾。

    兼容两种形态（与 0.9.0 相同）：

    - 裸 ``ESC ]52;c;<b64> BEL``（或 ``ESC \\`` 结尾）；
    - ``ESC Ptmux; ESC ]52;c;<b64> ESC \\`` —— 内层序列是**逐字节**原样的，
      所以不需要先剥 DCS 外壳：从 ``\\033]52;`` 起匹配、以共用的
      ``\\033\\134`` 收尾即可。内层 ESC 被程序重复成 ``ESC ESC`` 时同样成立
      （匹配落在第二个 ESC 上）。
    """
    found: list[str] = []
    index = 0
    while True:
        start = raw.find(_RAW_START, index)
        if start < 0:
            return found, _partial_start(raw)
        cursor = start + len(_RAW_START)
        sep = raw.find(";", cursor)
        if sep < 0:
            return found, raw[start:]
        selection = raw[cursor:sep]
        body_start = sep + 1
        ends = [(raw.find(mark, body_start), mark) for mark in _RAW_TERMINATORS]
        ends = [item for item in ends if item[0] >= 0]
        if not ends:
            return found, raw[start:]
        end, terminator = min(ends, key=lambda item: item[0])
        index = end + len(terminator)
        if selection not in ("", "c"):
            continue  # 主选择区（"p"）等：不碰
        body = raw[body_start:end]
        # 内层 ESC 被重复成 ``ESC ESC`` 时，终止符匹配落在第二个 ESC 上，
        # body 末尾会多出一颗 ESC（raw 形态是 ``\033``）——剥掉即可。
        if body.endswith(_RAW_ESC):
            body = body[: -len(_RAW_ESC)]
        text = decode_body(body)
        if text:
            found.append(text)


def _partial_start(raw: str) -> str:
    """raw 末尾的 OSC 52 报文头残片（只在**跨帧**时才会出现）。"""
    for size in range(len(_RAW_START) - 1, 0, -1):
        if raw.endswith(_RAW_START[:size]):
            return raw[-size:]
    return ""


def extract(payload: str) -> list[str]:
    """从**一帧** ``%output`` 负载（原始转义形态）里取出完整 OSC 52 文本。"""
    return _scan_raw(payload)[0]


# ---------- 跨帧重组（0.9.1） ----------
#
# 一条序列只可能被切在**同一个 pane** 的相邻帧里（pty 是字节流，切点只在
# tmux 的读边界），且相邻帧相隔毫秒级；所以按 pane 暂存、超时即弃。超时也
# 兜住「程序写到一半退出」：否则后面任何一条新序列都会被拼进这条僵尸里。
PARTIAL_TIMEOUT = 2.0


class Stream:
    """按 pane 重组跨帧的 OSC 52（0.9.1）。

    用法：每收到一帧 ``%output`` 就 ``feed(pane_id, payload)``，返回这一帧
    （连同之前暂存的前半段）凑齐的全部文本。``payload`` 是**原始转义形态**，
    不含行尾换行。
    """

    def __init__(
        self,
        *,
        max_bytes: int = MAX_BYTES,
        timeout: float = PARTIAL_TIMEOUT,
    ) -> None:
        # 与 decode_body 的 base64 上限同口径，再加报文头余量
        self._max_raw = max_bytes * 2 + 64
        self._timeout = timeout
        self._pending: dict[str, tuple[str, float]] = {}

    def __len__(self) -> int:
        """当前暂存的半条序列个数（诊断 / 测试用）。"""
        return len(self._pending)

    def feed(
        self,
        pane: str,
        payload: str,
        *,
        now: float | None = None,
    ) -> list[str]:
        """喂一帧负载，返回凑齐的文本（可能为空）。"""
        now = time.monotonic() if now is None else now
        pending = self._expire(pane, now)
        if pending is None and "]52;" not in payload:
            # 廉价预筛（刷屏时每帧都走）：本体不在、也没有报文头残片 → 没戏
            suffix = _partial_start(payload)
            if not suffix:
                return []
            self._pending[pane] = (suffix, now)
            return []
        if pending is None:
            data, since = payload, now
        else:
            data, since = pending[0] + payload, pending[1]
        found, tail = _scan_raw(data)
        if not tail:
            self._pending.pop(pane, None)
        elif len(tail) > self._max_raw:
            log.warning(
                "OSC 52 partial sequence too large (%d chars); dropped", len(tail)
            )
            self._pending.pop(pane, None)
        else:
            self._pending[pane] = (tail, since)
        return found

    def _expire(self, pane: str, now: float) -> tuple[str, float] | None:
        """取出该 pane 未完成的半条序列；过期 / 超限则丢弃。"""
        pending = self._pending.pop(pane, None)
        if pending is None:
            return None
        if now - pending[1] > self._timeout or len(pending[0]) > self._max_raw:
            log.debug("OSC 52 partial sequence expired (%s); dropped", pane)
            return None
        return pending
