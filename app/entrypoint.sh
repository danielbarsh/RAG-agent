#!/bin/sh
set -e
if [ "${ROLE}" = "worker" ]; then
  echo "starting worker"
  exec python -m app.worker
fi
echo "starting api"
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --timeout-keep-alive 120 --workers 1
