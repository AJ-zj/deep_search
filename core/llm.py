"""
共享 LLM 客户端：调研引擎与决策引擎都用这一个。

这是整个体系的"地基"——两个能力共用它，就是"同一套内核"的技术证据。
沿用第13/14章踩坑验证过的方案：
  1. trust_env=False 禁系统代理
  2. 流式读取，长请求不被掐断
  3. 中文手动 UTF-8 解码（不用 decode_unicode=True）
  4. 指数退避重试
"""
import time
import json
import logging
import requests
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)


@dataclass
class Usage(dict):
    """用法统计：既是 dict（可 json 序列化）又支持 .prompt_tokens 属性访问。"""
    def __init__(self, prompt_tokens=0, completion_tokens=0, total_tokens=0):
        super().__init__(prompt_tokens=prompt_tokens,
                         completion_tokens=completion_tokens,
                         total_tokens=total_tokens)

    def __getattr__(self, key):
        try:
            return self[key]
        except KeyError:
            raise AttributeError(key)


@dataclass
class LLMResponse:
    content: str
    usage: Usage = field(default_factory=Usage)
    latency_ms: int = 0


class LLM:
    def __init__(self, model: str, api_key: str, base_url: str,
                 temperature: float = 0.7, timeout: int = 180, max_retries: int = 3):
        self.model, self.api_key = model, api_key
        self.base_url = base_url.rstrip("/")
        self.temperature, self.timeout = temperature, timeout
        self.max_retries = max_retries
        self._session = requests.Session()
        self._session.trust_env = False   # 禁系统代理（关键）

    @staticmethod
    def _delay(attempt: int) -> float:
        return 0.5 * (2 ** (attempt - 1))

    def invoke(self, messages: List[dict], **kwargs) -> LLMResponse:
        """普通对话（流式读取，避免长请求断连）。"""
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": kwargs.get("temperature", self.temperature),
            "max_tokens": kwargs.get("max_tokens", 3000),
            "stream": True,
        }
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        t0 = time.time()
        last_err = None
        for attempt in range(1, self.max_retries + 1):
            parts = []
            try:
                with self._session.post(
                    f"{self.base_url}/chat/completions",
                    headers=headers, json=payload,
                    timeout=(10, 300), stream=True,
                ) as resp:
                    if resp.status_code != 200:
                        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
                    for raw in resp.iter_lines():          # 读原始字节
                        if not raw:
                            continue
                        line = raw.decode("utf-8", "replace")  # 手动 UTF-8 解码
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            delta = json.loads(data)["choices"][0].get("delta", {})
                            if delta.get("content"):
                                parts.append(delta["content"])
                        except (json.JSONDecodeError, KeyError, IndexError):
                            continue
                return LLMResponse(content="".join(parts),
                                   latency_ms=int((time.time() - t0) * 1000))
            except Exception as e:
                last_err = e
                logger.warning(f"LLM 失败（{attempt}/{self.max_retries}）: {type(e).__name__}: {str(e)[:80]}")
                if attempt < self.max_retries:
                    time.sleep(self._delay(attempt))
        raise last_err
