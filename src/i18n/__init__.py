from __future__ import annotations

"""ops-agent i18n — 轻量多语言支持"""

import os
import logging
from pathlib import Path

import yaml

logger = logging.getLogger("ops-agent.i18n")

# 全局语言设置
_current_lang: str = "zh"
_dicts: dict[str, dict] = {}

LANG = property(lambda self: _current_lang)  # module-level accessor via get_lang()


def init(cli_arg: str | None = None, config_value: str | None = None):
    """初始化 i18n，加载字典。

    优先级: cli_arg > 环境变量 > config_value > 默认 zh
    """
    global _current_lang, _dicts

    if cli_arg and cli_arg in ("zh", "en"):
        lang = cli_arg
    else:
        lang = _detect_lang(config_value)

    _current_lang = lang
    _dicts.clear()

    i18n_dir = Path(__file__).parent
    for lang_file in i18n_dir.glob("*.yaml"):
        lang_code = lang_file.stem
        with open(lang_file, encoding="utf-8") as f:
            _dicts[lang_code] = yaml.safe_load(f) or {}

    logger.debug(f"i18n initialized: lang={_current_lang}, dicts={list(_dicts.keys())}")


def _detect_lang(config_value: str | None = None) -> str:
    """按优先级检测语言：环境变量 > 配置文件 > 默认 zh"""
    # 1. 环境变量
    env_lang = os.environ.get("OPS_AGENT_LANG")
    if env_lang and env_lang in ("zh", "en"):
        return env_lang

    # 2. 配置值（从 config/lang.yaml 读取后传入）
    if config_value and config_value in ("zh", "en"):
        return config_value

    # 3. 配置文件
    config_path = Path(
        os.environ.get("OPS_WORKSPACE", "~/.ops-agent")
    ).expanduser() / "config" / "lang.yaml"
    if config_path.exists():
        try:
            with open(config_path, encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            cfg_lang = data.get("lang")
            if cfg_lang in ("zh", "en"):
                return cfg_lang
        except Exception:
            pass

    return "zh"


def get_lang() -> str:
    return _current_lang


def set_lang(lang: str):
    """运行时切换语言（由 --lang 参数调用）"""
    global _current_lang
    if lang in _dicts or lang == "zh":
        _current_lang = lang
    else:
        logger.warning(f"Unknown language: {lang}, fallback to zh")
        _current_lang = "zh"


def t(key: str, **kwargs) -> str:
    """翻译 key，支持 {placeholder} 格式化。key 格式: section.subkey"""
    d = _dicts.get(_current_lang, {})

    # 逐层查找
    parts = key.split(".")
    for part in parts:
        if isinstance(d, dict):
            d = d.get(part)
        else:
            d = None
            break

    if d is None:
        # fallback 到 zh
        d = _dicts.get("zh", {})
        for part in parts:
            if isinstance(d, dict):
                d = d.get(part)
            else:
                d = None
                break

    if d is None:
        logger.warning(f"Missing i18n key: {key}")
        return key

    if not isinstance(d, str):
        return str(d)

    try:
        return d.format(**kwargs)
    except KeyError:
        return d
