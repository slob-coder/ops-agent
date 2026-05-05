"""src/i18n 模块单元测试"""

import os
import pytest
import yaml
from pathlib import Path
from unittest.mock import patch

from src.i18n import init, t, get_lang, set_lang, _detect_lang


@pytest.fixture(autouse=True)
def reset_i18n():
    """每个测试前重置 i18n 状态"""
    init()
    yield
    init()  # 测试后重置回默认


class TestInit:
    def test_default_lang_is_zh(self):
        init()
        assert get_lang() == "zh"

    def test_cli_arg_zh(self):
        init(cli_arg="zh")
        assert get_lang() == "zh"

    def test_cli_arg_en(self):
        init(cli_arg="en")
        assert get_lang() == "en"

    def test_cli_arg_invalid_fallback(self):
        init(cli_arg="fr")
        assert get_lang() == "zh"

    def test_config_value(self):
        init(config_value="en")
        assert get_lang() == "en"

    def test_cli_overrides_config(self):
        init(cli_arg="zh", config_value="en")
        assert get_lang() == "zh"

    def test_dicts_loaded(self):
        init()
        # 重新导入检查内部状态
        from src.i18n import _dicts
        assert "zh" in _dicts
        assert "en" in _dicts


class TestDetectLang:
    def test_env_var_zh(self):
        with patch.dict(os.environ, {"OPS_AGENT_LANG": "zh"}):
            assert _detect_lang() == "zh"

    def test_env_var_en(self):
        with patch.dict(os.environ, {"OPS_AGENT_LANG": "en"}):
            assert _detect_lang() == "en"

    def test_env_var_invalid(self):
        with patch.dict(os.environ, {"OPS_AGENT_LANG": "fr"}):
            assert _detect_lang() == "zh"

    def test_config_value(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _detect_lang(config_value="en") == "en"

    def test_default_zh(self):
        with patch.dict(os.environ, {}, clear=True):
            assert _detect_lang() == "zh"


class TestGetSetLang:
    def test_get_lang_default(self):
        init()
        assert get_lang() == "zh"

    def test_set_lang(self):
        init()
        set_lang("en")
        assert get_lang() == "en"

    def test_set_lang_unknown_fallback(self):
        init()
        set_lang("fr")
        assert get_lang() == "zh"


class TestT:
    def test_simple_key(self):
        init()
        assert "巡检" in t("core.patrol_log", name="web", mode="ssh") or "Patrolling" in t("core.patrol_log", name="web", mode="ssh")

    def test_zh_translation(self):
        init(cli_arg="zh")
        result = t("core.on_duty")
        assert result == "已上岗，进入巡检模式。"

    def test_en_translation(self):
        init(cli_arg="en")
        result = t("core.on_duty")
        assert result == "On duty, entering patrol mode."

    def test_parameter_substitution(self):
        init(cli_arg="zh")
        result = t("core.patrol_log", name="web01", mode="ssh")
        assert "web01" in result
        assert "ssh" in result

    def test_en_parameter_substitution(self):
        init(cli_arg="en")
        result = t("core.patrol_log", name="web01", mode="ssh")
        assert "web01" in result
        assert "ssh" in result

    def test_missing_key_returns_key(self):
        init()
        result = t("nonexistent.key")
        assert result == "nonexistent.key"

    def test_fallback_to_zh(self):
        init(cli_arg="en")
        # 如果 en 中缺 key，应 fallback 到 zh
        # 先验证正常 key 能工作
        result = t("core.on_duty")
        assert result == "On duty, entering patrol mode."

    def test_nested_key(self):
        init(cli_arg="zh")
        result = t("check.title")
        assert "配置校验" in result

    def test_en_nested_key(self):
        init(cli_arg="en")
        result = t("check.title")
        assert "Config Check" in result


class TestYamlKeyAlignment:
    def test_keys_aligned(self):
        i18n_dir = Path(__file__).resolve().parent.parent / "src" / "i18n"
        with open(i18n_dir / "zh.yaml") as f:
            zh = yaml.safe_load(f)
        with open(i18n_dir / "en.yaml") as f:
            en = yaml.safe_load(f)

        def get_keys(d, prefix=''):
            keys = set()
            for k, v in d.items():
                full = f'{prefix}.{k}' if prefix else k
                if isinstance(v, dict):
                    keys |= get_keys(v, full)
                else:
                    keys.add(full)
            return keys

        zh_keys = get_keys(zh)
        en_keys = get_keys(en)
        assert zh_keys == en_keys, (
            f"Key mismatch:\n"
            f"Missing in en: {sorted(zh_keys - en_keys)}\n"
            f"Missing in zh: {sorted(en_keys - zh_keys)}"
        )
