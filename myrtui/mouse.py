"""鼠标事件转发：Textual 鼠标事件 → SGR(1006) 或 xterm 旧 3 字节编码（§10.2）。

背景：v0.6 及以前把「pane 内 TUI 程序的鼠标」列为 v1 非目标（§1.1）——
myrtui 从不 attach tmux client，tmux 收不到输入端鼠标，opencode 等程序
（实测请求 DECSET 1003）一个鼠标字节都收不到。v0.7 起推翻该非目标：
前端把 Textual 鼠标事件自行合成鼠标序列，经 ``send-keys -H`` 注入 pane
的 pty。

**编码维度（v0.7.1，审阅 F3）**：程序能通过编码位区分来源——恒发 SGR 会让
默认配置的 vim（``ttymouse=xterm``，sgr=0）把 x 当行号、点击落点错行。
必须按 pane 的 ``mouse_sgr_flag`` 选编码：

- ``sgr=True``（程序请求 1006）：SGR 序列 ``CSI < b ; x ; y M/m``；
- ``sgr=False``（仅请求 1000/1002/1003）：xterm 旧 3 字节
  ``ESC [ M`` + ``chr(32+b)`` + ``chr(33+x)`` + ``chr(33+y)``（0-based 坐标）。

旧编码公式经 **tmux 翻译 oracle 实证**（PTY attach + mouse on，tmux 为权威
参考实现）+ 复审对 tmux 3.8 源码逐常量核对（`MOUSE_PARAM_BTN_OFF=0x20` /
`POS_OFF=0x21` / `MAX=0xff`）：press ``b=button``、release ``b=3``（无释放
形态、以 b=3 标识）、wheel up/down ``b=64/65``（byte 96/97）、
motion ``b=32|32|btn``（hover=67，`x10_motion` 供 DRAG/ALL 级使用）、
shift/meta/ctrl 修饰 +4/+8/+16（与 SGR 同位）。motion 是否转发由**门控**
决定而与编码无关：**CLICK 级别不转发 motion**（tmux 在 1000 模式下同样
不转发，oracle 实证）；DRAG/ALL 级下 motion 正常经旧编码发出。
坐标单字节上限 223（0-based clamp ≤222，byte ≤255）——超宽 pane 不回退
SGR（那等于在宽 pane 重演 F3），与「按程序请求编码」原则一致。

门控依据 pane 内程序请求的追踪模式（tmux ``mouse_*_flag``，实测三态互斥、
后设覆盖、``any`` 为聚合位、``all`` 直读 1003，tmux 3.8）::

    程序请求      std  btn  any  all  级别
    无             0    0    0    0    NONE(0)
    1000（点击）   1    0    1    0    CLICK(1)
    1002（+拖动）  0    1    1    0    DRAG(2)
    1003（+移动）  0    0    1    1    ALL(3)   ← 1003 覆盖 std/btn

转发规则（§10.2）：
- press / release / wheel：级别 ≥ CLICK；
- 拖动 motion（有按钮）：级别 ≥ DRAG，**目标 = 按下时的 pane**（越界坐标
  clamp 进该 pane，保证 release 必达、不卡按住状态）；
- hover motion（无按钮）：级别 = ALL，指针下 pane；
- motion 在 App 侧按 30ms 合并节流（1003 下每次移动一次 exec 不可承受）。
"""
from __future__ import annotations

# 门控级别（见模块 docstring 的实测矩阵）
LEVEL_NONE = 0
LEVEL_CLICK = 1
LEVEL_DRAG = 2
LEVEL_ALL = 3

# 各类事件所需的最低级别
_KIND_MIN_LEVEL = {
    "press": LEVEL_CLICK,
    "release": LEVEL_CLICK,
    "wheel": LEVEL_CLICK,
    "drag": LEVEL_DRAG,
    "motion": LEVEL_ALL,
}


def decode_level(standard: bool, button: bool, any_: bool,
                 all_: bool = False) -> int:
    """tmux ``mouse_*_flag`` 四元组 → 门控级别（v0.7.1：``all`` 直读 1003）。

    优先级：``all``(1003) → ``button``(1002) → ``standard``(1000)。
    ``any`` 是聚合位（任一模式启用即亮），单独亮而三模式皆灭属未观测态
    （可能 1005/1015 等扩展）→ 保守给 CLICK（少转发优于错转发）。
    注：该 ``any`` 兜底在当前 tmux 语义下**不可达**（any 即三模式聚合位），
    属防御性分支——保留以防 tmux 未来扩展 flag 语义（复审 §5-E 认定无害）。
    """
    if all_:
        return LEVEL_ALL
    if button:
        return LEVEL_DRAG
    if standard:
        return LEVEL_CLICK
    if any_:
        return LEVEL_CLICK
    return LEVEL_NONE


def can_forward(level: int, kind: str) -> bool:
    """``kind`` ∈ press/release/wheel/drag/motion，返回该级别是否放行。"""
    return level >= _KIND_MIN_LEVEL[kind]


# ---------- 编码：SGR(1006) ----------

def _mods(shift: bool, meta: bool, ctrl: bool) -> int:
    b = 0
    if shift:
        b |= 4
    if meta:
        b |= 8
    if ctrl:
        b |= 16
    return b


# Textual button（1=左 2=中 3=右）→ SGR 按钮码（0/1/2）
_BTN = {1: 0, 2: 1, 3: 2}
# Textual MouseMove.button（0=无 1=左 2=中 3=右）→ SGR motion 按钮码（3=无）
_MOTION_BTN = {0: 3, 1: 0, 2: 1, 3: 2}

# 旧编码坐标上限（0-based）：33 + 222 = 255（单字节）
X10_MAX_COORD = 222


def _sgr_seq(x: int, y: int, buttons: int, press: bool) -> str:
    # SGR 1006：CSI < b ; x ; y M（press/motion）/ m（release）；坐标 1-based（N9 同口径
    # 的坐标侧：cell 坐标直接 +1，不做 len 运算）
    return f"\x1b[<{buttons};{x + 1};{y + 1}{'M' if press else 'm'}"


def sgr_press(x: int, y: int, button: int,
              shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    """按下序列。``button`` 为 Textual 口径（1=左 2=中 3=右）。"""
    return _sgr_seq(x, y, _BTN.get(button, 0) | _mods(shift, meta, ctrl), True)


def sgr_release(x: int, y: int, button: int,
                shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    """释放序列（SGR 用小写 ``m`` 区分 release）。"""
    return _sgr_seq(x, y, _BTN.get(button, 0) | _mods(shift, meta, ctrl), False)


def sgr_wheel(up: bool, x: int, y: int,
              shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    """滚轮序列：64=上 65=下（按 press 形态发出，xterm/TUI 惯例只需 press）。"""
    return _sgr_seq(x, y, (64 if up else 65) | _mods(shift, meta, ctrl), True)


def sgr_motion(x: int, y: int, button: int = 0,
               shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    """移动序列：32=motion 位 | 按钮码（无按钮=3 → 35=纯 hover）。"""
    return _sgr_seq(x, y, 32 | _MOTION_BTN.get(button, 3) | _mods(shift, meta, ctrl), True)


# ---------- 编码：xterm 旧 3 字节（sgr=0，审阅 F3；字节形态 tmux oracle 实证） ----------

def _x10_seq(x: int, y: int, b: int) -> str:
    """``ESC [ M`` + chr(32+b) + chr(33+x) + chr(33+y)（0-based 坐标，clamp ≤222）。

    chr 结果可含 >0x7f 字节（宽 pane）——注入侧必须 latin-1 编码（见
    ``send_mouse``），不能 UTF-8（会把单字节打成双字节、三字节帧全错）。
    """
    cx = min(max(0, x), X10_MAX_COORD)
    cy = min(max(0, y), X10_MAX_COORD)
    return f"\x1b[M{chr(32 + b)}{chr(33 + cx)}{chr(33 + cy)}"


def x10_press(x: int, y: int, button: int,
              shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    return _x10_seq(x, y, _BTN.get(button, 0) | _mods(shift, meta, ctrl))


def x10_release(x: int, y: int, button: int,
                shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    # 旧编码无独立释放形态：b=3（无按钮）标识 release（oracle 实证 byte 35='#'）
    return _x10_seq(x, y, 3 | _mods(shift, meta, ctrl))


def x10_wheel(up: bool, x: int, y: int,
              shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    # oracle 实证：wheel up byte1=96(32+64)、down=97(32+65)
    return _x10_seq(x, y, (64 if up else 65) | _mods(shift, meta, ctrl))


def x10_motion(x: int, y: int, button: int = 0,
               shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    # oracle 实证（1002）：drag byte1=64(32+32+0)、hover(32|3)=67
    return _x10_seq(x, y, 32 | _MOTION_BTN.get(button, 3) | _mods(shift, meta, ctrl))


# ---------- dispatch：按 pane 编码选合成器（v0.7.1 F3） ----------

def press(x: int, y: int, button: int, sgr: bool, *,
          shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    fn = sgr_press if sgr else x10_press
    return fn(x, y, button, shift, meta, ctrl)


def release(x: int, y: int, button: int, sgr: bool, *,
            shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    fn = sgr_release if sgr else x10_release
    return fn(x, y, button, shift, meta, ctrl)


def wheel(up: bool, x: int, y: int, sgr: bool, *,
          shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    fn = sgr_wheel if sgr else x10_wheel
    return fn(up, x, y, shift, meta, ctrl)


def motion(x: int, y: int, button: int, sgr: bool, *,
           shift: bool = False, meta: bool = False, ctrl: bool = False) -> str:
    fn = sgr_motion if sgr else x10_motion
    return fn(x, y, button, shift, meta, ctrl)
