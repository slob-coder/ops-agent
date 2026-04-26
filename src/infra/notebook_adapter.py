"""
notebook_adapter.py — Notebook 工厂

自动检测已安装的 Notebook 扩展包，选择增强实现或降级到 Basic：
- 扩展包已安装（如 smart_notebook） → 使用扩展实现
- 未安装 → Basic Notebook（文件系统 + git）

零配置：import 成功就是增强模式，失败就是 Basic。

用法::

    from src.infra.notebook_adapter import create_notebook
    notebook = create_notebook(notebook_path="notebook", llm=self.llm)
"""

from __future__ import annotations

import logging
from typing import Any

from src.infra.notebook import Notebook, NotebookProtocol

logger = logging.getLogger("ops-agent.notebook_adapter")

# 已知的 Notebook 扩展包列表，按优先级排序
# 扩展包必须暴露 create_bridge(notebook_path, llm) -> NotebookProtocol
_KNOWN_EXTENSIONS = ["smart_notebook"]


def create_notebook(notebook_path: str = "notebook",
                    llm: Any = None) -> NotebookProtocol:
    """
    工厂函数：创建 Notebook 实例。

    Args:
        notebook_path: notebook 目录路径
        llm: LLM 实例（扩展包可能需要）

    Returns:
        符合 NotebookProtocol 的实例（扩展实现或 Basic）。

    降级策略:
        1. 按优先级尝试 import 已知扩展包
           a. 成功 → 调用 create_bridge() → 返回扩展实现
           b. 失败 → 尝试下一个
        2. 所有扩展都不可用 → Basic Notebook
    """
    for ext_name in _KNOWN_EXTENSIONS:
        try:
            ext = __import__(ext_name)
            bridge = ext.create_bridge(
                notebook_path=notebook_path, llm=llm,
            )
            logger.info(f"Notebook extension loaded: {ext_name}")
            return bridge
        except ImportError:
            continue
        except Exception as e:
            logger.warning(f"Notebook extension {ext_name} init failed: {e}")
            continue

    logger.info("No notebook extension installed → using basic mode")
    notebook = Notebook(notebook_path)
    logger.info("Basic Notebook initialized")
    return notebook
