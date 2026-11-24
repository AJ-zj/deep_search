"""
搜索工具：调研引擎的核心工具（支持全文抓取）。

成本意识：用 Tavily（走 API，稳定）。
  基础：每次调研 3 次搜索
  全文：include_raw_content=True，拿到整页正文而不只是摘要（质量关键）

全文的坑（实测）：
  - 长度差异大（1 千 ~ 5 万字），必须截断
  - 开头常是导航/菜单/多语言切换噪音，要跳过
  - 结尾常是页脚/版权，也要砍掉
所以用一个"取中段"的清洗函数，而不是简单 head 截断。
"""
import re
import logging
from typing import List, Dict

import requests

logger = logging.getLogger(__name__)

# 低成本降级资料（旅行主题，仅网络不可用时用）
_OFFLINE = {
    "杭州": [
        {"title": "杭州西湖游玩指南", "url": "https://example.com/hangzhou-westlake",
         "snippet": "西湖建议清晨游览避开人流，断桥、雷峰塔、苏堤是经典路线；秋季桂花季游客较多。"},
        {"title": "杭州美食推荐", "url": "https://example.com/hangzhou-food",
         "snippet": "杭州特色有西湖醋鱼、龙井虾仁、片儿川；河坊街和胜利河美食街是热门去处。"},
    ],
}


def clean_fulltext(raw: str, max_chars: int = 6000) -> str:
    """清洗全文：去噪音、取中段、限长度。

    为什么取中段而不是开头？
        实测网页开头大段是导航菜单/多语言切换（如 "English (Global) English
        (North America) ..."），正文往往在中间。单纯截取开头会拿到一堆噪音。
    """
    if not raw:
        return ""
    text = raw.strip()

    # 1) 去掉 Markdown 加粗符号、多余空行
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)

    # 2) 砍掉开头的导航噪音：找到第一个"像正文"的位置
    #    启发式：跳过连续短行（菜单项通常很短）和英文导航行
    lines = text.split("\n")
    start = 0
    short_streak = 0
    for i, line in enumerate(lines[:60]):
        s = line.strip()
        is_short = len(s) < 12
        is_nav = bool(re.fullmatch(r"[A-Za-z\s\(\)\-]{3,40}", s)) and not re.search(r"[a-z]{4,}\s+[a-z]{4,}", s)
        if is_short or is_nav:
            short_streak += 1
            if short_streak >= 6:
                start = i + 1
                short_streak = 0
        else:
            short_streak = 0
    body = "\n".join(lines[start:]).strip()
    if len(body) < 200:      # 启发式误伤，回退原文
        body = text

    # 3) 限长度（保留中段信息密度最高的部分）
    if len(body) > max_chars:
        head = body[: int(max_chars * 0.6)]
        tail = body[-int(max_chars * 0.4):]
        body = head + "\n...（中间省略）...\n" + tail
    return body


class SearchTool:
    def __init__(self, api_key: str = "", fulltext: bool = True):
        self.api_key = api_key
        self.fulltext = fulltext
        self._session = requests.Session()
        self._session.trust_env = True   # Tavily 走系统代理可通

    def search(self, query: str, max_results: int = 3) -> List[Dict[str, str]]:
        """返回 [{title, url, snippet, fulltext}]。

        snippet: Tavily 摘要（短，稳定）
        fulltext: 整页正文（长，质量高；fulltext=False 时为空）
        """
        try:
            if self.api_key:
                results = self._tavily(query, max_results)
                if results:
                    return results
        except Exception as e:
            logger.warning(f"在线搜索失败，降级离线: {type(e).__name__}: {e}")
        return self._offline(query, max_results)

    def _tavily(self, query: str, max_results: int) -> List[Dict[str, str]]:
        payload = {
            "api_key": self.api_key, "query": query,
            "max_results": max_results, "search_depth": "basic",
        }
        if self.fulltext:
            payload["include_raw_content"] = True
        resp = self._session.post("https://api.tavily.com/search", json=payload, timeout=60)
        resp.raise_for_status()
        out = []
        for item in (resp.json() or {}).get("results", []):
            if not isinstance(item, dict):
                continue
            raw = item.get("raw_content") or ""
            out.append({
                "title": str(item.get("title") or ""),
                "url": str(item.get("url") or ""),
                "snippet": str(item.get("content") or item.get("snippet") or ""),
                "fulltext": clean_fulltext(raw) if raw else "",
            })
        return out

    @staticmethod
    def _offline(query: str, max_results: int) -> List[Dict[str, str]]:
        results = []
        for key, docs in _OFFLINE.items():
            if key in query:
                results.extend({**d, "fulltext": d.get("snippet", "")} for d in docs)
        if not results:
            results = [{"title": f"关于{query}的资料", "url": "https://example.com/offline",
                        "snippet": f"（离线资料）当前网络搜索不可用，{query} 建议联网时重新调研。",
                        "fulltext": ""}]
        return results[:max_results]
