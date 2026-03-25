#!/bin/bash
set -e

echo "=== PPTAgent API 一键部署 ==="

# 拉取 sandbox 镜像
echo "[1/3] 拉取 sandbox 镜像..."
docker pull forceless/deeppresenter-sandbox:0.1.0
docker tag forceless/deeppresenter-sandbox:0.1.0 deeppresenter-sandbox:0.1.0

# 构建并启动
echo "[2/3] 构建 API 服务镜像并启动..."
docker compose up -d --build

# 等待服务就绪
echo "[3/3] 等待服务就绪..."
for i in $(seq 1 30); do
    if curl -s http://127.0.0.1:8000/health > /dev/null 2>&1; then
        echo ""
        PUBLIC_IP=$(curl -s --connect-timeout 5 ifconfig.me 2>/dev/null || echo "127.0.0.1")
        echo "=== 部署完成 ==="
        echo "API 地址: http://${PUBLIC_IP}:8000"
        echo "接口文档: http://${PUBLIC_IP}:8000/docs"
        echo "健康检查: http://${PUBLIC_IP}:8000/health"
        echo ""
        echo "使用示例:"
        echo "  curl -X POST http://${PUBLIC_IP}:8000/api/v1/generate \\"
        echo "    -H 'Content-Type: application/json' \\"
        echo "    -d '{\"prompt\":\"请介绍人工智能的发展历史\",\"language\":\"zh\",\"pages\":\"5-6\"}'"
        exit 0
    fi
    printf "."
    sleep 2
done

echo ""
echo "服务启动超时，请检查: docker compose logs api-server"
exit 1
