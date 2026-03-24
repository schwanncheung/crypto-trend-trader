#!/usr/bin/env python3
"""
K线图生成脚本
使用 mplfinance 绘制专业K线图，支持多时间框架和支撑阻力标注
"""

import os
import base64
import logging
from datetime import datetime, timezone
from config_loader import now_cst, now_cst_str
from pathlib import Path

import pandas as pd
import numpy as np
import matplotlib
import matplotlib.pyplot as plt
import matplotlib.font_manager as fm
import mplfinance as mpf
import yaml
from dotenv import load_dotenv
from pathlib import Path
import sys

# ── 中文字体注册（必须在 mplfinance 导入后立即执行）──
_FONT_PATH = "/usr/share/fonts/google-noto-cjk/NotoSansCJK-Regular.ttc"
if Path(_FONT_PATH).exists():
    fm.fontManager.addfont(_FONT_PATH)
    matplotlib.rcParams["font.family"] = "sans-serif"
    matplotlib.rcParams["font.sans-serif"] = ["Noto Sans CJK JP", "DejaVu Sans"]
    matplotlib.rcParams["axes.unicode_minus"] = False

# 配置日志：同时输出到控制台和文件
from config_loader import CHART_CFG, setup_logging
setup_logging("generate_chart")
logger = logging.getLogger(__name__)

# 加载环境变量
load_dotenv()

# 项目根目录
PROJECT_ROOT = Path(__file__).parent.parent


def load_config() -> dict:
    """加载配置文件"""
    config_path = PROJECT_ROOT / "config" / "settings.yaml"
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def format_symbol_for_filename(symbol: str) -> str:
    """
    格式化交易对名称用于文件名
    BTC/USDT:USDT -> BTC_USDT_USDT
    """
    return symbol.replace("/", "_").replace(":", "_")


def generate_timestamp() -> str:
    """生成时间戳字符串"""
    return now_cst_str()


def generate_kline_chart(
    df: pd.DataFrame,
    symbol: str,
    timeframe: str,
    support_levels: list = None,
    resistance_levels: list = None,
    volume_ma: pd.Series = None,
    output_dir: str = "logs/decisions"
) -> str:
    """
    生成K线图并保存为PNG
    
    Args:
        df: K线数据 DataFrame
        symbol: 交易对名称
        timeframe: 时间框架
        support_levels: 支撑位列表
        resistance_levels: 阻力位列表
        volume_ma: 成交量均线
        output_dir: 输出目录
    
    Returns:
        图片路径
    """
    if df.empty:
        print(f"⚠️ 数据为空，跳过图表生成")
        return ""
    
    # 确保输出目录存在
    output_path = PROJECT_ROOT / output_dir
    output_path.mkdir(parents=True, exist_ok=True)
    
    # 文件名
    timestamp = generate_timestamp()
    filename = f"{format_symbol_for_filename(symbol)}_{timeframe}_{timestamp}.png"
    filepath = output_path / filename
    
    # 准备数据（mplfinance 需要特定格式）
    plot_df = df.copy()
    plot_df.index = pd.to_datetime(plot_df.index)
    
    # 自定义样式
    mc = mpf.make_marketcolors(
        up="#26a69a",       # 上涨绿色
        down="#ef5350",     # 下跌红色
        edge="inherit",
        wick="inherit",
        volume="in",
    )
    
    style = mpf.make_mpf_style(
        marketcolors=mc,
        gridstyle="-",
        gridcolor="#404040",
        facecolor="#1a1a1a",
        edgecolor="#333333",
        figcolor="#1a1a1a",
        rc={
            "font.size": 10,
            "font.family": "sans-serif",
            "font.sans-serif": ["Noto Sans CJK JP", "DejaVu Sans"],
            "axes.unicode_minus": False,
            "axes.labelcolor": "#cccccc",
            "axes.titlesize": 12,
            "xtick.color": "#cccccc",
            "ytick.color": "#cccccc",
        }
    )
    
    # 附加绘图元素
    apds = []
    
    # 20日均线（主图）
    if len(plot_df) >= 20:
        ma20 = plot_df["close"].rolling(window=20).mean()
        apds.append(mpf.make_addplot(ma20, color="#FFA500", width=1))
    
    # 成交量均线（副图）
    if volume_ma is not None and not volume_ma.empty:
        apds.append(
            mpf.make_addplot(
                volume_ma, 
                color="#FFFF00", 
                width=1, 
                panel=1, 
                linestyle="--"
            )
        )
    
    # 支撑位和阻力位水平线
    if support_levels:
        for level in support_levels:
            apds.append(
                mpf.make_addplot(
                    [level] * len(plot_df),
                    color="#2196F3",
                    linestyle="--",
                    width=1,
                )
            )
    
    if resistance_levels:
        for level in resistance_levels:
            apds.append(
                mpf.make_addplot(
                    [level] * len(plot_df),
                    color="#F44336",
                    linestyle="--",
                    width=1,
                )
            )
    
    # 标题
    title = f"{symbol} - {timeframe} - {now_cst_str('%Y-%m-%d %H:%M')} CST"
    
    # 绘制图表
    try:
        mpf.plot(
            plot_df,
            type="candle",
            style=style,
            title=title,
            ylabel="价格",
            ylabel_lower="成交量",
            volume=True,
            addplot=apds if apds else None,
            panel_ratios=(4, 1),
            figsize=(16, 9),
            savefig=str(filepath),
        )
        
        print(f"✅ 图表已生成：{filepath}")
        return str(filepath)
        
    except Exception as e:
        print(f"❌ 图表生成失败: {e}")
        return ""


def generate_multi_chart(
    multi_tf_data: dict,
    symbol: str,
    support_levels: list = None,
    resistance_levels: list = None,
    save_dir: str = "logs/decisions"
) -> dict:
    """
    生成四周期K线图
    返回：各周期 {tf: "/path/to/file.png"} 字典
    """
    from pathlib import Path
    import os

    save_path = Path(save_dir)
    save_path.mkdir(parents=True, exist_ok=True)

    # 时间戳前缀，避免文件名冲突
    ts = now_cst_str()
    # 合约名去掉特殊字符，适合做文件名
    safe_symbol = symbol.replace("/", "").replace(":", "")

    chart_paths = {}

    for tf, df in multi_tf_data.items():
        if df is None or df.empty:
            logger.warning(f"[{tf}] 数据为空，跳过生成图表")
            continue

        try:
            # 图片保存路径
            filename = f"{safe_symbol}_{tf}_{ts}.png"
            filepath = str(save_path / filename)

            # 计算成交量均线
            volume_ma = calculate_volume_ma(df, period=5)
            
            # 计算支撑阻力（如果未传入，使用 multi_tf_data 中第一个有效周期）
            ref_support = support_levels
            ref_resistance = resistance_levels
            if not support_levels:
                ref_tf = next((tf for tf, d in multi_tf_data.items() if d is not None and not d.empty), None)
                if ref_tf:
                    from fetch_kline import calculate_support_resistance
                    ref_support, ref_resistance = calculate_support_resistance(multi_tf_data[ref_tf])

            # 绘制K线图（直接调用 generate_kline_chart）
            path = generate_kline_chart(
                df=df,
                symbol=symbol,
                timeframe=tf,
                support_levels=ref_support,
                resistance_levels=ref_resistance,
                volume_ma=volume_ma,
                output_dir=save_dir
            )
            
            # ✅ 关键：验证文件真实存在后再加入返回值
            if os.path.exists(path):
                chart_paths[tf] = path
                logger.info(f"  [{tf}] 图表生成成功：{path}")
            else:
                logger.error(f"  [{tf}] 图表文件生成后不存在：{path}")

        except Exception as e:
            logger.error(f"  [{tf}] 图表生成失败：{e}")
            continue

    # 最终检查：至少要有1张图才能继续
    if not chart_paths:
        raise RuntimeError(f"{symbol} 所有周期图表均生成失败，无法进行AI分析")

    logger.info(f"图表生成完成，共 {len(chart_paths)} 张：{list(chart_paths.keys())}")
    return chart_paths  # {"1d": "/path/xxx.png", "4h": "/path/xxx.png", ...}


def calculate_volume_ma(df: pd.DataFrame, period: int = 5) -> pd.Series:
    """
    计算成交量移动平均
    
    Args:
        df: K线数据
        period: 均线周期
    
    Returns:
        成交量均线 Series
    """
    if df.empty or "volume" not in df.columns:
        return pd.Series()
    
    return df["volume"].rolling(window=period).mean()


def encode_image_base64(image_path: str) -> str:
    """
    将图片编码为base64字符串
    
    Args:
        image_path: 图片路径
    
    Returns:
        base64 编码字符串（不含 data:image 前缀）
    """
    try:
        with open(image_path, "rb") as f:
            return base64.b64encode(f.read()).decode("utf-8")
    except Exception as e:
        print(f"❌ 图片Base64编码失败: {e}")
        return ""


def add_chart_annotations(
    filepath: str,
    swing_highs: list = None,
    swing_lows: list = None,
    recent_price: float = None
) -> str:
    """
    在图表上添加标注（波段高低点）
    注意：此函数需要 PIL/Pillow，可选实现
    
    Args:
        filepath: 图表路径
        swing_highs: 波段高点列表
        swing_lows: 波段低点列表
        recent_price: 最新价
    
    Returns:
        标注后的图片路径
    """
    # TODO: 使用 PIL 在图片上添加 H/L 标注
    # 当前版本通过 mplfinance 标注
    return filepath


if __name__ == "__main__":
    from fetch_kline import fetch_multi_timeframe, calculate_support_resistance
    
    symbol = "BTC/USDT:USDT"
    print(f"获取 {symbol} 多周期数据...")
    
    data = fetch_multi_timeframe(symbol)
    
    # 计算支撑阻力（使用4h）
    support = []
    resistance = []
    if "4h" in data and not data["4h"].empty:
        support, resistance = calculate_support_resistance(data["4h"])
        print(f"支撑位: {support}")
        print(f"阻力位: {resistance}")
    
    # 生成1小时图测试
    if "1h" in data and not data["1h"].empty:
        path = generate_kline_chart(
            df=data["1h"],
            symbol=symbol,
            timeframe="1h",
            support_levels=support,
            resistance_levels=resistance,
            volume_ma=calculate_volume_ma(data["1h"])
        )
        print(f"1H图表已保存：{path}")
    
    # 生成四周期图
    print("\n生成四周期图表...")
    paths = generate_multi_chart(data, symbol, support, resistance)
    for tf, p in paths.items():
        print(f"[{tf}] {p}")