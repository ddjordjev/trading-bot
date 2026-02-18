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

mkdir -p logs

echo "Starting bot..."
python bot.py
