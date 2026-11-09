#!/usr/bin/env bash
set -e

if [ -f /etc/debian_version ]; then
    sudo apt update
    sudo apt install -y python3 python3-pip python3-venv git
elif [ -f /etc/oracle-release ] || [ -f /etc/redhat-release ]; then
    sudo dnf install -y python3 python3-pip git
fi

python3 -m venv venv
source venv/bin/activate

pip install --upgrade pip
pip install -r requirements.txt

playwright install chromium
playwright install-deps chromium

if [ ! -f config.json ] && [ -f config.example.json ]; then
    cp config.example.json config.json
fi

echo "environment ready."
echo "set your session with: python streak.py --set-cookie '<sessionid>'"
