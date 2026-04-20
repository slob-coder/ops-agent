"""
LLM 客户端抽象层
支持 Anthropic / OpenAI / 本地模型，通过环境变量切换
"""

import os
import json
import logging

logger = logging.getLogger("ops-agent.llm")


class LLMInterrupted(Exception):
    """LLM 流式生成被人类中断"""
    pass


class LLMDegraded(Exception):
    """Sprint 5: LLM 连续失败,Agent 应进入降级模式"""
    pass


class RetryingLLM:
    """Sprint 5: 包装任何 .ask(prompt, system, ...) 兼容对象,加入重试与降级语义。

    - LLMInterrupted 立即透传(不算失败)
    - 其他异常 → 指数退避重试 max_attempts 次
    - 用尽后抛 LLMDegraded,并把 degraded 标志置为 True
    - 下次成功调用会自动复位 degraded
    - 失败/恢复都通过 on_state_change 回调通知主进程

    所有外部依赖(sleep / on_state_change)都可注入,方便测试。
    """

    def __init__(self, inner, max_attempts: int = 3,
                 base_backoff: float = 1.0,
                 sleep_fn=None, on_state_change=None):
        self._inner = inner
        self.max_attempts = max_attempts
        self.base_backoff = base_backoff
        self._sleep = sleep_fn or __import__("time").sleep
        self._on_state_change = on_state_change or (lambda old, new, info: None)
        self.degraded: bool = False
        self.consecutive_failures: int = 0
        self.last_failure: str = ""

    def ask(self, prompt: str, system: str = "", max_tokens: int = 4096,
            interrupt_check=None) -> str:
        last_err: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                result = self._inner.ask(
                    prompt, system=system, max_tokens=max_tokens,
                    interrupt_check=interrupt_check,
                )
                # 成功 → 复位
                if self.degraded:
                    self.degraded = False
                    self._on_state_change(True, False, "recovered")
                self.consecutive_failures = 0
                self.last_failure = ""
                return result
            except LLMInterrupted:
                raise  # 不算失败
            except Exception as e:
                last_err = e
                self.consecutive_failures += 1
                self.last_failure = f"{type(e).__name__}: {e}"
                logger.warning(
                    f"LLM call failed (attempt {attempt}/{self.max_attempts}): "
                    f"{self.last_failure}"
                )
                if attempt < self.max_attempts:
                    backoff = self.base_backoff * (2 ** (attempt - 1))
                    self._sleep(min(backoff, 60))

        # 用尽
        was_degraded = self.degraded
        self.degraded = True
        if not was_degraded:
            self._on_state_change(False, True, self.last_failure)
        raise LLMDegraded(
            f"LLM 连续 {self.max_attempts} 次失败: {self.last_failure}"
        ) from last_err


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

    def ask(self, prompt: str, system: str = "", max_tokens: int = 4096,
            interrupt_check=None) -> str:
        """向 LLM 提问，返回纯文本回答

        参数:
            prompt: 用户消息
            system: system prompt
            max_tokens: 最大输出 tokens
            interrupt_check: 可选回调函数。返回 True 时立即中止生成并抛出 LLMInterrupted

        实现说明:
            为了能在 LLM 长时间生成时响应人类指令，使用流式 API。
            每收到一个 chunk 就检查一次 interrupt_check。
        """
        client = self._get_client()

        logger.debug(f"LLM request ({self.provider}/{self.model}): {prompt}...")

        try:
            if self.provider == "anthropic":
                kwargs = {
                    "model": self.model,
                    "max_tokens": max_tokens,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if system:
                    kwargs["system"] = system

                # 流式生成，期间可被中断
                text_parts = []
                with client.messages.stream(**kwargs) as stream:
                    for chunk in stream.text_stream:
                        text_parts.append(chunk)
                        if interrupt_check and interrupt_check():
                            logger.info("LLM stream interrupted by user")
                            raise LLMInterrupted("被人类中断")
                text = "".join(text_parts)

            elif self.provider in ("openai", "zhipu"):
                messages = []
                if system:
                    messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": prompt})

                # 流式生成
                text_parts = []
                stream = client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_tokens=max_tokens,
                    stream=True,
                )
                for chunk in stream:
                    if chunk.choices and chunk.choices[0].delta.content:
                        text_parts.append(chunk.choices[0].delta.content)
                    if interrupt_check and interrupt_check():
                        logger.info("LLM stream interrupted by user")
                        try:
                            stream.close()
                        except Exception:
                            pass
                        raise LLMInterrupted("被人类中断")
                text = "".join(text_parts)

            logger.debug(f"LLM response: {text}...")
            return text

        except LLMInterrupted:
            raise
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
