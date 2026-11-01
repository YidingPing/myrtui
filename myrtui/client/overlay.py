"""被盖住的屏幕不再驱动画帧（0.6.1）。

**问题**：Textual 8.2.8 的 ``Screen._compositor_refresh`` 会把**背景屏**的脏区
抄到顶层屏（``app.screen.refresh(*dirty)``），随后又把背景屏自己重新标脏
（``self._repaint_required = True``）——于是「背景屏有东西要画 → 顶层屏重画对应
区域 → 背景屏又被标脏」自成一个循环。顶层若是整屏 Sixel 页（``v``），每一次都是
把整张图（2520×1260 实测 ≈95 KiB）重发一遍：实测 2 帧/秒，用户看到的就是**不翻页
也在闪**。而 ``Screen._on_idle`` 只做一件事——「有待画标记就把更新定时器续上」，
它对背景屏（``is_current`` 包含 ``app._background_screens``）同样生效，所以这个
循环永远停不下来。

**做法**：混入本类的屏幕在**不是顶层**时直接跳过那一步（其余空闲回调照跑，恢复
时 ``Screen._on_screen_resume`` 会 ``refresh()`` 整屏，画面不会丢）。被盖住的屏幕
本来也看不见，没人需要它的帧。
"""
from __future__ import annotations

from textual import events


class QuietWhenCovered:
    """混入屏基类（写在 Textual 屏类之前）：被盖住时不跑更新定时器。"""

    async def _on_idle(self, event: events.Idle) -> None:
        try:
            covered = self.app.screen is not self
        except Exception:  # noqa: BLE001 —— teardown 竞态：按「被盖住」处理
            covered = True
        if not covered:
            await super()._on_idle(event)  # type: ignore[misc]
            return
        # 与 Screen._on_idle 的背景分支等价，只是不 resume 更新定时器。
        event.prevent_default()
        await self._invoke_and_clear_callbacks()  # type: ignore[attr-defined]
