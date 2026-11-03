#!/usr/bin/env bash
# Build the frontend (if needed) and start the server on http://127.0.0.1:8000
set -euo pipefail
cd "$(dirname "$0")"
if [ ! -f frontend/dist/index.html ]; then (cd frontend && npm run build); fi
cd backend
exec python3 -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"
