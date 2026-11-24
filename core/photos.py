"""
图片服务：给行程地点配真实照片。

三级策略（保证每个地点都有图，且不烧额度）：
  1. 本地缓存：同一地点第二次出现直接用缓存，不再调 API
  2. Unsplash：真实实景照（免费额度 50 次/小时，超了返回 403）
  3. Picsum：Unsplash 限流/无结果时兜底（免费不限量，随机风景照）

这样即使额度用完，界面也不会出现"无图"空块。
"""
import json
import logging
import hashlib
import random
from pathlib import Path
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class PhotoService:
    BASE = "https://api.unsplash.com/search/photos"

    def __init__(self, access_key: str, cache_dir: Optional[Path] = None):
        self.access_key = access_key
        self._session = requests.Session()
        self._session.trust_env = True
        self._rate_limited = False   # 一旦限流，本次流程不再重试，直接用兜底
        self._cache = {}
        self._cache_file = None
        if cache_dir:
            cache_dir = Path(cache_dir)
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._cache_file = cache_dir / "photo_cache.json"
            self._load_cache()

    def _load_cache(self):
        try:
            if self._cache_file and self._cache_file.is_file():
                self._cache = json.loads(self._cache_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._cache = {}

    def _save_cache(self):
        try:
            if self._cache_file:
                self._cache_file.write_text(
                    json.dumps(self._cache, ensure_ascii=False, indent=1), encoding="utf-8")
        except OSError:
            pass

    @property
    def enabled(self) -> bool:
        return bool(self.access_key)

    def search_one(self, query: str, orientation: str = "landscape") -> Optional[dict]:
        """搜一张图：缓存 → Unsplash → Picsum 兜底。"""
        if not query:
            return None
        key = f"v2::{query}"

        # 1) 缓存
        if key in self._cache:
            return self._cache[key]

        # 2) Unsplash（已限流则跳过）
        result = None
        if self.access_key and not self._rate_limited:
            result = self._unsplash(query, orientation)

        # 3) Picsum 兜底：保证有图
        if not result:
            result = self._picsum(query)

        if result:
            self._cache[key] = result
            self._save_cache()
        return result

    def _unsplash(self, query: str, orientation: str) -> Optional[dict]:
        try:
            r = self._session.get(self.BASE, params={
                "query": query, "per_page": 1, "orientation": orientation,
            }, headers={"Authorization": f"Client-ID {self.access_key}"}, timeout=25)
            if r.status_code == 403:
                logger.warning("Unsplash 限流（免费额度 50/小时已用完），本次改用 Picsum 兜底")
                self._rate_limited = True
                return None
            if r.status_code != 200:
                logger.warning(f"Unsplash HTTP {r.status_code}")
                return None
            results = (r.json() or {}).get("results") or []
            if not results:
                return None
            p = results[0]
            urls = p.get("urls") or {}
            user = p.get("user") or {}
            return {
                "url": urls.get("regular") or urls.get("small") or "",
                "thumb": urls.get("small") or urls.get("thumb") or "",
                "alt": p.get("alt_description") or query,
                "credit": user.get("name", ""),
                "source": "unsplash",
            }
        except Exception as e:
            logger.warning(f"Unsplash 失败: {query} | {type(e).__name__}: {e}")
            return None

    @staticmethod
    def _picsum(query: str) -> dict:
        """Picsum 兜底：按 query 生成稳定种子，同一地点每次同一张图。"""
        seed = hashlib.md5(query.encode("utf-8")).hexdigest()[:10]
        return {
            "url": f"https://picsum.photos/seed/{seed}/640/420",
            "thumb": f"https://picsum.photos/seed/{seed}/320/210",
            "alt": query,
            "credit": "Picsum",
            "source": "picsum",
        }
