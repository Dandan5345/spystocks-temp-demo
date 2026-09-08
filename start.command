#!/bin/zsh
set -e

cd -- "$(dirname "$0")"

if [[ ! -x .venv/bin/python ]]; then
  echo "Preparing מערכת בדיקת מידע for first use..."
  python3 -m venv .venv
  .venv/bin/python -m pip install --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
fi

(sleep 2; open "http://127.0.0.1:8000") &
exec .venv/bin/python run.py
