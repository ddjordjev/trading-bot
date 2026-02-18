#!/usr/bin/env bash
set -euo pipefail

echo "=== Trading Bot - Local Run ==="

cd "$(dirname "$0")/.."

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi

source .venv/bin/activate

echo "Installing dependencies..."
pip install -q -r requirements.txt

if [ ! -f .env ]; then
    echo "No .env file found. Copying from .env.example..."
    cp .env.example .env
    echo "Please edit .env with your API keys, then run this script again."
    exit 1
fi

mkdir -p logs data

MODE="${1:-all}"

if [ "$MODE" = "bot" ]; then
    echo "Starting bot only (no separate monitor/analytics)..."
    python bot.py
elif [ "$MODE" = "monitor" ]; then
    echo "Starting monitor service only..."
    python run_monitor.py
elif [ "$MODE" = "analytics" ]; then
    echo "Starting analytics service only..."
    python run_analytics.py
else
    echo "Starting all services..."
    echo "  Bot + Dashboard:  python bot.py"
    echo "  Monitor service:  python run_monitor.py"
    echo "  Analytics service: python run_analytics.py"
    echo ""
    echo "Launching in parallel (Ctrl+C to stop all)..."
    python bot.py &
    BOT_PID=$!
    sleep 2
    python run_monitor.py &
    MON_PID=$!
    python run_analytics.py &
    ANA_PID=$!

    trap "kill $BOT_PID $MON_PID $ANA_PID 2>/dev/null; exit" INT TERM
    wait
fi
