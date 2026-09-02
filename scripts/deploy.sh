#!/usr/bin/env bash
#
# Empty subscription -> running system, in one command.
#
#   ./scripts/deploy.sh
#
# What it does, and why it is not a single `terraform apply`:
#   1. Creates the resource group and the container registry only. The registry
#      has to exist before an image can be pushed into it, and the container
#      apps will not start without an image.
#   2. Builds the image with `az acr build`, which runs the Docker build inside
#      Azure. Nobody needs Docker installed, and the build layer cache lives in
#      the registry.
#   3. Applies the rest of the infrastructure with that image tag.
#   4. Terraform then runs scripts/provision_search.py, which PUTs the index,
#      skillset, indexer and data source - the objects the azurerm provider does
#      not model.
#
set -euo pipefail

cd "$(dirname "$0")/.."
ROOT="$PWD"
INFRA="$ROOT/infra"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Missing $1. See README prerequisites."; exit 1; }; }
need az
need terraform
need python3

echo "==> Azure account"
az account show --query "{subscription:name, tenant:tenantId}" -o tsv

echo "==> Python environment for the search provisioner"
python3 -m venv "$ROOT/.venv" 2>/dev/null || true
# shellcheck disable=SC1091
source "$ROOT/.venv/bin/activate"
pip install --quiet --disable-pip-version-check -r "$ROOT/scripts/requirements.txt"

echo "==> Terraform init"
terraform -chdir="$INFRA" init -input=false

echo "==> Stage 1: resource group and container registry"
terraform -chdir="$INFRA" apply -input=false -auto-approve \
  -target=azurerm_container_registry.acr

ACR="$(terraform -chdir="$INFRA" output -raw registry_name)"
TAG="$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M%S)"

echo "==> Stage 2: build image ${ACR}.azurecr.io/sprag:${TAG} in Azure"
az acr build --registry "$ACR" --image "sprag:${TAG}" --file "$ROOT/app/Dockerfile" "$ROOT/app"

echo "==> Stage 3: everything else, plus the search objects"
terraform -chdir="$INFRA" apply -input=false -auto-approve -var="image_tag=${TAG}"

URL="$(terraform -chdir="$INFRA" output -raw app_url)"
echo
echo "Done."
echo "  Application : $URL"
echo "  Storage     : $(terraform -chdir="$INFRA" output -raw storage_account)"
echo "  Search      : $(terraform -chdir="$INFRA" output -raw search_endpoint)"
echo
echo "Sign in with the account that ran this script; it was granted the FileAdmin role."
echo "To put sample documents in the library:  ./scripts/seed_demo.sh"
