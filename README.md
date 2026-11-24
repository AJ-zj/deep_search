# TripResearch · 调研 → 决策双引擎 Agent

> 输入一个主题，先做**带来源的调研**，再基于证据做**决策**（如生成旅行行程）。
> 调研引擎是独立的通用组件，可切换领域：旅行 / 政策 / 电商 / 通用。

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com/)
[![CI](https://github.com/AJ-zj/deep_search/actions/workflows/ci.yml/badge.svg)](https://github.com/AJ-zj/deep_search/actions/workflows/ci.yml)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

---

## 效果预览

**旅行规划**：左侧地图（真实坐标 + 打点）、天气预报；右侧逐日行程，每个地点带实景图、地址、附近美食、导航链接。

![旅行规划](assets/screenshot-travel.png)

**政策调研**（深度档）：每条结论都带来源链接，可逐条核对。

![政策调研](assets/screenshot-policy.png)

**入口**：领域可切换、深度可开关。

![首页](assets/screenshot-home.png)

---

## 它解决什么问题

直接用大模型生成方案，最大的问题是**没有依据**——"推荐去西湖"这类话谁都会说，
但你无法核实它是否真实、是否过时。

本项目把流程拆成两步：

1. **调研**：把主题拆成若干子问题 → 联网搜索 → 提炼成**每条都带来源 URL** 的发现
2. **决策**：**只依据这些发现**生成方案，并标注每条依据的是哪条调研结果

于是产出的方案可以**逐条溯源**：页面上的每个安排都能点开原始网页核对。

```
用户输入主题
   │
   ├─ ① 调研引擎 ── 拆问题 → 搜索 → 提炼 → KnowledgePack（带来源）
   │
   └─ ② 决策引擎 ── 消费 KnowledgePack → 生成方案（标注引用编号）
```

## 两种调研深度

| 档位 | 读什么 | 是否追问 | 耗时 |
|---|---|---|---|
| 快速（默认） | 搜索摘要 | 否，固定 3 轮 | ~16s |
| 深度 | **整页正文** | **是**，发现证据缺口自动补搜 1-2 轮 | ~18-30s |

深度档会读取网页全文（而非摘要），并让 LLM 充当"审校员"判断还缺什么信息，
不足则自动追问再搜一轮。实测同一政策主题：快速档只得到笼统结论，
深度档能拿到具体数字（补贴标准、文号、适用期限）。

## 调研引擎是通用组件

引擎逻辑只有一份，"怎么拆问题、怎么过滤"由领域配置决定，
见 [`core/domain.py`](core/domain.py)。换配置即换业务，代码不用改。

| 领域 | key | 拆解维度 |
|---|---|---|
| 旅行规划 | `travel` | 景点口碑 / 特色美食 / 行程节奏与避坑 |
| 政策法规 | `policy` | 政策文件与主体 / 适用范围 / 关键数字 / 有效期 |
| 电商内容 | `ecommerce` | 人群痛点 / 竞品卖点 / 真实评价 / 平台合规 |
| 通用调研 | `general` | 概念背景 / 现状事实 / 案例数据 / 趋势风险 |

```python
from core.llm import LLM
from core.tools import SearchTool
from agents.researcher import ResearchEngine

engine = ResearchEngine(
    LLM(model="Qwen/Qwen2.5-32B-Instruct", api_key="...", base_url="..."),
    SearchTool(api_key="tvly-..."),
)

# 同一个引擎，四种用途
pack = engine.research("杭州 3 天人文美食游", domain="travel")
pack = engine.research("浙江省分布式光伏补贴政策", domain="policy", deep=True)
pack = engine.research("抖音鞋类带货文案卖点", domain="ecommerce")

for f in pack.findings:
    print(f.claim, "←", f.source)
```

## 快速开始

```bash
git clone <this-repo>
cd TripResearch

python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env      # 然后填入 API Key
python api/main.py        # 打开 http://127.0.0.1:8003
```

最少只需要配置 `OPENAI_API_KEY`（任意 OpenAI 兼容接口）。
强烈建议再配 `TAVILY_API_KEY`，否则搜索会降级到内置示例资料。

## 接口

```http
POST /api/plan/stream
Content-Type: application/json

{
  "topic": "杭州 3 天人文美食游",
  "days": 3,
  "interests": ["人文", "美食"],
  "domain": "travel",
  "deep": false
}
```

响应是 SSE 流，逐条推送进度与最终结果：

```
data: {"type":"progress","percentage":15,"text":"正在调研：..."}
data: {"type":"status","text":"正在调研 1/3：..."}
data: {"type":"report","data":"<Markdown 报告>","plan":{...},"pack":{...}}
data: {"type":"done"}
```

其他接口：

- `GET /api/domains` — 列出可用领域
- `GET /health` — 健康检查

## 项目结构

```
TripResearch/
├── core/                 共享内核（两个引擎都用）
│   ├── llm.py            LLM 客户端（流式 / 禁代理 / UTF-8 / 退避重试）
│   ├── config.py         配置（.env + 环境变量）
│   ├── tools.py          搜索工具（Tavily，支持全文抓取与清洗）
│   ├── evidence.py       证据契约（Finding / KnowledgePack / 方案数据结构）
│   ├── domain.py         领域配置（通用组件的关键）
│   ├── amap.py           高德：坐标 / 周边 / 天气
│   └── photos.py         图片（Unsplash + 缓存 + 兜底）
├── agents/
│   ├── researcher.py     调研引擎（可配置领域、可选深度）
│   └── planner.py        决策引擎（知识包 → 结构化方案）
├── pipeline.py           编排层（串起两个引擎）
├── api/
│   ├── main.py           FastAPI + SSE
│   └── static/index.html 单页界面（原生 JS，零构建）
└── tests/                分步测试
```

## 设计取舍

- **不用 Agent 框架**：整条链是确定的流程（拆解→搜索→提炼→生成），
  用一个轻量 Agent 循环即可，引入重型框架反而增加调试成本。
- **LLM 客户端自己写**：基于 `requests`，禁用系统代理、流式读取、
  手动 UTF-8 解码、指数退避。兼容 OpenAI 风格响应结构。
- **证据契约先行**：`KnowledgePack` 是调研与决策之间唯一的接口，
  保证下游只能用"已读取的证据"，不允许凭空生成。
- **防御脏数据**：LLM 输出的 JSON 会带代码块围栏、尾逗号、字段别名；
  搜索结果可能跑题（实测搜杭州美食返回日本料理）。都做了容错与过滤。

## 已知限制

- 搜索结果质量依赖搜索 API，域名权威性未做排序加权
- 决策（排行程）只实现了旅行场景；其他领域只产出调研发现
- 未做用户体系与持久化，`workspace/` 仅缓存图片 URL
- 深度档成本高于快速档（约 2 倍），并发高时建议用快速档

## License

MIT
