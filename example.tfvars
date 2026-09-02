# Copy to terraform.tfvars (gitignored) to override defaults.
# name_prefix     = "sprag"
# location        = "swedencentral"
# openai_location = "eastus2"

# Free (50 MB, no semantic ranker, no SLA) is the default so an idle deployment
# costs nothing. Switch to basic once the index outgrows ~250 PDFs.
# search_sku = "basic"

# Removes the 5-15s cold start on the first request, at roughly $12/month.
# api_min_replicas = 1

# Only if your tenant forbids app registrations. Leaves the app public.
# enable_auth = false
