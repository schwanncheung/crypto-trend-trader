#!/bin/bash
# crypto-trend-trader 依赖安装脚本

set -e

echo "📦 安装 Python 依赖..."

# 创建虚拟环境（可选）
# python3 -m venv venv
# source venv/bin/activate

# 核心交易库
pip install ccxt

# 数据处理
pip install pandas numpy

# K线图绘制
pip install mplfinance

# AI视觉分析
pip install dashscope

# 配置管理
pip install pyyaml python-dotenv

# HTTP请求
pip install requests

# 异步支持（可选，用于高性能场景）
pip install aiohttp asyncio-atexit

echo "✅ 依赖安装完成"
echo ""
echo "📝 下一步："
echo "1. 复制 .env.example 为 .env 并填写 API Key"
echo "2. 配置 config/settings.yaml"
echo "3. 运行 python scripts/fetch_kline.py 测试数据获取"
pip install dashscope
