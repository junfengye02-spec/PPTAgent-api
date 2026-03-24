#!/usr/bin/env python3
"""
PPTAgent API 客户端轮询脚本
----------------------------
用户运行此脚本 → 输入 PDF URL → 自动提交任务 → 轮询等待 → 打印下载链接。
实现"一次操作，一键拿 URL"体验。

用法:
    python client_poll_script.py
    # 或带参数直接运行:
    python client_poll_script.py --url "https://example.com/paper.pdf"
"""

import argparse
import sys
import time
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# 默认配置（可通过命令行参数覆盖）
# ---------------------------------------------------------------------------
DEFAULT_API_BASE = "http://127.0.0.1:8000"
POLL_INTERVAL_SEC = 10          # 轮询间隔
MAX_WAIT_SEC = 30 * 60          # 最大等待 30 分钟


def submit_task(
    api_base: str,
    document_url: str,
    theme_style: str = "academic",
    language: str = "zh",
) -> Optional[str]:
    """向 API 提交生成任务，返回 task_id；失败返回 None。"""
    payload = {
        "document_url": document_url,
        "theme_style": theme_style,
        "language": language,
    }
    try:
        resp = requests.post(
            f"{api_base}/api/v1/generate",
            json=payload,
            timeout=120,
        )
        resp.raise_for_status()
        body = resp.json()
    except requests.RequestException as exc:
        print(f"\n[ERROR] 提交任务失败: {exc}")
        return None

    if body.get("code") != 200:
        print(f"\n[ERROR] 服务端返回错误: {body.get('message')}")
        return None

    return body["data"]["task_id"]


def poll_status(api_base: str, task_id: str) -> dict:
    """轮询任务状态直到完成或失败，返回最终状态数据。"""
    start = time.time()

    while True:
        elapsed = time.time() - start
        if elapsed > MAX_WAIT_SEC:
            return {"status": "timeout"}

        try:
            resp = requests.get(
                f"{api_base}/api/v1/status/{task_id}",
                timeout=30,
            )
            resp.raise_for_status()
            body = resp.json()
        except requests.RequestException as exc:
            m, s = divmod(int(elapsed), 60)
            print(f"  [{m:02d}:{s:02d}] 查询出错 ({exc})，继续等待...")
            time.sleep(POLL_INTERVAL_SEC)
            continue

        data = body.get("data", {})
        status = data.get("status", "unknown")
        m, s = divmod(int(elapsed), 60)

        if status in ("pending", "downloading", "processing"):
            print(f"  [{m:02d}:{s:02d}] 状态: {status}，继续等待...")
            time.sleep(POLL_INTERVAL_SEC)
        else:
            data["_elapsed"] = elapsed
            return data


def main() -> None:
    parser = argparse.ArgumentParser(description="PPTAgent 一键生成客户端")
    parser.add_argument("--url", type=str, default=None, help="文档公网下载链接")
    parser.add_argument("--style", type=str, default="academic",
                        help="PPT 风格 (academic/business/creative/minimal/modern)")
    parser.add_argument("--lang", type=str, default="zh", help="语言 (zh/en)")
    parser.add_argument("--api", type=str, default=DEFAULT_API_BASE,
                        help="API 服务地址")
    args = parser.parse_args()

    print("=" * 60)
    print("  PPTAgent API 客户端 — 一键生成 PPT")
    print("=" * 60)

    # ---- 获取文档 URL ----
    doc_url = args.url
    if not doc_url:
        doc_url = input("\n请输入文档的公网下载链接 (PDF/DOCX URL): ").strip()
    if not doc_url:
        print("[ERROR] URL 不能为空")
        sys.exit(1)

    theme = args.style
    if not args.url:
        user_theme = input(
            "请输入 PPT 风格 (academic/business/creative/minimal, 默认 academic): "
        ).strip()
        if user_theme:
            theme = user_theme

    # ---- 提交任务 ----
    print(f"\n>>> 正在提交任务...")
    print(f"    文档: {doc_url}")
    print(f"    风格: {theme}")
    print(f"    语言: {args.lang}")
    print(f"    API:  {args.api}")

    task_id = submit_task(args.api, doc_url, theme, args.lang)
    if not task_id:
        sys.exit(1)
    print(f"\n[OK] 任务已提交!  Task ID: {task_id}")

    # ---- 轮询等待 ----
    print(f"\n>>> 开始轮询 (每 {POLL_INTERVAL_SEC}s 查询一次，PPT 生成通常需要 5-15 分钟)...\n")
    result = poll_status(args.api, task_id)
    status = result.get("status", "unknown")
    elapsed = result.get("_elapsed", 0)
    m, s = divmod(int(elapsed), 60)

    if status == "completed":
        ppt_url = result.get("ppt_url", "")
        file_size = result.get("file_size", 0)
        print(f"\n{'=' * 60}")
        print(f"  PPT 生成成功!")
        print(f"  耗时: {m} 分 {s} 秒")
        if file_size:
            print(f"  文件大小: {file_size / 1024:.1f} KB")
        print(f"  下载链接:")
        print(f"    {ppt_url}")
        print(f"{'=' * 60}")

    elif status == "failed":
        error = result.get("error_detail", "未知错误")
        print(f"\n[FAILED] PPT 生成失败!")
        print(f"  错误详情: {error[:800]}")
        sys.exit(1)

    elif status == "timeout":
        print(f"\n[TIMEOUT] 已等待 {MAX_WAIT_SEC // 60} 分钟，任务仍未完成。")
        print(f"  你可以稍后手动查询: GET {args.api}/api/v1/status/{task_id}")
        sys.exit(1)

    else:
        print(f"\n[UNKNOWN] 未知状态: {status}")
        sys.exit(1)


if __name__ == "__main__":
    main()
