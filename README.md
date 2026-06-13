# Pet Food Barcode Lookup

Look up Hong Kong pet food products by barcode: nutrition, verified identity, and **HK$ pricing** from local retailers. Results are cached in **Redis**, stored in **Pinecone**, and enriched from a **local HK retailer catalog** and curated **barcode overrides**.

**Production:** `http://34.133.118.0` · **Domain (HTTPS pending):** `https://api.mindmycat.com`

---

## Features

- **Barcode validation** — EAN-13, UPC-A, EAN-8 with GS-1 check digit
- **Three-layer cache** — Redis (24 h) → Pinecone (permanent) → Gemini live search
- **HK catalog** — SQLite DB from scraped retailer CSVs (HKTVmall, Shopify stores, master scrape)
- **Barcode overrides** — Curated fixes in `data/barcode_overrides.json` for collision / wrong-product cases
- **REST API** — `GET`/`POST` `/api/lookup` with JSON responses
- **Web UI** — Browser lookup at `/` and ops dashboard at `/dashboard`
- **CI/CD** — GitHub Actions deploy to GCP VM (see [deploy/CICD.md](deploy/CICD.md))

---

## Architecture

```
Barcode input
     │
     ▼
┌─────────────┐
│  Validator  │  GS-1 check digit
└──────┬──────┘
       ▼
┌─────────────┐  HIT ──► return (source: redis)
│    Redis    │
└──────┬──────┘
       │ MISS
       ▼
┌─────────────┐  HIT ──► return (source: pinecone) + re-cache Redis
│  Pinecone   │
└──────┬──────┘
       │ MISS
       ▼
┌─────────────────────────────────────────────┐
│  Live search (Gemini + SerpAPI)              │
│  · Local HK catalog + barcode overrides      │
│  · Product identity, image, nutrition        │
│  · HK retailer URLs and HK$ prices           │
└──────────────────┬──────────────────────────┘
                   ▼
         Save Redis + upsert Pinecone
         (source: live_search)
```

---

## Quick start (local)

### Docker (recommended)

```bash
git clone git@github.com:evans-manyala/Pet-Food-Barcode-Lookup.git
cd Pet-Food-Barcode-Lookup

cp deploy/env.production.example .env
# Edit .env — at minimum: GOOGLE_CLOUD_PROJECT, OPENROUTER_API_KEY, SERPAPI_API_KEY, PINECONE_API_KEY

docker compose -f docker-compose.yml -f docker-compose.direct.yml up -d --build
```

Open **http://localhost:80/** (or set `APP_PORT=8000` in `.env` and use port 8000).

### Native Python

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp deploy/env.production.example .env
# Start Redis: docker run -d -p 6379:6379 redis:7-alpine
uvicorn api.app:app --reload --host 0.0.0.0 --port 8000
```

---

## API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/api/health` | Liveness check |
| `GET` | `/api/lookup?barcode=&force_refresh=` | Look up by barcode |
| `POST` | `/api/lookup` | JSON body: `{ "barcode", "force_refresh" }` |
| `GET` | `/api/stats?hours=&token=` | Dashboard metrics |
| `GET` | `/docs` | Swagger UI (OpenAPI) |
| `GET` | `/` | Web lookup UI |
| `GET` | `/dashboard` | Ops dashboard |

**Full reference:** [docs/API.md](docs/API.md)

**Postman:** Import [postman/Pet-Food-Barcode-Lookup.postman_collection.json](postman/Pet-Food-Barcode-Lookup.postman_collection.json) and an environment from [postman/environments/](postman/environments/). See [postman/README.md](postman/README.md).

### Example

```bash
curl -s "http://localhost:8000/api/health"
curl -s "http://localhost:8000/api/lookup?barcode=9003579008331"
```

Successful lookup response:

```json
{
  "success": true,
  "data": {
    "barcode": "9003579008331",
    "product_name": "Royal Canin Medium Puppy Dry Dog Food (Medium Junior)",
    "brand": "Royal Canin",
    "source": "redis",
    "price_comparison": [{ "store": "PetShack", "price_display": "HK$272.00", "url": "..." }],
    "identity_confidence": "high"
  }
}
```

---

## CLI

```bash
python main.py                              # interactive
python main.py --barcode 9003579008331
python main.py --barcode 9003579008331 --force-refresh
```

---

## Configuration

Copy `deploy/env.production.example` to `.env`. Key variables:

| Variable | Purpose |
|----------|---------|
| `GOOGLE_CLOUD_PROJECT` | Vertex AI / Gemini |
| `OPENROUTER_API_KEY` | Embeddings for Pinecone |
| `SERPAPI_API_KEY` | Identity, images, HK retailer search |
| `PINECONE_API_KEY` | Vector storage |
| `REDIS_URL` | Cache (`redis://redis:6379/0` in Docker) |
| `HK_CATALOG_DB_PATH` | Local SQLite catalog |
| `HK_CATALOG_OVERRIDES_PATH` | Curated barcode fixes |
| `STATS_TOKEN` | Optional — protect `/api/stats` |

Import HK catalog (optional, from scraped CSVs):

```bash
python scripts/import_hk_catalog.py
```

---

## Deployment

| Guide | Use case |
|-------|----------|
| [deploy/DEPLOY.md](deploy/DEPLOY.md) | First-time GCP VM setup |
| [deploy/CICD.md](deploy/CICD.md) | Domain + GitHub Actions CI/CD |

Production deploys run automatically on push to `main`.

---

## Project structure

```
Pet-Food-Barcode-Lookup/
├── api/app.py              # FastAPI service
├── main.py                 # CLI
├── frontend/               # Web UI + dashboard
├── postman/                # Postman collection + environments
├── docs/API.md             # API reference
├── data/
│   ├── barcode_overrides.json
│   └── hk_retailer_catalog.db   # gitignored — import locally
├── scripts/import_hk_catalog.py
├── src/
│   ├── service.py          # Redis → Pinecone → live search
│   ├── llm_searcher.py     # Gemini + SerpAPI
│   ├── catalog/            # HK retailer catalog + overrides
│   ├── redis_cache.py
│   ├── pinecone_store.py
│   └── observability.py    # /api/stats metrics
├── docker-compose.yml
├── docker-compose.direct.yml   # public port (demo)
├── docker-compose.prod.yml     # nginx: localhost:8000 only
└── deploy/
```

---

## Demo barcodes

| Barcode | Product |
|---------|---------|
| `9003579008331` | Royal Canin Medium Puppy dry |
| `4894514083033` | VitaPet Supreme tuna & whitebait |
| `45070778` | Purina Mon Petit |
| `0023100031105` | Hill's Science Diet (generic demo) |

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| `503` Google credentials | Run `gcloud auth application-default login` locally; on GCP VM use service account |
| Redis unavailable | `docker compose ps` — wait for redis healthy |
| Slow lookup | Live search takes 30–90 s; use cache or avoid `force_refresh` |
| Invalid barcode | Check digit / length (8, 12, or 13 digits) |

---

## License

MIT
