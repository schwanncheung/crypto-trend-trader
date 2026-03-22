"""
integration_test.py
整体联调测试脚本
按步骤验证每个模块是否正常工作
"""

import sys
import json
import traceback
from pathlib import Path

# 添加scripts到路径
sys.path.insert(0, str(Path(__file__).parent))


def print_step(step: int, title: str):
    print(f"\n{'='*50}")
    print(f"Step {step}: {title}")
    print('='*50)


def print_result(success: bool, msg: str):
    icon = "✅" if success else "❌"
    print(f"{icon} {msg}")


def test_config():
    """Step 1: 测试配置加载"""
    print_step(1, "配置文件加载测试")
    try:
        import yaml
        from dotenv import load_dotenv
        import os

        load_dotenv()

        with open("config/settings.yaml", "r") as f:
            cfg = yaml.safe_load(f)
        with open("config/symbols.yaml", "r") as f:
            sym = yaml.safe_load(f)

        print_result(True, f"settings.yaml 加载成功")
        print_result(True, f"symbols.yaml 加载成功，监控合约数：{len(sym['symbols'])}")

        # 检查环境变量
        keys = {
            "EXCHANGE_API_KEY": os.getenv("EXCHANGE_API_KEY", ""),
            "DASHSCOPE_API_KEY": os.getenv("DASHSCOPE_API_KEY", ""),
            "OPENAI_API_KEY": os.getenv("OPENAI_API_KEY", ""),
        }
        for k, v in keys.items():
            if v:
                print_result(True, f"{k} 已配置")
            else:
                print_result(False, f"{k} 未配置（可选项除外）")

        return cfg, sym
    except Exception as e:
        print_result(False, f"配置加载失败：{e}")
        traceback.print_exc()
        return None, None


def test_exchange(cfg):
    """Step 2: 测试交易所连接"""
    print_step(2, "交易所连接测试")
    try:
        from execute_trade import create_exchange
        exchange = create_exchange()

        # 测试获取余额
        balance = exchange.fetch_balance()
        usdt = float(balance["free"].get("USDT", 0))
        print_result(True, f"交易所连接成功")
        print_result(True, f"可用余额：{usdt:.2f} USDT")

        # 测试获取持仓
        from execute_trade import get_open_positions
        positions = get_open_positions(exchange)
        print_result(True, f"持仓查询成功，当前持仓：{len(positions)} 个")

        return exchange
    except Exception as e:
        print_result(False, f"交易所连接失败：{e}")
        traceback.print_exc()
        return None


def test_kline(sym):
    """Step 3: 测试K线数据获取"""
    print_step(3, "K线数据获取测试")
    try:
        from fetch_kline import (
            fetch_multi_timeframe,
            calculate_support_resistance,
            detect_trend_structure,
            calculate_volume_ma
        )

        symbol = sym["priority"][0]
        print(f"测试品种：{symbol}")

        data = fetch_multi_timeframe(symbol)
        for tf, df in data.items():
            print_result(True, f"[{tf}] 获取 {len(df)} 根K线")

        # 测试技术计算
        support, resistance = calculate_support_resistance(data["4h"])
        print_result(True, f"支撑位：{support}")
        print_result(True, f"阻力位：{resistance}")

        structure = detect_trend_structure(data["1d"])
        print_result(True, f"日线趋势：{structure['trend']}")

        vol_ma = calculate_volume_ma(data["1h"])
        print_result(True, f"成交量均线计算成功，最新值：{vol_ma.iloc[-1]:.2f}")

        return data, support, resistance, symbol
    except Exception as e:
        print_result(False, f"K线数据获取失败：{e}")
        traceback.print_exc()
        return None, None, None, None


def test_chart(data, support, resistance, symbol):
    """Step 4: 测试K线图生成"""
    print_step(4, "K线图生成测试")
    try:
        from generate_chart import generate_multi_chart

        paths = generate_multi_chart(
            multi_tf_data=data,
            symbol=symbol,
            support_levels=support,
            resistance_levels=resistance
        )

        for tf, path in paths.items():
            exists = Path(path).exists()
            print_result(exists, f"[{tf}] 图表：{path}")

        return paths
    except Exception as e:
        print_result(False, f"K线图生成失败：{e}")
        traceback.print_exc()
        return None


def test_ai(paths):
    """Step 5: 测试AI分析"""
    print_step(5, "AI视觉分析测试")
    try:
        from ai_analysis import analyze_with_fallback, passes_risk_filter

        image_list = list(paths.values())
        print(f"输入图片数量：{len(image_list)}")

        decision = analyze_with_fallback(image_list, multi_tf=True)

        print_result(True, f"AI分析成功，使用模型：{decision.get('_model_used', '未知')}")
        print(f"\n📊 分析结果：")
        print(f"  趋势方向：{decision.get('trend')}")
        print(f"  信号：{decision.get('signal')}")
        print(f"  置信度：{decision.get('confidence')}")
        print(f"  信号强度：{decision.get('signal_strength')}/10")
        print(f"  成交量确认：{decision.get('volume_confirmed')}")
        print(f"  风险回报比：{decision.get('risk_reward')}")
        print(f"  分析理由：{decision.get('reason', '')[:100]}")

        passed = passes_risk_filter(decision)
        print_result(passed, f"风控过滤：{'通过' if passed else '未通过（正常，不一定有信号）'}")

        return decision
    except Exception as e:
        print_result(False, f"AI分析失败：{e}")
        traceback.print_exc()
        return None


def test_risk_filter(decision, exchange):
    """Step 6: 测试风控模块"""
    print_step(6, "风控模块测试")
    try:
        from risk_filter import (
            check_signal_quality,
            calculate_position_size
        )

        passed, reason = check_signal_quality(decision)
        print_result(passed, f"信号质量：{reason}")

        # 测试仓位计算（使用mock数据）
        position = calculate_position_size(
            balance_usdt=1000,
            entry_price=decision.get("entry_price", 60000),
            stop_loss=decision.get("stop_loss", 58000)
        )
        if position:
            print_result(True, f"仓位计算成功：")
            print(f"  合约数量：{position['contracts']}")
            print(f"  所需保证金：{position['margin_usdt']} USDT")
            print(f"  最大风险：{position['risk_usdt']} USDT")
        else:
            print_result(False, "仓位计算返回空")

        return True
    except Exception as e:
        print_result(False, f"风控模块测试失败：{e}")
        traceback.print_exc()
        return False


def main():
    print("\n🚀 Crypto Trend Trader 整体联调测试")
    print("=" * 50)

    results = {}

    # Step 1: 配置
    cfg, sym = test_config()
    results["config"] = cfg is not None

    if not cfg:
        print("\n❌ 配置加载失败，终止测试")
        return

    # Step 2: 交易所
    exchange = test_exchange(cfg)
    results["exchange"] = exchange is not None

    # Step 3: K线数据
    data, support, resistance, symbol = test_kline(sym)
    results["kline"] = data is not None

    if not data:
        print("\n❌ K线数据获取失败，终止测试")
        return

    # Step 4: K线图
    paths = test_chart(data, support, resistance, symbol)
    results["chart"] = paths is not None

    if not paths:
        print("\n❌ K线图生成失败，终止测试")
        return

    # Step 5: AI分析
    decision = test_ai(paths)
    results["ai"] = decision is not None

    if not decision:
        print("\n❌ AI分析失败，终止测试")
        return

    # Step 6: 风控模块
    risk_ok = test_risk_filter(decision, exchange)
    results["risk_filter"] = risk_ok

    # 最终汇总
    print(f"\n{'='*50}")
    print("📋 联调测试汇总")
    print('='*50)

    all_passed = True
    step_names = {
        "config":      "Step 1: 配置加载",
        "exchange":    "Step 2: 交易所连接",
        "kline":       "Step 3: K线数据获取",
        "chart":       "Step 4: K线图生成",
        "ai":          "Step 5: AI视觉分析",
        "risk_filter": "Step 6: 风控模块",
    }

    for key, name in step_names.items():
        passed = results.get(key, False)
        print_result(passed, name)
        if not passed:
            all_passed = False

    print(f"\n{'='*50}")
    if all_passed:
        print("🎉 所有模块联调通过！系统已就绪")
        print("\n下一步操作：")
        print("  1. 确认 config/settings.yaml 中 testnet: true")
        print("  2. 在 OpenClaw 中启动 market_scanner skill")
        print("  3. 在 OpenClaw 中启动 trade_manager skill")
        print("  4. 在 OpenClaw 中启动 daily_report skill")
        print("  5. 观察日志和通知，确认运行正常")
        print("  6. 测试网稳定运行2周后，再考虑切换主网")
    else:
        print("⚠️  部分模块存在问题，请根据上方错误信息逐一排查")
        print("\n常见排查步骤：")
        print("  - 检查 .env 文件中的 API Key 是否正确")
        print("  - 检查网络是否可以访问交易所 API")
        print("  - 检查阿里云 DASHSCOPE_API_KEY 是否有效")
        print("  - 运行单个脚本逐步排查（见 README.md）")

    print('='*50)


if __name__ == "__main__":
    import os
    os.chdir(Path(__file__).parent.parent)
    main()