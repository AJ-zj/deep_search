# 换设备 / 重新部署指南

> 仓库：https://github.com/AJ-zj/deep_search
> 密钥不入库（安全设计），所以换机器后需要自己创建 `.env`。

---

## 一、在另一台设备上跑起来

### 1. 克隆

```bash
git clone git@github.com:AJ-zj/deep_search.git
cd deep_search
```

> 用 HTTPS 也行：`git clone https://github.com/AJ-zj/deep_search.git`
> SSH 需要新机器配好 key；HTTPS 会要求输入账号 + Personal Access Token。

### 2. 建虚拟环境 + 装依赖

```bash
python -m venv .venv

# Windows
.venv\Scripts\activate
# macOS / Linux
source .venv/bin/activate

pip install -r requirements.txt
```

要求 Python 3.10 或更高。

### 3. 创建 `.env`（关键，必须手动做）

```bash
# Windows
copy .env.example .env
# macOS / Linux
cp .env.example .env
```

然后编辑 `.env`，填入你自己的 key：

```ini
# 必填：LLM（任意 OpenAI 兼容接口）
OPENAI_BASE_URL=https://api.siliconflow.cn/v1
OPENAI_API_KEY=sk-你的密钥
LLM_MODEL=Qwen/Qwen2.5-32B-Instruct

# 强烈建议填：搜索（不填会降级为内置示例资料）
TAVILY_API_KEY=tvly-你的密钥

# 仅旅行领域需要
AMAP_API_KEY=
UNSPLASH_ACCESS_KEY=

HOST=127.0.0.1
PORT=8003
```

### 4. 启动

```bash
python api/main.py
```

打开 http://127.0.0.1:8003

### 5. 验证是否正常

```bash
# 健康检查
curl http://127.0.0.1:8003/health

# 离线单测（不需要 key）
python tests/test_offline.py

# 评测集格式检查
python tests/check_eval_set.py
```

---

## 二、key 在哪申请

| Key | 用途 | 地址 | 是否必填 |
|---|---|---|---|
| `OPENAI_API_KEY` | LLM 调用 | https://cloud.siliconflow.cn/ （或任意兼容服务） | **必填** |
| `TAVILY_API_KEY` | 联网搜索 | https://tavily.com （免费约 1000 次/月） | 强烈建议 |
| `AMAP_API_KEY` | 地图/天气 | https://console.amap.com （选「Web服务」类型） | 仅旅行 |
| `UNSPLASH_ACCESS_KEY` | 景点图片 | https://unsplash.com/developers （免费 50 次/小时） | 可选 |

---

## 三、电报式速查（Windows PowerShell）

```powershell
git clone https://github.com/AJ-zj/deep_search.git
cd deep_search
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
notepad .env          # 填入 key
python api/main.py
```

---

## 四、常见问题

**Q：启动报 `No module named fastapi`**
A：没装依赖，或虚拟环境没激活。确认命令行前面有 `(.venv)`，再 `pip install -r requirements.txt`。

**Q：调研结果是"当前网络不可用"**
A：没配 `TAVILY_API_KEY`，搜索降级到了内置示例资料。填上 key 重试。

**Q：Windows 控制台中文乱码**
A：终端编码问题，不是数据问题。PowerShell 先执行 `chcp 65001`，或设 `$env:PYTHONIOENCODING="utf-8"`。

**Q：端口 8003 被占用**
A：改 `.env` 里的 `PORT`，或结束占用进程。

**Q：Unsplash 图片显示不出来**
A：免费额度 50 次/小时，用完会自动降级为示意图。等一小时或忽略。
