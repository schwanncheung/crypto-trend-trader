#!/usr/bin/env python3
"""
OpenClaw Skill: 生成今日交易报告
用法: python skills/generate_report.py
"""

import sys
from pathlib import Path

# 添加 scripts 目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))

from daily_report import main

if __name__ == "__main__":
    try:
        main()
        print("\n✅ 报告生成完成")
        sys.exit(0)
    except Exception as e:
        print(f"\n❌ 报告生成失败: {e}")
        sys.exit(1)
