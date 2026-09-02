# Limitations

Honest inventory. Split into what is stubbed on purpose, what is thin, what
breaks under load, and what I would do next.

## Stubbed on purpose (the brief permits these)

- **OCR for scanned PDFs.** A PDF with no text layer is indexed as an empty
  document. The indexer reports it as a warning, so it is visible rather than
  silent, but it is not searchable. The seam is one skill: insert a
  `DocumentIntelligenceLayoutSkill` (or `OcrSkill` + `MergeSkill`) before
  `SplitSkill` in `scripts/search_definitions.py`. It is not enabled because
  Document Intelligence is billed per page with no useful free tier, and the
  brief says not to spend money.
- **Non-PDF file types.** `indexedFileNameExtensions: ".pdf"` on the indexer and
  a `.pdf` check in the upload validator. Removing both is enough for Office
  documents; the blob indexer already cracks them.
- **Permission trimming.** Everyone who can sign in can search everything. The
  shape of the real thing: carry an ACL field on each chunk populated from the
  source system, and add `filter: groups/any(g: search.in(g, '<user groups>'))`
  to every query, with the group list taken from the validated principal rather
  than from the request.
- **Multiple libraries.** One container, one data source, one indexer, one index.
  Multi-library means one data source and indexer per container projecting into
  a shared index with a `library` field, plus that field in the query filter.
- **Multi-tenant.** Single tenant throughout: one Entra app registration, one
  storage account, one index.

## Thin, and I would rather say so

- **The Terraform has not been applied end to end in this environment.** It was
  written against the `azurerm` 4.x, `azuread` 3.x and `azapi` 2.x schemas but I
  had no Azure subscription available while writing it, so `terraform validate`
  and `plan` have not been run. Expect one or two argument-name corrections on
  first apply, most likely on `azurerm_storage_table`
  (`storage_account_name` vs `storage_account_id`, which moved between provider
  majors) and on the `azapi` auth config body shape. The Python, shell and
  JavaScript are syntax-checked; the sample-PDF generator is round-tripped
  through a PDF parser.
- **Free-tier search and data-plane RBAC.** The service is created with
  `local_authentication_enabled = false`, which requires Entra data-plane
  authentication to be available on the tier in use. If the free tier refuses,
  the fix is either `-var search_sku=basic` or re-enabling key auth and putting
  the admin key in a Container Apps secret — the second is worse and I would
  take the first.
- **Chat history lives in one Table Storage property.** Trimmed to the last 24
  messages and 60 KB. Long conversations silently lose their beginning. A real
  version stores one entity per message.
- **File resolution is string matching**, not retrieval. `store.resolve_file`
  scores exact match, prefix, substring and token overlap. It is deliberately
  explainable rather than clever, but it will not resolve "the contract we signed
  with the caterers" unless the file name says so. The better version searches
  the index for the reference and resolves through `title` facets on the hits.
- **The staging container is never garbage collected.** Uploads that are never
  confirmed accumulate. One lifecycle management rule (`delete after 7 days`)
  fixes it; it is not in the Terraform.
- **`propose_add` decides between add and replace itself** when a file of that
  name already exists. That is convenient, and it means an "add" can turn into an
  overwrite. It is safe because the confirmation card says "Replace" and names
  the file, but it is a place where the model's framing and the actual operation
  could diverge if the card were ever skipped.
- **No CSRF token on the confirm endpoint.** It is a JSON POST from the same
  origin behind Entra sign-in with a single-use, user-scoped proposal id, which
  is a high bar to clear, but a `SameSite` cookie assumption is doing work here
  that an explicit token should do.
- **The worker polls three loops in one process.** Simple and cheap; it also
  means one wedged loop is invisible unless you read the logs. A real version
  gives each loop a heartbeat.

## What breaks under load

Ordered, with the reasoning in `ARCHITECTURE.md` §3.5:

1. Azure OpenAI embedding TPM quota during a large backfill. This is the first
   wall and it arrives at tens of thousands of documents, not millions.
2. Blob enumeration for change detection at a five-minute cadence over hundreds
   of thousands of blobs.
3. The 24-hour indexer run cap, which turns a backfill into a multi-day planned
   operation rather than a button.
4. Log Analytics ingestion cost.
5. The delete sweep's `$skip` paging, capped at 100,000 results. Theoretical for
   one document; real if the sweep were ever generalised to a prefix.
6. `api_min_replicas = 0` means the first request after idle waits 5-15 seconds.
   Fine for a demo, wrong for a tool people use daily.

## What I would do next, in priority order

1. **Apply it against a live subscription** and fix whatever the provider
   schemas disagree with. Everything else is speculation until that is done.
2. **Permission trimming**, because a shared policy library where everyone sees
   everything is not a shipping product, and retrofitting ACLs into an index
   after the fact is painful.
3. **Reconciliation job.** A nightly pass that lists the container, lists
   distinct `source_path` values in the index, and reports both directions of
   drift. Today the system is self-healing through the indexer schedule but
   nothing *proves* the index matches the library; that report is what makes the
   "how would you know" question answerable without reading logs.
4. **Alerts, not just logs.** Two rules: no successful indexer run in 30 minutes,
   and `itemsFailed > 0`. Both are one KQL query against the structured line the
   worker already emits.
5. **Per-message session storage** and a proper conversation list in the UI.
6. **Layout-aware chunking** via Document Intelligence for the contracts, where
   tables and clause numbering carry meaning that character-window chunking
   destroys. This is the largest available quality win and the only one that
   costs money.
7. **Evaluation.** A fixed set of question-and-expected-source pairs run against
   the index on every change to the chunking or the model, so "we changed the
   chunk size" stops being a matter of opinion.
