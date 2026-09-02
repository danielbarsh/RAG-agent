# Architecture

## The shape of it

```
                      ┌───────────────────────────────────────────┐
   Person             │  Container App: api  (scale 0..3)         │
   browser  ─────────▶│  Entra ID sign-in (Container Apps auth)   │
                      │  FastAPI + static front end               │
                      └───┬───────────────┬──────────────┬────────┘
                          │               │              │
              SSE chat    │      job      │     SAS      │  read-only
              + proposals │   confirm     │   for upload │  search
                          │               │              │
                          ▼               ▼              │
                 ┌────────────────┐  ┌──────────┐        │
                 │ Table Storage  │  │  Queue   │        │
                 │ jobs/proposals │  │  "jobs"  │        │
                 │ sessions       │  └────┬─────┘        │
                 └────────────────┘       │              │
                                          │ KEDA         │
                                          ▼              │
                      ┌───────────────────────────────┐  │
                      │ Container App: worker (0..3)  │  │
                      │  add / replace / delete       │  │
                      │  lease, copy, sweep, verify   │  │
                      └───┬───────────────────────┬───┘  │
                          │                       │      │
        blob lease + copy │                       │ run  │
                          ▼                       │      │
   browser ══ SAS PUT ══▶ ┌──────────────────┐    │      │
   (50 MB direct)         │  Blob Storage    │    │      │
                          │  documents/      │    │      │
                          │  staging/        │    │      │
                          └───┬──────────────┘    │      │
                              │ BlobCreated       │      │
                              │ BlobDeleted       │      │
                              ▼                   │      │
                        ┌───────────┐  queue      │      │
                        │Event Grid │────────────▶│      │
                        └───────────┘ index-events│      │
                                                  ▼      ▼
                      ┌────────────────────────────────────────┐
                      │  Azure AI Search                        │
                      │   blob indexer  (schedule PT5M +        │
                      │                  on-demand runs)        │
                      │   skillset: SplitSkill -> Embedding     │
                      │   index projections -> docs-chunks      │
                      │   alias "docs"                          │
                      └──────────────┬─────────────────────────┘
                                     │ embed (indexing + query)
                                     ▼
                            ┌──────────────────┐
                            │  Azure OpenAI     │
                            │  embedding + chat │
                            └──────────────────┘
```

Solid arrows are calls made with a managed identity. Nothing in this diagram
carries an API key.

## The three flows

**Indexing.** A blob lands in `documents/`. Event Grid raises `BlobCreated`,
which goes to the `index-events` queue. The worker wakes, coalesces whatever
arrived in the last three seconds into one `indexer/run`, and tolerates a 409 if
a run is already in flight. The indexer cracks the PDF, `SplitSkill` chunks it,
`AzureOpenAIEmbeddingSkill` embeds each chunk with the search service's own
identity, and index projections write one document per chunk into
`docs-chunks`. Independently of all of that, the indexer also runs every five
minutes, which is the guarantee when the event path is broken.

**Asking.** The browser POSTs to `/api/chat` and reads server-sent events. The
model calls `search_documents`; the API runs a hybrid query (BM25 + HNSW, fused
with RRF, semantically reranked above the free tier) against the `docs` alias
and hands back numbered, delimited passages. Tools are then removed from the
conversation, and the completion that reads those passages streams a cited
answer with no ability to call anything.

**Changing.** The browser gets a user-delegation SAS and PUTs the PDF straight
into `staging/`. The model calls `propose_delete` / `propose_replace` /
`propose_add`, which resolve the reference to exactly one file and write a
single-use proposal row. The browser renders a confirmation card naming that
file. On confirmation, the API — checking the `FileAdmin` role and proposal
ownership — writes a job row and a queue message. KEDA wakes the worker, which
takes a blob lease, performs the copy or delete, triggers the indexer, sweeps
the chunks, verifies, and writes the outcome to the job row that the browser is
polling.

---

## 3.4 Every resource: what was chosen, what was rejected, and why

### Indexing compute — Azure AI Search indexer + skillset

**Chosen** because the hard parts of an indexing pipeline are not chunking and
embedding, they are change detection, deletion detection, batching, throttling,
per-item error isolation and a run report. The blob indexer has all six and they
are free. Our code contributes the chunk parameters, the trigger and the
verification.

- *Azure Functions doing the loop by hand* — rejected. It means reimplementing a
  LastModified high-water mark, a deletion detection policy, retry on 429 from
  the embedding endpoint, and per-document error quarantine, and then defending
  each of those reimplementations at a review.
- *Azure Data Factory / Synapse pipelines* — rejected. Batch ETL machinery for a
  few thousand small documents; minimum billing granularity and pipeline-run
  charges make it more expensive per document than the thing it orchestrates.
- *Logic Apps* — rejected as the trigger. It is genuinely good at "blob created →
  call an HTTP endpoint", but it is another billed resource for one HTTP call
  that Event Grid already delivers into a queue we already have, and its
  execution history is a worse debugging surface than our own job rows.
- *The portal's "Import and vectorize data" wizard* — rejected outright. It
  produces exactly this topology and 3.2 forbids it, which is the right call: it
  is unversioned and unreproducible.

### API compute — Azure Container Apps (Consumption)

**Chosen** for scale-to-zero (the demo costs nothing idle), a real long-lived
HTTP connection for SSE, built-in Entra authentication, KEDA for the worker, and
managed-identity registry pulls. One resource type covers both the API and the
worker.

- *Azure Functions (Flex Consumption)* — rejected, though close. Streaming
  responses over a long-lived connection are awkward there, Easy Auth on
  Functions is the older module, and running the worker as a queue-triggered
  function would split the codebase across two hosting models for no gain.
- *App Service* — rejected. No scale-to-zero; the smallest always-on plan that
  is not the crippled F1 tier costs more per month than everything else here
  combined.
- *AKS* — rejected. A cluster control plane, node pools and an ingress
  controller to run two containers.
- *Static site + a serverless API* — rejected because it creates a second origin
  and therefore a second authentication boundary for no benefit at this size.

### Queue and worker — Azure Storage Queues + Container Apps + KEDA

**Chosen** because the durability requirements in 2.7 are exactly what a storage
queue gives: at-least-once delivery, a visibility timeout that turns a worker
crash into an automatic retry, a dequeue count that bounds retries, and a
first-class poison queue. It costs about $0.0004 per 10,000 operations and it
already lives in the storage account we need anyway.

- *Azure Service Bus* — rejected for this scale. Sessions, ordering, topics,
  dead-letter subqueues and duplicate detection are real advantages, but they
  cost $10/month for the Basic tier before a single message, and the two things
  we would actually use (dead-lettering, dedupe) we get from a poison queue and
  an ETag-guarded claim. This is the first thing to change if job ordering per
  file ever matters.
- *Durable Functions* — rejected. Orchestration state, replay semantics and a
  second hosting model, to run a three-step job whose state we want visible in a
  table the UI can poll anyway.
- *In-process background tasks in the API* — rejected. It fails 2.3 and 2.7 on
  its own terms: a page refresh or a scale-in loses the job.

### Search tier — Azure AI Search, `free` by default, `basic` for real use

**Chosen** because it is the only Azure service that does document cracking,
chunking, embedding, hybrid retrieval and deletion propagation as one indexed
unit. Hand-assembling that from a vector database plus a parser is more code and
more failure modes.

- *Free tier as the default* — a deliberate cost choice for this exercise. 50 MB
  of index, 3 indexes / indexers / skillsets, shared compute, no SLA, no semantic
  ranker, and a short cap on indexer run time. It holds roughly 200-300 average
  PDFs. `-var search_sku=basic` is the one-word upgrade and the code branches on
  it only to enable semantic ranking.
- *pgvector on Azure Database for PostgreSQL Flexible Server* — rejected.
  Cheaper per GB at scale and a fine vector store, but then chunking, embedding,
  change detection, deletion and hybrid ranking are all application code, and the
  cheapest burstable instance still costs more than the free search tier.
- *Azure Cosmos DB vector search* — rejected for the same reason plus RU
  provisioning as a second cost model to reason about.
- *Qdrant / Weaviate / Pinecone* — rejected. Self-hosting means compute we do not
  otherwise need; the hosted versions are a second vendor, a second bill and a
  second identity system in an Azure-only brief.

### Model hosting — Azure OpenAI, `text-embedding-3-small` and `gpt-4o-mini`

**Chosen** because the embedding model has to be callable by Azure AI Search's
managed identity for both indexing and query-time vectorisation, and Azure
OpenAI is what the `AzureOpenAIEmbeddingSkill` and the `azureOpenAI` vectorizer
speak. Keeping both in one account keeps it to one identity and one RBAC grant.

- `text-embedding-3-small` over `-large`: 1536 dimensions against 3072 halves
  index storage and query latency, at $0.02 per million tokens against $0.13.
  The quality difference on English policy text does not justify 6.5× the cost
  and 2× the index.
- `gpt-4o-mini` over `gpt-4o`: roughly 25× cheaper on input. The job here is to
  call one tool and summarise six retrieved passages with citations, which is
  not a frontier-model task. `-var chat_model=gpt-4o` is one variable if a
  reviewer disagrees.
- *Azure AI Foundry serverless / open-weight models* — rejected: an extra
  deployment surface and no free tier advantage at this size.
- *OpenAI direct* — rejected: a separate API key to store and rotate, in a
  design whose main claim is that it has no keys.
- *Azure OpenAI "On Your Data"* — rejected. It is the obvious first-party option
  and it would collapse Part 2's retrieval into one call, but it owns the
  chunking, the index schema and the citation format, so 1.3 and 1.4 could not be
  answered, and the injection firewall in 2.6 would be someone else's
  implementation detail.
- *Copilot Studio / Microsoft 365 Copilot* — rejected. Per-user licensing, no
  control over the index, and nothing to defend at a review.

### Secrets — managed identity, and two secrets that are not ours

**Chosen**: a single user-assigned managed identity for the API and the worker,
a system-assigned identity for the search service, and RBAC grants instead of
keys. Azure AI Search is created with `local_authentication_enabled = false`, so
there is no admin key to leak. The browser never receives a token of any kind:
the platform's auth module holds the session and injects a validated principal
header.

Two secrets exist and both are server-side:
1. The Entra client secret used by Container Apps built-in auth. Required by the
   confidential-client flow; stored as a Container Apps secret, referenced by
   name, never in git.
2. The storage connection string used by the KEDA queue-length scale rule. KEDA
   cannot use a managed identity for this through the `azurerm` provider today.
   Also a Container Apps secret.

- *Azure Key Vault* — considered and rejected for this deployment. With managed
  identity there is nothing left for it to hold except those two values, which
  Container Apps already encrypts at rest and scopes to the app. Adding it would
  mean a vault, a soft-delete policy, an access grant and a per-transaction bill
  for two strings that are already handled. It becomes correct the moment a
  third-party API key enters the system, and the seam is one `secret` block.
- *Keys in app settings* — rejected on 2.9.

### State — Azure Table Storage

**Chosen** for jobs, proposals and chat sessions. The access pattern is
point-read and single-partition scan by a key we already have (session id, user
id). Table Storage does that for cents, in the storage account that exists
anyway, with optimistic concurrency via ETag — which is precisely the primitive
the job claim needs.

- *Cosmos DB* — rejected. Serverless Cosmos would work and would give better
  querying, but it is a second data service and a second cost model for what is
  a key-value workload with fewer than a thousand rows a day.
- *PostgreSQL* — rejected. No relational query is needed anywhere, and the
  cheapest instance costs more per month than the entire rest of the demo.
- *Redis for sessions* — rejected. Sessions must survive a restart (2.3), which
  is the opposite of a cache, and Azure Cache for Redis has no free tier.

### File transfer — user-delegation SAS, direct browser to blob

**Chosen** so a 50 MB PDF never traverses the API container. The API mints a
write-only SAS scoped to one blob in `staging/` for twenty minutes, signed with
a user-delegation key derived from the app's managed identity — so the SAS is
revocable by revoking the identity's role, unlike an account-key SAS. The worker
promotes staging to `documents/` with a server-side copy, which never moves
bytes through our compute at all.

- *Multipart POST through the API* — rejected. It puts a 50 MB body through a
  0.5 vCPU container, needs request-size limits raised, and doubles the egress.
- *Account-key SAS* — rejected. It requires the account key in the app and
  cannot be revoked without rotating the key for everything.
- *Azure Blob Upload SDK in the browser with an OBO token* — rejected as more
  moving parts than a scoped, short-lived SAS.

### Observability — Log Analytics + Application Insights

**Chosen**: Container Apps streams stdout to a Log Analytics workspace with a
daily ingestion cap, so a runaway log loop cannot eat the credit. The worker
emits one structured line per indexer run — status, processed, failed, first
errors — which is what 1.9's alert is written against ("no successful run in 30
minutes", "itemsFailed > 0"). Job progress is additionally in the job row, which
is the surface the person in the chat actually sees.

- *Azure Monitor alerts as the only reporting* — rejected as insufficient: the
  requirement is that a person sees what failed, not that an ops team gets a
  page.
- *A third-party APM* — rejected: another vendor, another key, no free tier.
- *Diagnostic settings on every resource* — deliberately not enabled by default.
  It is the single easiest way to spend a free credit on logs nobody reads; the
  seam is one `azurerm_monitor_diagnostic_setting` per resource.

### Front-end hosting — served by the API container

**Chosen**: three static files (`index.html`, `styles.css`, `app.js`) mounted by
FastAPI. Same origin as the API, so one authentication boundary, no CORS, no
build step, no bundle in which a secret could hide, and no extra resource.

- *Azure Static Web Apps (free tier)* — rejected despite being free and
  genuinely good. It adds a second origin, which means either a linked-backend
  configuration or CORS plus a second auth setup, to serve 20 KB of static
  assets that the container is already serving for nothing.
- *Azure CDN / Front Door* — rejected. Caching and WAF are the right answer at
  scale and pointless for a single-tenant internal tool with three files.
- *A React/Vite SPA* — rejected. A build step, a lockfile and a bundle to audit,
  to render a message list and one confirmation card.

### Identity — Entra ID via Container Apps built-in authentication

**Chosen** because the requirement is "users sign in" and the platform can do it
before a request reaches application code. Unauthenticated browsers are
redirected; the application receives a validated principal in
`X-MS-CLIENT-PRINCIPAL` and never handles a token. Authorisation is ours: the
`FileAdmin` app role, declared in Terraform and assigned to the deployer
automatically, is what permits mutation.

- *MSAL in the browser* — rejected. Token acquisition, refresh and storage in
  client code, all of it auditable surface, to reach the same place.
- *Entra External ID / B2C* — rejected. This is an internal tool for one tenant.
- *No sign-in with a network restriction instead* — rejected on 2.8, and it
  would leave no principal to attribute a delete to.

---

## 3.5 Cost

All figures are West Europe / Sweden Central list prices at the time of writing,
in USD per month unless stated. Assumed average PDF: 20 pages, ~9,000 words,
~12,000 tokens of extracted text, ~1.5 MB.

### Indexing 1,000 PDFs once (requirement 1.10)

- Embeddings: 12,000 tokens per PDF, plus 25% for chunk overlap, is 15,000
  billable tokens. 1,000 PDFs is 15M tokens. At $0.02 per million for
  `text-embedding-3-small`: **$0.30**.
- Document cracking and chunking in the indexer: **$0.00**. Only skills that call
  a billed service cost anything, and neither `SplitSkill` nor PDF text
  extraction does.
- Blob storage for 1.5 GB, hot LRS: **$0.03/month**.
- Compute to trigger it: inside the Container Apps free grant, **$0.00**.
- **Total: about $0.35**, of which 86% is embeddings.
- Index footprint: ~30,000 chunks × (6.1 KB vector + ~2 KB text and metadata) ≈
  **300 MB**, which does not fit the free tier's 50 MB. 1,000 PDFs means Basic.

### At 5,000 PDFs and 200 chat sessions a day

Assume 5 turns per session, 6 retrieved chunks per answer, ~3,500 input and ~350
output tokens per turn.

- Azure AI Search Basic, 1 replica, 1 partition: **$75**. This is the floor and
  two thirds of the bill.
- Chat: 1,000 turns/day → 3.5M input and 0.35M output tokens/day. At $0.15 and
  $0.60 per million: $0.74/day → **$22**.
- Query-time embeddings: 1,000 queries/day × ~20 tokens: **under $0.01**.
- Steady-state indexing: assume 2% of the library churns monthly, 100 PDFs →
  1.5M tokens → **$0.03**.
- Container Apps: API scaling 0→1 on bursty traffic plus a worker that wakes a
  few times an hour. Roughly 350k vCPU-seconds and 700k GiB-seconds a month
  against a free grant of 180k and 360k: **$5-8**.
- Blob storage 7.5 GB hot + transactions: **$0.70**.
- Container Registry Basic: **$5**.
- Event Grid: ~5,000 operations against 100,000 free: **$0.00**.
- Log Analytics at a realistic 50 MB/day: **$4**.
- Table Storage and queues: **under $0.50**.
- **Total: about $115/month**, plus a one-off $1.50 to embed the initial 5,000
  PDFs.

Set `search_sku=free` and the same workload runs for **$18/month** — until the
index passes 50 MB, which for 5,000 PDFs it does immediately. The free tier is
for the demo, not for this.

### At a hundred times that: 500,000 PDFs, 20,000 sessions a day

- Search: 15M chunks ≈ 150 GB of index. On Standard S2 (100 GB per partition,
  $1,000 per search unit) that is 2 partitions × 2 replicas = 4 units:
  **$4,000**. S1 with 6 partitions × 2 replicas is $3,000 and slower. Call it
  **$3,000-4,000** and note that this is now 70-80% of the bill.
- Chat: 100,000 turns/day → **$2,200**.
- Container Apps with min_replicas 2-3 on the API and a permanently warm worker:
  **$120**.
- Blob storage 750 GB: **$14**.
- Log Analytics, uncapped, at this request volume: **$50-150**.
- Initial embedding of 500,000 PDFs: 7.5B tokens → **$150 one-off**.
- **Total: roughly $5,500/month**, dominated by search capacity, then chat.

### What breaks first, in order

1. **Azure OpenAI embedding quota, during backfill.** At the default 30k TPM,
   7.5B tokens is 250,000 minutes — 173 days. The indexer will sit in a 429
   retry loop and the run will look "healthy but slow", which is the worst
   failure mode there is. This breaks long before storage or query capacity, and
   it breaks at far less than a million documents: even 50,000 PDFs is a
   21-hour backfill at default quota. Fixes: raise the deployment to 1-2M TPM,
   split the backfill across several indexers over prefix-filtered data sources,
   and watch `itemsProcessed` per run rather than the run's status.
2. **Single-indexer enumeration.** Change detection re-lists the container each
   run. Listing 500,000 blobs every five minutes is minutes of work to discover
   that nothing changed. Fix: drop the schedule to hourly as the safety net,
   lean on the event path for freshness, and partition the container by prefix
   with one indexer per prefix.
3. **The 24-hour indexer run cap** on Standard tiers, which a single backfill of
   this size will exceed. Blob indexers resume from their high-water mark on the
   next run, so this degrades rather than fails — but it means a backfill is a
   multi-day operation to be planned, not a button someone presses.
4. **Log Analytics ingestion** becomes the third-largest line item and the
   easiest to halve, by sampling request logs and keeping only the structured
   indexer reports.
5. **The chunk-sweep query** in the delete path pages with `$skip`, which Azure
   AI Search caps at 100,000. No single document produces that many chunks
   (100,000 chunks is a ~25,000-page PDF), so this is a theoretical ceiling, but
   the fix is a search-after/range scan on `chunk_id` and it is noted in
   `LIMITATIONS.md`.
6. **Everything else** — storage queues (2,000 messages/second), Table Storage
   (2,000 entities/second per partition), Event Grid — has one to three orders of
   magnitude of headroom at this scale and is not worth pre-optimising.
