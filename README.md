# PPTAgent API Server

将 [PPTAgent](https://github.com/icip-cas/PPTAgent) 封装为 HTTP API 服务，支持通过自然语言指令或文档附件异步生成 PPT 演示文稿。

## 功能特性

- **自然语言生成**：只传一句话即可生成 PPT（如 "请介绍小米SU7的外观和价格"）
- **文档驱动生成**：传入 PDF / Word 等文档链接，基于文档内容生成 PPT
- **多附件支持**：支持同时传入多个文件
- **异步任务**：提交后立即返回 task_id，后台生成，轮询获取结果
- **Docker 一键部署**：`docker compose up -d` 即可运行

## 快速开始

### 1. 克隆项目

```bash
git clone --recurse-submodules <your-repo-url>
cd my_api_server
# 如果首次克隆时未带子模块参数，可补执行：
git submodule update --init --recursive
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入你的 LLM API Key 等配置
```

### 3. Docker 部署（推荐）

```bash
docker compose up -d
```

首次启动会自动构建镜像并拉取 sandbox。启动后访问：

```
http://服务器IP:8000/docs
```

### 4. 本地开发运行

```bash
cd PPTAgent
uv venv --python 3.11
source .venv/bin/activate
uv pip install -e .
playwright install chromium
npm install --prefix deeppresenter/html2pptx
docker pull forceless/deeppresenter-sandbox:0.1.0
docker tag forceless/deeppresenter-sandbox:0.1.0 deeppresenter-sandbox:0.1.0

cd ..
uv pip install -r requirements_api.txt
python api_server.py
```

---

## API 接口

### POST `/api/v1/generate` — 提交生成任务

**请求体：**

| 参数 | 类型 | 必填 | 默认值 | 说明 |
|------|------|------|--------|------|
| `prompt` | `string` | 是 | — | 自然语言指令 |
| `files` | `string[]` | 否 | `[]` | 附件公网下载链接列表 |
| `language` | `string` | 否 | `"zh"` | 语言：`zh` / `en` |
| `pages` | `string` | 否 | `null` | 页数范围，如 `"8-12"` |
| `aspect_ratio` | `string` | 否 | `"16:9"` | 幻灯片比例 |

**请求示例：**

纯自然语言：

```json
{ "prompt": "请介绍小米SU7的外观和价格" }
```

带文档附件：

```json
{
  "prompt": "请将给定文档制作成学术风格的PPT",
  "files": ["https://arxiv.org/pdf/2501.03936"]
}
```

**响应：**

```json
{
  "code": 200,
  "message": "Task submitted successfully",
  "data": { "task_id": "uuid-string" }
}
```

### GET `/api/v1/status/{task_id}` — 查询任务状态

**状态流转：** `pending` → `downloading` → `processing` → `completed` / `failed`

成功时返回 `ppt_url` 字段即为下载地址。

### GET `/api/v1/tasks` — 列出所有任务

### GET `/health` — 健康检查

---

## 目录结构

```
my_api_server/
├── Dockerfile                # API 服务镜像
├── docker-compose.yml        # 服务编排
├── api_server.py             # FastAPI 主程序
├── metaso_search.py          # 秘塔搜索 MCP 工具
├── client_poll_script.py     # 调试用轮询客户端
├── requirements_api.txt      # API 额外依赖
├── .env.example              # 环境变量模板
├── .dockerignore             # Docker 构建排除
├── .gitignore                # Git 排除
├── PPTAgent/                 # PPTAgent 源码（git clone）
├── downloads/                # (运行时) 下载的源文件
└── outputs/                  # (运行时) 生成的 PPTX
```

## 注意事项

1. **生成耗时**：通常 5-15 分钟，取决于文档长度和 LLM 响应速度
2. **并发限制**：建议同时不超过 2-3 个任务
3. **Docker Socket**：API 容器需挂载 `/var/run/docker.sock` 管理 sandbox
4. **PUBLIC_URL**：`.env` 中留空则自动探测公网 IP；部署时建议手动设置

## License

MIT
