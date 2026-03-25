"""
PPTAgent API Server
-------------------
基于 FastAPI 的 HTTP 服务，封装 PPTAgent CLI 实现异步 PPT 生成。
通过 asyncio.create_subprocess_exec 调用 pptagent generate 子进程，
使用内存字典维护任务状态，StaticFiles 挂载 outputs/ 提供文件下载。
"""

import asyncio
import json
import logging
import os
import shutil
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

import httpx
import yaml
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# 加载 .env 文件中的环境变量
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# 路径定义 — 以当前文件所在目录作为工作根目录
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.resolve()
PPTAGENT_DIR = BASE_DIR / "PPTAgent"
DOWNLOADS_DIR = BASE_DIR / "downloads"
OUTPUTS_DIR = BASE_DIR / "outputs"

DOWNLOADS_DIR.mkdir(exist_ok=True)
OUTPUTS_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# 服务器 & PPTAgent CLI 配置
# ---------------------------------------------------------------------------
API_HOST: str = os.getenv("API_HOST", "0.0.0.0")
API_PORT: int = int(os.getenv("API_PORT", "8000"))
PUBLIC_URL: str = os.getenv("PUBLIC_URL", "")  # 空值 → 启动时自动探测公网 IP
PPTAGENT_CMD: str = os.getenv("PPTAGENT_CMD", "pptagent")

# ---------------------------------------------------------------------------
# LLM / 第三方服务 Key（从 .env 读取，用于自动生成 PPTAgent 配置文件）
# ---------------------------------------------------------------------------
LLM_API_KEY: str = os.getenv("LLM_API_KEY", "")
LLM_BASE_URL: str = os.getenv("LLM_BASE_URL", "")
LLM_MODEL: str = os.getenv("LLM_MODEL", "qwen-plus")
LLM_LONG_CONTEXT_MODEL: str = os.getenv("LLM_LONG_CONTEXT_MODEL", "qwen-long")
TAVILY_API_KEY: str = os.getenv("TAVILY_API_KEY", "")
MINERU_API_KEY: str = os.getenv("MINERU_API_KEY", "")
METASO_API_KEY: str = os.getenv("METASO_API_KEY", "")

# ---------------------------------------------------------------------------
# 日志
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger("ppt_api")

# ---------------------------------------------------------------------------
# 内存任务状态字典  task_id -> { status, created_at, ... }
# ---------------------------------------------------------------------------
tasks: dict[str, dict] = {}


# ===========================================================================
# PPTAgent 配置自动生成
# ===========================================================================
def ensure_pptagent_config() -> None:
    """
    若 ~/.config/deeppresenter/ 下的 config.yaml / mcp.json 不存在，
    则根据 .env 中的 LLM_API_KEY 等参数自动生成，免去手动运行 pptagent onboard。
    """
    config_dir = Path.home() / ".config" / "deeppresenter"
    config_file = config_dir / "config.yaml"
    mcp_file = config_dir / "mcp.json"

    if config_file.exists() and mcp_file.exists():
        logger.info("PPTAgent 配置已存在: %s", config_dir)
        return

    if not LLM_API_KEY:
        logger.warning(
            "LLM_API_KEY 未设置，无法自动生成 PPTAgent 配置。"
            "请在 .env 中填写后重启，或手动运行 pptagent onboard。"
        )
        return

    config_dir.mkdir(parents=True, exist_ok=True)

    # ---- config.yaml ----
    if not config_file.exists():
        config_data = {
            "offline_mode": False,
            "context_folding": True,
            "research_agent": {
                "base_url": LLM_BASE_URL,
                "model": LLM_MODEL,
                "api_key": LLM_API_KEY,
            },
            "design_agent": {
                "base_url": LLM_BASE_URL,
                "model": LLM_MODEL,
                "api_key": LLM_API_KEY,
            },
            "long_context_model": {
                "base_url": LLM_BASE_URL,
                "model": LLM_LONG_CONTEXT_MODEL,
                "api_key": LLM_API_KEY,
            },
        }
        with open(config_file, "w", encoding="utf-8") as f:
            yaml.dump(config_data, f, default_flow_style=False, allow_unicode=True)
        logger.info("已自动生成 config.yaml → %s", config_file)

    # ---- mcp.json（从 PPTAgent 模板复制并填充 key） ----
    if not mcp_file.exists():
        template_mcp = PPTAGENT_DIR / "deeppresenter" / "mcp.json.example"
        if template_mcp.exists():
            with open(template_mcp, encoding="utf-8") as f:
                mcp_data = json.load(f)
            for server in mcp_data:
                if server.get("name") == "search":
                    if METASO_API_KEY:
                        server["description"] = "Web search tools (Metaso)"
                        server["args"] = [
                            str(BASE_DIR / "metaso_search.py"),
                            "$WORKSPACE",
                        ]
                        server["env"] = {"METASO_API_KEY": METASO_API_KEY}
                    elif TAVILY_API_KEY:
                        server["env"]["TAVILY_API_KEY"] = TAVILY_API_KEY
                if server.get("name") == "any2markdown" and MINERU_API_KEY:
                    server["env"]["MINERU_API_KEY"] = MINERU_API_KEY
            with open(mcp_file, "w", encoding="utf-8") as f:
                json.dump(mcp_data, f, indent=2, ensure_ascii=False)
            logger.info("已自动生成 mcp.json → %s", mcp_file)
        else:
            logger.warning("MCP 模板不存在: %s，请手动创建", template_mcp)

    # 同步写一份到 PPTAgent 包目录，供非 CLI 路径使用
    pkg_config = PPTAGENT_DIR / "deeppresenter" / "config.yaml"
    pkg_mcp = PPTAGENT_DIR / "deeppresenter" / "mcp.json"
    if not pkg_config.exists() and config_file.exists():
        shutil.copy(config_file, pkg_config)
    if not pkg_mcp.exists() and mcp_file.exists():
        shutil.copy(mcp_file, pkg_mcp)


# ===========================================================================
# Lifespan（替代已废弃的 on_event）
# ===========================================================================
async def _detect_public_url() -> str:
    """尝试自动探测服务器公网 IP，拼成 http://<ip>:<port>。"""
    for endpoint in ("https://api.ipify.org", "https://ifconfig.me/ip", "https://icanhazip.com"):
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(endpoint)
                ip = resp.text.strip()
                if ip:
                    return f"http://{ip}:{API_PORT}"
        except Exception:
            continue
    return f"http://127.0.0.1:{API_PORT}"


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    global PUBLIC_URL  # noqa: PLW0603
    if not PUBLIC_URL:
        PUBLIC_URL = await _detect_public_url()
        logger.info("自动探测公网地址: %s", PUBLIC_URL)
    ensure_pptagent_config()
    logger.info("API Server 启动完毕")
    logger.info("  PUBLIC_URL   = %s", PUBLIC_URL)
    logger.info("  PPTAGENT_CMD = %s", PPTAGENT_CMD)
    logger.info("  DOWNLOADS    = %s", DOWNLOADS_DIR)
    logger.info("  OUTPUTS      = %s", OUTPUTS_DIR)
    yield


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="PPTAgent API Server",
    description="通过 HTTP 调用 PPTAgent CLI 生成 PPT 演示文稿",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===========================================================================
# Pydantic 请求 / 响应模型
# ===========================================================================
class GenerateRequest(BaseModel):
    prompt: str                                # 自然语言指令（必填）
    files: list[str] = []                      # 附件公网下载链接列表（可选）
    language: str = "zh"                       # 语言 en / zh
    pages: Optional[str] = None                # 页数范围 e.g. "8-12"
    aspect_ratio: str = "16:9"                 # 幻灯片比例


class APIResponse(BaseModel):
    code: int
    message: str
    data: dict


# ===========================================================================
# 工具函数：文件下载
# ===========================================================================
async def download_file(url: str, dest_dir: Path) -> Path:
    """从公网 URL 下载文件到本地目录，返回本地路径。"""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30, read=180, write=30, pool=30),
        follow_redirects=True,
    ) as client:
        resp = await client.get(url)
        resp.raise_for_status()

        # 尝试从 Content-Disposition 解析文件名
        cd = resp.headers.get("content-disposition", "")
        if "filename=" in cd:
            filename = cd.split("filename=")[-1].strip('"').strip("'")
        else:
            filename = url.split("/")[-1].split("?")[0]
        filename = Path(unquote(filename)).name
        if not filename:
            filename = "document.pdf"

        dest = dest_dir / filename
        dest.write_bytes(resp.content)
        logger.info("文件已下载: %s → %s (%d bytes)", url, dest, len(resp.content))
        return dest


# ===========================================================================
# 核心：后台子进程任务
# ===========================================================================
async def run_pptagent_task(
    task_id: str,
    request: GenerateRequest,
) -> None:
    """
    在后台完成文件下载 + 调用 pptagent generate 子进程，
    全程不阻塞 FastAPI 主事件循环。
    """
    try:
        # ---- 第一步：下载附件（如果有） ----
        downloaded_files: list[Path] = []
        if request.files:
            tasks[task_id]["status"] = "downloading"
            dl_dir = DOWNLOADS_DIR / task_id
            dl_dir.mkdir(parents=True, exist_ok=True)
            for url in request.files:
                logger.info("[%s] 后台下载: %s", task_id, url)
                try:
                    file_path = await download_file(url, dl_dir)
                    downloaded_files.append(file_path)
                except Exception as exc:
                    tasks[task_id].update(
                        status="failed",
                        error_detail=f"文件下载失败: {url} — {exc}",
                    )
                    logger.error("[%s] 下载失败: %s — %s", task_id, url, exc)
                    return
            tasks[task_id]["local_files"] = [str(p) for p in downloaded_files]

        # ---- 第二步：调用 pptagent generate ----
        tasks[task_id]["status"] = "processing"
        tasks[task_id]["started_at"] = datetime.now().isoformat()

        output_filename = f"{task_id}.pptx"
        output_path = OUTPUTS_DIR / output_filename

        cmd_parts = PPTAGENT_CMD.split()
        cmd: list[str] = [
            *cmd_parts,
            "generate",
            request.prompt,
            "-o", str(output_path.resolve()),
            "-l", request.language,
            "-a", request.aspect_ratio,
        ]
        for fp in downloaded_files:
            cmd.extend(["-f", str(fp.resolve())])
        if request.pages:
            cmd.extend(["-p", request.pages])

        cmd_display = " ".join(cmd)
        logger.info("[%s] 执行: %s", task_id, cmd_display)
        tasks[task_id]["command"] = cmd_display

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(BASE_DIR),
        )

        stdout_raw, stderr_raw = await process.communicate()
        stdout_str = stdout_raw.decode("utf-8", errors="replace") if stdout_raw else ""
        stderr_str = stderr_raw.decode("utf-8", errors="replace") if stderr_raw else ""

        logger.info("[%s] 退出码=%s", task_id, process.returncode)
        if stdout_str:
            logger.debug("[%s] STDOUT(尾部): %s", task_id, stdout_str[-2000:])
        if stderr_str:
            logger.warning("[%s] STDERR(尾部): %s", task_id, stderr_str[-2000:])

        # ---- 判断生成结果 ----
        if process.returncode == 0 and output_path.exists():
            tasks[task_id].update(
                status="completed",
                output_filename=output_filename,
                output_size=output_path.stat().st_size,
                completed_at=datetime.now().isoformat(),
            )
            logger.info("[%s] 成功: %s", task_id, output_path)
            return

        # html2pptx 可能 fallback 为 PDF
        pdf_fallback = output_path.with_suffix(".pdf")
        if pdf_fallback.exists():
            pdf_name = f"{task_id}.pdf"
            pdf_dest = OUTPUTS_DIR / pdf_name
            if pdf_fallback != pdf_dest:
                shutil.copy(pdf_fallback, pdf_dest)
            tasks[task_id].update(
                status="completed",
                output_filename=pdf_name,
                output_size=pdf_dest.stat().st_size,
                completed_at=datetime.now().isoformat(),
            )
            logger.info("[%s] 成功(PDF fallback): %s", task_id, pdf_dest)
            return

        # 也检查 workspace 中是否有生成的 pptx（pptagent 可能输出到 workspace）
        workspace_base = Path.home() / ".cache" / "deeppresenter"
        if workspace_base.exists():
            for pptx_file in workspace_base.rglob("*.pptx"):
                # 只取最近 10 分钟内修改过的文件
                if pptx_file.stat().st_mtime > (datetime.now().timestamp() - 600):
                    dest = OUTPUTS_DIR / output_filename
                    shutil.copy(pptx_file, dest)
                    tasks[task_id].update(
                        status="completed",
                        output_filename=output_filename,
                        output_size=dest.stat().st_size,
                        completed_at=datetime.now().isoformat(),
                    )
                    logger.info("[%s] 成功(workspace扫描): %s → %s", task_id, pptx_file, dest)
                    return

        # 真正失败
        error_detail = (
            f"退出码: {process.returncode}\n"
            f"--- STDERR ---\n{stderr_str[-3000:]}\n"
            f"--- STDOUT ---\n{stdout_str[-3000:]}"
        )
        tasks[task_id].update(status="failed", error_detail=error_detail)
        logger.error("[%s] 失败: %s", task_id, error_detail[:500])

    except Exception as exc:
        msg = f"{type(exc).__name__}: {exc}"
        tasks[task_id].update(status="failed", error_detail=msg)
        logger.exception("[%s] 异常: %s", task_id, msg)


# ===========================================================================
# API 端点
# ===========================================================================

@app.post("/api/v1/generate", response_model=APIResponse)
async def api_generate(request: GenerateRequest) -> APIResponse:
    """提交 PPT 生成任务，立即返回 task_id（文件下载也在后台进行）。"""
    task_id = str(uuid.uuid4())

    tasks[task_id] = {
        "status": "pending",
        "created_at": datetime.now().isoformat(),
        "prompt": request.prompt,
        "files": request.files,
    }

    # 文件下载 + pptagent 调用全部在后台执行，POST 秒回
    asyncio.create_task(run_pptagent_task(task_id, request))

    return APIResponse(
        code=200,
        message="Task submitted successfully",
        data={"task_id": task_id},
    )


@app.get("/api/v1/status/{task_id}", response_model=APIResponse)
async def api_status(task_id: str) -> APIResponse:
    """查询指定任务的当前状态。"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")

    task = tasks[task_id]
    status = task["status"]

    if status in ("pending", "downloading", "processing"):
        return APIResponse(code=202, message="Processing", data={"status": status})

    if status == "completed":
        filename = task.get("output_filename", "")
        return APIResponse(
            code=200,
            message="Success",
            data={
                "status": "completed",
                "ppt_url": f"{PUBLIC_URL}/outputs/{filename}",
                "file_size": task.get("output_size", 0),
            },
        )

    # failed
    return APIResponse(
        code=500,
        message="Failed",
        data={
            "status": "failed",
            "error_detail": task.get("error_detail", "未知错误"),
        },
    )


@app.get("/api/v1/tasks", response_model=APIResponse)
async def api_list_tasks() -> APIResponse:
    """列出所有已提交的任务（仅返回摘要信息）。"""
    summary = {
        tid: {
            "status": t["status"],
            "created_at": t.get("created_at"),
            "prompt": t.get("prompt"),
        }
        for tid, t in tasks.items()
    }
    return APIResponse(code=200, message="OK", data=summary)


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


# ===========================================================================
# 挂载静态文件（outputs/ 目录供下载生成的 PPTX / PDF）
# ===========================================================================
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")


# ===========================================================================
# 本地开发入口
# ===========================================================================
if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api_server:app",
        host=API_HOST,
        port=API_PORT,
        reload=False,
        log_level="info",
    )
