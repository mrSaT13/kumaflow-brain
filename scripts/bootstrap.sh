#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

ROOT=$(pwd)

echo "==> Creating server venv"
cd "$ROOT/server"
python3 -m venv .venv
# shellcheck disable=SC1091
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

if [ ! -f .env ]; then
  cp .env.example .env
  echo "==> Created server/.env — please fill in credentials"
fi

echo "==> Installing web deps"
cd "$ROOT/web"
if [ ! -f .env.local ]; then
  cp .env.example .env.local
fi
npm install

echo "==> Done. Start backend with:"
echo "    cd server && source .venv/bin/activate && uvicorn app.main:app --reload"
echo "==> Start worker in another terminal:"
echo "    cd server && source .venv/bin/activate && python -m app.workers.rq_worker"
echo "==> Start frontend:"
echo "    cd web && npm run dev"
