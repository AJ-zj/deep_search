"""
FastAPI 入口：把"调研→决策"包装成可交互的产品。

接口：
  POST /api/plan/stream  —— SSE 流式：实时推送调研/决策进度，最后推完整报告
  GET  /                 —— 单页界面（原生 JS，零构建）

为什么用 SSE？
  这条链要跑几十秒（搜索+多次 LLM），必须让用户看到进度，否则像卡死。
  用队列把工作线程的事件传给异步生成器，避免阻塞事件循环。
"""
import json
import os
import queue
import asyncio
import logging
import threading
import sys
from pathlib import Path
from typing import AsyncGenerator, Optional

# 可选：若本机把依赖解压在某个目录（离线/受限环境），用 VENDOR_PATH 指过去。
# 正常 `pip install -r requirements.txt` 即可，无需设置此变量。
_vendor = os.environ.get("VENDOR_PATH")
if _vendor and Path(_vendor).is_dir() and _vendor not in sys.path:
    sys.path.insert(0, _vendor)

# 把项目根目录加入 sys.path，才能 import core / agents / pipeline
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from fastapi import FastAPI
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from core.config import get_settings
from core.llm import LLM
from core.tools import SearchTool
from core.amap import AmapService
from core.photos import PhotoService
from pipeline import TripResearchPipeline

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("api")

app = FastAPI(title="TripResearch · 调研→决策双引擎")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
                   allow_headers=["*"], allow_credentials=True)

STATIC = Path(__file__).resolve().parent / "static"


class PlanRequest(BaseModel):
    topic: str
    days: int = 3
    interests: list[str] = []
    domain: str = "travel"   # travel / policy / ecommerce / general
    deep: bool = False       # True=读全文+迭代追问（更慢更深）


def _sse(payload: dict) -> str:
    return "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n"


def _worker(req: PlanRequest, q: queue.Queue):
    """工作线程：跑完整流水线，把事件塞进队列。"""
    try:
        s = get_settings()
        llm = LLM(s.llm_model, s.openai_api_key, s.openai_base_url)
        # 必须从 config 读 key（它已合并 .env + 环境变量），
        # 直接读 os.environ 会漏掉写在本项目 .env 里的配置。
        search = SearchTool(api_key=s.tavily_api_key)
        amap = AmapService(api_key=s.amap_key)
        photos = PhotoService(access_key=s.unsplash_key,
                              cache_dir=_ROOT / "workspace")
        pipe = TripResearchPipeline(llm, search, event_sink=lambda e: q.put(e),
                                    amap=amap, photos=photos)
        pack, plan = pipe.run(req.topic, {
            "days": req.days, "interests": req.interests,
        }, domain=req.domain, deep=req.deep)
        q.put({"type": "report",
               "data": plan.to_markdown(pack),
               "plan": plan.to_dict(),
               "pack": pack.to_dict(),
               "domain": req.domain})
    except Exception as e:
        logger.exception("流水线失败")
        q.put({"type": "error", "message": f"处理失败：{e}"})
    finally:
        q.put({"__done__": True})


async def _stream(req: PlanRequest) -> AsyncGenerator[str, None]:
    topic = (req.topic or "").strip()
    if not topic:
        yield _sse({"type": "error", "message": "主题不能为空"})
        return

    q: queue.Queue = queue.Queue()
    threading.Thread(target=_worker, args=(req, q), daemon=True).start()

    pct = 5
    yield _sse({"type": "progress", "percentage": pct, "text": "准备中..."})
    try:
        while True:
            event = await asyncio.to_thread(q.get)
            if event.get("__done__"):
                break
            t = event.get("type")
            if t == "stage":
                pct = 15 if event.get("stage") == "research" else 80
                yield _sse({"type": "progress", "percentage": pct,
                            "text": event.get("message", "")})
            elif t == "progress":
                # 调研引擎的细粒度进度（第几个问题、深挖中）
                yield _sse({"type": "status", "text": event.get("text", "")})
            elif t == "findings":
                yield _sse({"type": "progress", "percentage": 70,
                            "text": event.get("message", "")})
            elif t == "plan":
                yield _sse({"type": "progress", "percentage": 90,
                            "text": event.get("message", "")})
            elif t == "report":
                yield _sse({"type": "report", "data": event.get("data", ""),
                            "plan": event.get("plan", {}), "pack": event.get("pack", {}),
                            "domain": event.get("domain", "")})
            elif t == "error":
                yield _sse({"type": "error", "message": event.get("message", "")})
        yield _sse({"type": "progress", "percentage": 100, "text": "完成"})
        yield _sse({"type": "done"})
    except asyncio.CancelledError:
        raise


@app.post("/api/plan/stream")
async def plan_stream(req: PlanRequest):
    return StreamingResponse(_stream(req), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.get("/api/domains")
async def domains():
    """列出可用的调研领域（前端下拉用）。"""
    from core.domain import list_domains
    return {"domains": list_domains()}


@app.get("/health")
async def health():
    s = get_settings()
    return {"status": "ok", "model": s.llm_model,
            "has_llm": bool(s.openai_api_key)}


@app.get("/", response_class=HTMLResponse)
async def index():
    html = STATIC / "index.html"
    if html.is_file():
        return HTMLResponse(html.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>TripResearch</h1><p>static/index.html 缺失</p>")


if __name__ == "__main__":
    import uvicorn
    s = get_settings()
    uvicorn.run(app, host=s.host, port=s.port)
