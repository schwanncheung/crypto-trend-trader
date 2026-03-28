#!/usr/bin/env bash
# backtest/install.sh
# 回测系统依赖安装脚本
# 用法：bash backtest/install.sh
# 在项目根目录下执行

set -euo pipefail

echo "========================================"
echo "  Crypto Trend Trader — 回测依赖安装"
echo "========================================"

# ── Python 版本检查 ────────────────────────────────────────────────
PYTHON=$(command -v python3 || command -v python || echo "")
if [[ -z "$PYTHON" ]]; then
  echo "[ERROR] 未找到 Python，请先安装 Python 3.10+"
  exit 1
fi

PY_VERSION=$("$PYTHON" -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
PY_MAJOR=$("$PYTHON" -c "import sys; print(sys.version_info.major)")
PY_MINOR=$("$PYTHON" -c "import sys; print(sys.version_info.minor)")

echo "Python 版本：$PY_VERSION"

if [[ "$PY_MAJOR" -lt 3 ]] || [[ "$PY_MAJOR" -eq 3 && "$PY_MINOR" -lt 10 ]]; then
  echo "[ERROR] 需要 Python 3.10 或以上，当前版本：$PY_VERSION"
  exit 1
fi

# ── 虚拟环境检测 ──────────────────────────────────────────────────
if [[ -z "${VIRTUAL_ENV:-}" ]] && [[ -z "${CONDA_DEFAULT_ENV:-}" ]]; then
  echo ""
  echo "[警告] 未检测到虚拟环境，建议先创建："
  echo "  python3 -m venv .venv && source .venv/bin/activate"
  echo ""
  read -r -p "是否继续（直接安装到系统 Python）？[y/N] " CONFIRM
  if [[ "$CONFIRM" != "y" && "$CONFIRM" != "Y" ]]; then
    echo "已取消。"
    exit 0
  fi
else
  echo "虚拟环境：${VIRTUAL_ENV:-$CONDA_DEFAULT_ENV}"
fi

# ── 升级 pip ──────────────────────────────────────────────────────
echo ""
echo "[1/3] 升级 pip..."
"$PYTHON" -m pip install --upgrade pip --quiet

# ── 安装全量依赖 ──────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REQ_FILE="$PROJECT_ROOT/requirements.txt"

if [[ ! -f "$REQ_FILE" ]]; then
  echo "[ERROR] 未找到 $REQ_FILE"
  exit 1
fi

echo "[2/3] 安装依赖（requirements.txt）..."
"$PYTHON" -m pip install -r "$REQ_FILE"

# ── 验证关键包 ────────────────────────────────────────────────────
echo ""
echo "[3/3] 验证关键依赖..."

MISSING=()
check_pkg() {
  local pkg="$1"
  local import_name="${2:-$1}"
  if "$PYTHON" -c "import $import_name" 2>/dev/null; then
    echo "  [OK] $pkg"
  else
    echo "  [FAIL] $pkg"
    MISSING+=("$pkg")
  fi
}

check_pkg "ccxt"
check_pkg "pandas"
check_pkg "pyarrow"
check_pkg "plotly"
check_pkg "jinja2"
check_pkg "tqdm"
check_pkg "pyyaml" "yaml"
check_pkg "python-dotenv" "dotenv"
check_pkg "scipy"
check_pkg "mplfinance"
check_pkg "numpy"
check_pkg "openai"
check_pkg "httpx"

echo ""
if [[ ${#MISSING[@]} -eq 0 ]]; then
  echo "========================================"
  echo "  所有依赖安装成功！"
  echo "========================================"
  echo ""
  echo "下一步："
  echo "  # 下载历史数据"
  echo "  python backtest/run_backtest.py download \\"
  echo "      --symbols BTC/USDT:USDT ETH/USDT:USDT \\"
  echo "      --start 2024-01-01"
  echo ""
  echo "  # 运行回测"
  echo "  python backtest/run_backtest.py backtest \\"
  echo "      --start 2024-01-01 --end 2025-01-01"
else
  echo "[ERROR] 以下包安装失败：${MISSING[*]}"
  echo "请手动执行：$PYTHON -m pip install ${MISSING[*]}"
  exit 1
fi
