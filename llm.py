"""
LLM 客户端抽象层
支持 Anthropic / OpenAI / 本地模型，通过环境变量切换
"""

import os
import json
import logging

logger = logging.getLogger("ops-agent.llm")


class LLMClient:
    """统一的 LLM 调用接口"""

    # 各 Provider 的默认配置
    PROVIDER_DEFAULTS = {
        "anthropic": {
            "model": "claude-sonnet-4-20250514",
            "base_url": "",
        },
        "openai": {
            "model": "gpt-4o",
            "base_url": "",
        },
        "zhipu": {
            "model": "glm-4-plus",
            "base_url": "https://open.bigmodel.cn/api/paas/v4/",
        },
    }

    def __init__(self):
        self.provider = os.getenv("OPS_LLM_PROVIDER", "anthropic").lower()
        if self.provider not in self.PROVIDER_DEFAULTS:
            raise ValueError(
                f"Unsupported provider: {self.provider}. "
                f"Supported: {list(self.PROVIDER_DEFAULTS.keys())}"
            )

        defaults = self.PROVIDER_DEFAULTS[self.provider]
        self.model = os.getenv("OPS_LLM_MODEL") or defaults["model"]
        self.base_url = os.getenv("OPS_LLM_BASE_URL") or defaults["base_url"]
        self.api_key = os.getenv("OPS_LLM_API_KEY", "")
        self._client = None

    def _get_client(self):
        if self._client:
            return self._client

        if self.provider == "anthropic":
            try:
                import anthropic
                self._client = anthropic.Anthropic(api_key=self.api_key or None)
            except ImportError:
                raise RuntimeError("pip install anthropic")

        elif self.provider in ("openai", "zhipu"):
            # 智谱兼容 OpenAI SDK，复用 openai 客户端
            try:
                import openai
                kwargs = {}
                if self.api_key:
                    kwargs["api_key"] = self.api_key
                if self.base_url:
                    kwargs["base_url"] = self.base_url
                self._client = openai.OpenAI(**kwargs)
            except ImportError:
                raise RuntimeError("pip install openai")

        return self._client

    def ask(self, prompt: str, system: str = "", max_tokens: int = 4096) -> str:
        """向 LLM 提问，返回纯文本回答"""
        client = self._get_client()

        logger.debug(f"LLM request ({self.provider}/{self.model}): {prompt[:100]}...")

        try:
            if self.provider == "anthropic":
                kwargs = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if system:
                    kwargs["system"] = system
                response = client.messages.create(**kwargs)
                text = response.content[0].text

            elif self.provider in ("openai", "zhipu"):
                # 智谱和 OpenAI 的 Chat Completions API 接口一致
                messages = []
                if system:
                    messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": prompt})
                response = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                )
                text = response.choices[0].message.content

            logger.debug(f"LLM response: {text[:200]}...")
            return text

        except Exception as e:
            logger.error(f"LLM call failed: {e}")
            raise

    def extract_keywords(self, text: str) -> list[str]:
        """从文本中提取关键词用于搜索"""
        prompt = (
            f"从以下文本中提取 3-5 个最关键的搜索关键词，每行一个，不要编号：\n\n{text}"
        )
        try:
            result = self.ask(prompt, max_tokens=200)
            return [kw.strip("- ") for kw in result.strip().split("\n") if kw.strip()]
        except Exception:
            # 退化为简单分词
            return text.split()[:5]
