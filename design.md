# ops-agent i18n 设计文档

> 版本：v2.2.4 → 新增 i18n feature
> 日期：2026-05-06
> 原则：原项目改造，不影响现有功能，默认行为不变

---

## 1. 目标

为 ops-agent 新增多语言支持，首期覆盖中文（zh）和英文（en）。所有用户可见文本（终端输出、IM 通知、LLM prompt）均可按语言切换，默认保持 zh。

## 2. 语言优先级

```
--lang CLI 参数 > OPS_AGENT_LANG 环境变量 > config/lang.yaml > 默认 zh
```

- 默认 zh 保证现有用户零感知
- `--lang` 参数加在主 parser 上，所有子命令共享

## 3. 双轨架构

### 3.1 轨道一：YAML 字典（代码中的硬编码文本）

**文件结构：**
```
src/i18n/
├── __init__.py      # t() 函数 + 初始化逻辑
├── zh.yaml          # 中文字典
└── en.yaml          # 英文字典
```

**zh.yaml 示例：**
```yaml
core:
  patrol_log: "巡检中... [target={name}, mode={mode}]"
  emergency_stop: "🚨 紧急停止已激活: {reason}。已切换到只读模式。"
  llm_degraded: "🚨 LLM 调用持续失败，已切换到只读模式。\n原因: {reason}\n我会每 5 分钟尝试自动恢复。请检查 API key / 网络。"
  internal_error: "我遇到了内部错误：{error}，继续工作。"
  interrupted: "已中断当前任务（{error_type}）"
  recovered: "⚠️ 检测到上次未完成的工作 (incident={incident}),已恢复状态"

incident:
  found: "[{target}] 发现异常（严重度 {severity}/10）：{summary}"
  diagnose_rounds_exceeded: "已调查 {rounds} 轮仍无定论，"
  fix_attempts_failed: "⚠️ {attempts} 次修复尝试均失败，"
  fix_plan_failed: "⚠️ 无法制定修复方案:\n{summary}"
  verify_failed: "验证未通过 (尝试 {attempt}/{max})"

check:
  config_error: "❌ 配置有 {count} 个错误，请修复后重试"
  config_ok_warn: "✅ 配置基本完整，有警告项建议处理"
  config_ok: "✅ 配置完整，可以启动"
  targets_format_error: "targets.yaml 格式错误: {error}"
  limits_format_error: "limits.yaml 格式错误: {error}"
  notifier_format_error: "notifier.yaml 格式错误: {error}"
  llm_test_failed: "LLM 连通性测试失败: {error}"

chat:
  operation_cancelled: "操作已取消（来自 {source}）。"

notifier:
  observe_failed: "观察函数连续 3 次失败: {error}"
  anomaly_recurred: "检测到原异常复发: {signature}"
  observation_done: "观察 {duration}s,{checks} 次检查,无复发"
```

**en.yaml 示例：**
```yaml
core:
  patrol_log: "Patrolling... [target={name}, mode={mode}]"
  emergency_stop: "🚨 Emergency stop activated: {reason}. Switched to read-only mode."
  llm_degraded: "🚨 LLM calls keep failing, switched to read-only mode.\nReason: {reason}\nI'll try auto-recovery every 5 min. Check API key / network."
  internal_error: "Encountered internal error: {error}, continuing."
  interrupted: "Current task interrupted ({error_type})"
  recovered: "⚠️ Detected unfinished work (incident={incident}), state recovered"

incident:
  found: "[{target}] Anomaly detected (severity {severity}/10): {summary}"
  diagnose_rounds_exceeded: "Investigated {rounds} rounds without conclusion,"
  fix_attempts_failed: "⚠️ {attempts} fix attempts all failed,"
  fix_plan_failed: "⚠️ Cannot create fix plan:\n{summary}"
  verify_failed: "Verification failed (attempt {attempt}/{max})"

check:
  config_error: "❌ {count} config errors, please fix and retry"
  config_ok_warn: "✅ Config mostly complete, warnings should be addressed"
  config_ok: "✅ Config complete, ready to start"
  targets_format_error: "targets.yaml format error: {error}"
  limits_format_error: "limits.yaml format error: {error}"
  notifier_format_error: "notifier.yaml format error: {error}"
  llm_test_failed: "LLM connectivity test failed: {error}"

chat:
  operation_cancelled: "Operation cancelled (from {source})."

notifier:
  observe_failed: "Observe function failed 3 times: {error}"
  anomaly_recurred: "Anomaly recurrence detected: {signature}"
  observation_done: "Observed {duration}s, {checks} checks, no recurrence"
```

### 3.2 轨道二：Prompt 目录（LLM 使用的 prompt 模板）

**文件结构：**
```
prompts/
├── zh/               # 中文 prompt（从现有文件迁移）
│   ├── system.md
│   ├── observe.md
│   ├── assess.md
│   ├── diagnose.md
│   ├── plan.md
│   ├── verify.md
│   └── reflect.md
├── en/               # 英文 prompt
│   ├── system.md
│   ├── observe.md
│   ├── assess.md
│   ├── diagnose.md
│   ├── plan.md
│   ├── verify.md
│   └── reflect.md
└── _fallback/        # 兼容层：旧路径重定向
    └── (symlinks → ../zh/)
```

**迁移策略：**
1. `prompts/*.md` → `prompts/zh/*.md`（git mv，保留历史）
2. `prompts/*.md` 原位置创建兼容 symlinks 指向 `zh/`，确保旧路径仍可用
3. `_load_prompt()` 改为先查 `prompts/{lang}/`，fallback 到 `prompts/`（旧路径），再 fallback 到 `zh/`

## 4. 核心代码：`src/i18n/__init__.py`

```python
"""ops-agent i18n — 轻量多语言支持"""

import os
import logging
from pathlib import Path
from functools import lru_cache

import yaml

logger = logging.getLogger("ops-agent.i18n")

# 全局语言设置，模块加载时初始化
_current_lang: str = "zh"
_dicts: dict[str, dict] = {}

def init(lang: str | None = None):
    """初始化 i18n，加载字典。lang=None 时按优先级自动检测。"""
    global _current_lang, _dicts
    
    if lang is None:
        lang = _detect_lang()
    
    _current_lang = lang
    _dicts.clear()
    
    i18n_dir = Path(__file__).parent
    for lang_file in i18n_dir.glob("*.yaml"):
        lang_code = lang_file.stem
        with open(lang_file, encoding="utf-8") as f:
            _dicts[lang_code] = yaml.safe_load(f) or {}
    
    logger.debug(f"i18n initialized: lang={_current_lang}, dicts={list(_dicts.keys())}")

def _detect_lang() -> str:
    """按优先级检测语言：环境变量 > 配置文件 > 默认 zh"""
    # 1. 环境变量
    env_lang = os.environ.get("OPS_AGENT_LANG")
    if env_lang and env_lang in ("zh", "en"):
        return env_lang
    
    # 2. 配置文件
    config_path = Path(os.environ.get("OPS_WORKSPACE", "~/.ops-agent")).expanduser() / "config" / "lang.yaml"
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
```

## 5. 修改清单

### 5.1 `main.py` — CLI 参数 + 初始化

```python
# 新增 --lang 参数（在主 parser 上）
parser.add_argument("--lang", choices=["zh", "en"], default=None,
                    help="Language (zh/en), default: zh")

# main() 开头初始化 i18n
from src.i18n import init as i18n_init, set_lang
i18n_init()
if args.lang:
    set_lang(args.lang)
```

### 5.2 `src/agent/prompt_engine.py` — Prompt 加载改造

```python
from src.i18n import get_lang

def _load_prompt(self, name: str) -> str:
    """加载 prompt 模板，优先从语言目录加载"""
    if name not in self._prompts:
        prompts_root = Path(__file__).parent.parent.parent / "prompts"
        lang = get_lang()
        
        # 1. 语言目录: prompts/{lang}/{name}.md
        lang_path = prompts_root / lang / f"{name}.md"
        # 2. 旧路径: prompts/{name}.md（兼容）
        fallback_path = prompts_root / f"{name}.md"
        # 3. 中文兜底: prompts/zh/{name}.md
        zh_path = prompts_root / "zh" / f"{name}.md"
        
        for p in [lang_path, fallback_path, zh_path]:
            if p.exists():
                self._prompts[name] = p.read_text(encoding="utf-8")
                break
        else:
            raise FileNotFoundError(f"Prompt not found: {name}")
    
    return self._prompts[name]
```

### 5.3 各模块硬编码文本替换

| 文件 | 修改点数 | 类型 |
|------|---------|------|
| `src/core.py` | ~25 | `chat.say()`, `chat.log()`, `chat.notify()` 中的 f-string |
| `src/infra/chat.py` | ~5 | 用户交互提示 |
| `src/infra/notifier.py` | ~3 | 通知消息 |
| `src/infra/production_watcher.py` | ~3 | 监控消息 |
| `src/infra/notebook.py` | ~5 | notebook 初始化文本 |
| `src/check.py` | ~15 | 配置校验输出 |
| `src/trace_viewer/cli.py` | ~8 | CLI 输出 |
| `src/safety/*.py` | ~5 | 安全审计消息 |
| `src/init.py` | ~10 | 交互式引导文本 |
| **合计** | **~80** | |

> 注：之前估算 120 处，实际扫描约 80 处需要替换的面向用户文本。纯内部 debug 日志（logger.debug/info）不做 i18n。

### 5.4 Prompt 翻译

| Prompt | 中文（已有） | 英文（新增） |
|--------|------------|------------|
| system.md | ✅ | 翻译 |
| observe.md | ✅ | 翻译 |
| assess.md | ✅ | 翻译 |
| diagnose.md | ✅ | 翻译 |
| plan.md | ✅ | 翻译 |
| verify.md | ✅ | 翻译 |
| reflect.md | ✅ | 翻译 |

## 6. 兼容性保障

1. **默认 zh**：不改语言设置时行为完全一致
2. **Prompt 兼容**：`_load_prompt()` 有三层 fallback，旧路径仍可用
3. **无 i18n 模块时**：`t()` 函数 graceful 降级，返回 key 本身
4. **YAML 字典缺失 key**：fallback 到 zh，再 fallback 返回 key
5. **不修改任何公共 API 签名**

## 7. Subtask 拆分

### Subtask 1：基础设施 + Prompt 国际化

**范围：**
- 创建 `src/i18n/` 模块（`__init__.py`, `zh.yaml`, `en.yaml`）
- 修改 `main.py` 添加 `--lang` 参数和初始化调用
- `prompts/*.md` → `prompts/zh/*.md`（git mv）+ 兼容 symlinks
- 创建 `prompts/en/*.md`（7 个英文翻译）
- 修改 `prompt_engine.py` 支持语言目录
- 替换 `src/core.py` 中的硬编码文本（最核心的 ~25 处）

**预计改动：** ~400 行新增 + ~80 行修改
**风险：** 中（core.py 是主循环，需仔细测试）

### Subtask 2：UI 文本国际化

**范围：**
- 替换 `src/infra/chat.py`, `src/check.py`, `src/init.py`, `src/trace_viewer/cli.py` 中的硬编码
- 补充 `zh.yaml` / `en.yaml` 对应条目
- `src/infra/notebook.py` 初始化文本

**预计改动：** ~150 行修改
**风险：** 低（均为输出文本，无逻辑影响）

### Subtask 3：通知 + 边角模块 + 测试

**范围：**
- 替换 `src/infra/notifier.py`, `src/infra/production_watcher.py`, `src/safety/*.py` 中的硬编码
- 补充 `zh.yaml` / `en.yaml` 对应条目
- 编写 i18n 单元测试（`tests/test_i18n.py`）
- 编写 i18n 集成测试（语言切换、prompt 加载 fallback）
- 清理兼容 symlinks（可选，看是否保留）
- 更新 README 和 USER_GUIDE

**预计改动：** ~100 行修改 + ~150 行测试
**风险：** 低

## 8. 不做的事情

- ❌ 使用 gettext（过度工程化，YAML 更直观）
- ❌ 翻译 logger.debug / logger.info 内部日志
- ❌ 翻译 YAML 配置文件的 key/注释
- ❌ 翻译 notebook 内存储的 markdown 文档（那是运行时生成的数据）
- ❌ 支持超过 2 种语言（首期只 zh/en，架构可扩展）
