#!/bin/sh
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Création du virtualenv..."
  python3 -m venv .venv
fi

. .venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

python bot.py
