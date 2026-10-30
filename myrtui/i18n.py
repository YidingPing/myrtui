"""UI 文案国际化（默认 ``en``，``zh_CN`` 供所有者日常使用）。

设计要点（理由都在 GITHUB-PUBLISH-PLAN §4，这里只记实现约束）：

- **纯数据模块，不 import Textual**：语言解析与查表能脱离 UI 单测。
- **``t()`` 禁止在模块级调用**。模块级求值会在 import 时把语言固化，而
  ``tests/conftest.py`` 的 autouse 夹具晚于 import 执行，钉语言就钉不动它；
  ``tests/test_i18n.py::test_no_module_level_t_call`` 用 AST 守着这条。
- **缺 key 原样返回 key**：开发期一眼可见漏翻，不抛异常、不吞成空串。
- 语言优先级：``--lang``（``set_lang``）> ``MYRTUI_LANG`` > 用户配置
  ``<user_data_dir>/myrtui/config.json`` 的 ``lang`` 字段 > ``LC_ALL`` /
  ``LC_MESSAGES`` / ``LANG`` 是否为 ``zh*`` > ``en``。
- 不做运行时热切换：改语言靠重启（避免 Textual 全树重绘的复杂度）。
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from myrtui.locales import en, zh_CN

log = logging.getLogger("myrtui.i18n")

LANGUAGES: tuple[str, ...] = ("en", "zh_CN")
DEFAULT_LANG = "en"

#: 环境变量：所有者本机在 shell rc 里 ``export MYRTUI_LANG=zh_CN`` 即可。
LANG_ENV = "MYRTUI_LANG"

#: 测试用：把配置文件指到临时路径（生产走 platformdirs 的用户数据目录）。
CONFIG_ENV = "MYRTUI_CONFIG"

_CATALOGS: dict[str, dict[str, str]] = {
    "en": en.MESSAGES,
    "zh_CN": zh_CN.MESSAGES,
}

_override: str | None = None
_cached: str | None = None


def normalize_lang(raw: str | None) -> str | None:
    """把 ``zh_CN.UTF-8`` / ``zh-cn`` / ``en_US`` 归一成词表里的语言名。

    认不出来返回 ``None``（调用方继续沿优先级往下找），不抛异常：
    语言是「尽力而为」的东西，坏配置不该让应用起不来。
    """
    if not raw:
        return None
    code = raw.strip().lower().replace("-", "_")
    if code.startswith("zh"):
        return "zh_CN"
    if code.startswith("en"):
        return "en"
    return None


def config_path() -> Path:
    """用户配置文件位置（``{MYRTUI_CONFIG}`` 可覆盖，供测试隔离）。"""
    override = os.environ.get(CONFIG_ENV)
    if override:
        return Path(override)
    import platformdirs  # 延迟导入：单测不需要它

    return Path(platformdirs.user_data_dir("myrtui", appauthor=False)) / "config.json"


def _config_lang() -> str | None:
    """读 ``config.json`` 的 ``lang`` 字段；任何异常都当没配。"""
    try:
        data = json.loads(config_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return normalize_lang(data.get("lang") if isinstance(data.get("lang"), str) else None)


def resolve_lang() -> str:
    """按优先级算出当前语言（带进程内缓存，``reset_lang`` 清）。"""
    global _cached
    if _cached is not None:
        return _cached
    resolved: str | None = _override
    if resolved is None:
        resolved = normalize_lang(os.environ.get(LANG_ENV)) or _config_lang()
    if resolved is None:
        for var in ("LC_ALL", "LC_MESSAGES", "LANG"):
            resolved = normalize_lang(os.environ.get(var))
            if resolved is not None:
                break
    _cached = resolved or DEFAULT_LANG
    return _cached


def set_lang(lang: str | None) -> None:
    """``--lang`` 单次覆盖；``None`` = 清掉覆盖，回到环境/配置。"""
    global _override, _cached
    if lang is not None and lang not in LANGUAGES:
        raise ValueError(f"unknown language: {lang!r} (available: {', '.join(LANGUAGES)})")
    _override = lang
    _cached = None


def reset_lang() -> None:
    """清掉覆盖与缓存（测试夹具用；也让 ``resolve_lang`` 重新读环境）。"""
    global _override, _cached
    _override = None
    _cached = None


def t(key: str, **kw) -> str:
    """查表 + ``str.format``。

    回落链：当前语言 → ``en`` → key 本身。占位符不匹配时返回未格式化的原文
    并记一条 warning（跨语言占位符由 ``tests/test_i18n.py`` 静态守住，
    运行期不该因为这个把整个界面炸掉）。
    """
    catalog = _CATALOGS.get(resolve_lang(), {})
    text = catalog.get(key)
    if text is None:
        text = _CATALOGS[DEFAULT_LANG].get(key)
    if text is None:
        log.warning("missing i18n key: %s", key)
        return key
    if not kw:
        return text
    try:
        return text.format(**kw)
    except (KeyError, IndexError, ValueError):
        log.warning("i18n placeholder mismatch for %s: %r", key, text)
        return text


__all__ = [
    "CONFIG_ENV",
    "DEFAULT_LANG",
    "LANG_ENV",
    "LANGUAGES",
    "config_path",
    "normalize_lang",
    "reset_lang",
    "resolve_lang",
    "set_lang",
    "t",
]
