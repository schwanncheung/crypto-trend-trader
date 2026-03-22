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

# K线图绘制（用于日志存档）
pip install mplfinance

# 阿里云百炼 AI 接口
# dashscope: 视觉模型（qwen-vl 系列）原生 SDK
# openai:    文本模型（qwen3.5-plus / kimi-k2.5）OpenAI 兼容接口
pip install dashscope openai

# 配置管理
pip install pyyaml python-dotenv

# HTTP请求
pip install requests

echo "✅ 依赖安装完成"
echo ""
echo "📝 下一步："
echo "1. 创建 .env 并填写 API Key"
echo "2. 检查 config/settings.yaml（analysis.mode / exchange.testnet）"
echo "3. 运行 python scripts/fetch_kline.py 测试数据获取"
echo "4. 运行 python scripts/market_scanner.py 启动主扫描"
