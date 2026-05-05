"""测试配置 — 确保项目根目录在 sys.path 中"""
import sys
from pathlib import Path

# 项目根目录加入 sys.path（供直接运行测试脚本时使用）
_project_root = str(Path(__file__).resolve().parent.parent)
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# 初始化 i18n，确保 t() 可用
from src.i18n import init as _i18n_init
_i18n_init()
