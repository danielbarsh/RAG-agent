#!/usr/bin/env bash
# Puts the sample PDFs into the library and waits for them to become searchable.
set -euo pipefail
cd "$(dirname "$0")/.."

ACCOUNT="$(terraform -chdir=infra output -raw storage_account)"
CONTAINER="$(terraform -chdir=infra output -raw documents_container)"

python3 scripts/make_sample_pdfs.py samples

for pdf in samples/*.pdf; do
  echo "uploading $(basename "$pdf")"
  az storage blob upload \
    --account-name "$ACCOUNT" \
    --container-name "$CONTAINER" \
    --name "$(basename "$pdf")" \
    --file "$pdf" \
    --auth-mode login \
    --overwrite \
    --only-show-errors >/dev/null
done

echo
echo "Uploaded. Event Grid has already told the worker; the indexer usually finishes"
echo "within 30-60 seconds. Watch the Index panel in the app, or:"
echo "  curl -s \"$(terraform -chdir=infra output -raw app_url)/api/indexer/status\""
