#!/usr/bin/env bash
# Removes everything, including the Entra ID app registration.
set -euo pipefail
cd "$(dirname "$0")/.."
terraform -chdir=infra destroy -auto-approve
echo "Resource group and app registration removed."
echo "Azure OpenAI accounts are soft-deleted for 48h; purge_soft_delete_on_destroy is on, so the name is reusable immediately."
