"""
配置：只从本项目 .env 或环境变量读取。

为什么不用 pydantic-settings？
    环境里未必装，用标准库手写极简解析器，零依赖、可控。

查找顺序（后面的优先）：
    1. 项目根目录 .env
    2. 系统环境变量
"""
import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> dict:
    d = {}
    if not path.is_file():
        return d
    with open(path, encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip().strip("'\"")
    return d


@dataclass
class Settings:
    openai_api_key: str = ""
    openai_base_url: str = "https://api.siliconflow.cn/v1"
    llm_model: str = "Qwen/Qwen2.5-32B-Instruct"
    tavily_api_key: str = ""
    amap_key: str = ""
    unsplash_key: str = ""
    host: str = "127.0.0.1"
    port: int = 8003

    def __post_init__(self):
        env = _load_env(_ROOT / ".env")

        def pick(name: str, default: str = "") -> str:
            """环境变量优先，其次 .env，最后默认值。"""
            return os.environ.get(name) or env.get(name) or default

        self.openai_api_key = pick("OPENAI_API_KEY", self.openai_api_key)
        self.openai_base_url = pick("OPENAI_BASE_URL", self.openai_base_url)
        self.llm_model = pick("LLM_MODEL", self.llm_model)
        self.tavily_api_key = pick("TAVILY_API_KEY", self.tavily_api_key)
        # 高德：AMAP_API_KEY 优先，兼容 AMAP_WEB_KEY
        self.amap_key = pick("AMAP_API_KEY", "") or pick("AMAP_WEB_KEY", "")
        self.unsplash_key = pick("UNSPLASH_ACCESS_KEY", "")
        self.host = pick("HOST", self.host)
        try:
            self.port = int(pick("PORT", str(self.port)))
        except ValueError:
            pass


@lru_cache()
def get_settings() -> Settings:
    return Settings()
