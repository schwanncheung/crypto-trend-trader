#!/usr/bin/env python3
"""
OpenClaw Skill: 执行一次完整的市场扫描
用法: python skills/scan_market.py
"""

import sys
from pathlib import Path

# 添加 scripts 目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from market_scanner import main

if __name__ == "__main__":
    try:
        main()
        print("\n✅ 市场扫描完成")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 扫描失败: {e}")
        sys.exit(1)
