"""确保 tests/ 下可以 import 项目根目录的模块"""
import sys
from pathlib import Path

# 项目根目录加入 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
