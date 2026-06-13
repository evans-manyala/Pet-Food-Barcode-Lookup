# Pet Food Barcode Lookup — API Reference

Base URL examples:

| Environment | Base URL |
|-------------|----------|
| Local (Docker / uvicorn) | `http://localhost:8000` |
| Production (VM IP) | `http://34.133.118.0` |
| Production (domain) | `https://api.mindmycat.com` |

Interactive OpenAPI docs (when the API is running): `{base_url}/docs` and `{base_url}/redoc`

Postman collection: [`postman/Pet-Food-Barcode-Lookup.postman_collection.json`](../postman/Pet-Food-Barcode-Lookup.postman_collection.json)

---

## Authentication

Public endpoints (`/api/health`, `/api/lookup`) require **no API key**.

`/api/stats` is open by default. Set `STATS_TOKEN` in `.env` to require `?token=...` on stats and the dashboard.

---

## Lookup pipeline

Every successful lookup returns a `source` field indicating which layer answered:

| `source` | Meaning |
|----------|---------|
| `redis` | Redis cache hit |
| `pinecone` | Pinecone vector DB hit (re-cached to Redis) |
| `live_search` | Gemini + SerpAPI live search (saved to Redis + Pinecone) |

Set `force_refresh=true` to bypass Redis and Pinecone and run a fresh live search (still uses local HK catalog + barcode overrides).

---

## Endpoints

### `GET /api/health`

Liveness check for load balancers and CI/CD.

**Response `200`**

```json
{
  "status": "ok",
  "service": "pet-food-barcode-lookup"
}
```

---

### `GET /api/lookup`

Look up a product by barcode (query parameters).

**Query parameters**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `barcode` | string | yes | — | EAN-13, UPC-A, or EAN-8 (GS-1 check digit validated) |
| `force_refresh` | boolean | no | `false` | Skip Redis/Pinecone; run live search |

**Example**

```http
GET /api/lookup?barcode=9003579008331
GET /api/lookup?barcode=9003579008331&force_refresh=true
```

**Response `200` — success**

```json
{
  "success": true,
  "data": {
    "barcode": "9003579008331",
    "product_name": "Royal Canin Medium Puppy Dry Dog Food (Medium Junior)",
    "brand": "Royal Canin",
    "target_animal": "Dog",
    "image_url": "https://www.petshack.hk/cdn/shop/files/...",
    "guaranteed_analysis": {
      "protein": 32.0,
      "fat_content": 20.0,
      "moisture": 8.0
    },
    "nutrition_display": [
      { "label": "Crude Protein (min)", "value": "32.0" }
    ],
    "price_comparison": [
      {
        "store": "PetShack",
        "retailer_name": "PetShack",
        "price": 272.0,
        "price_display": "HK$272.00",
        "currency": "HKD",
        "url": "https://www.petshack.hk/...",
        "in_stock": true,
        "region": "HK"
      }
    ],
    "best_price": { "store": "PetShack", "price_display": "HK$272.00" },
    "source_urls": ["https://www.petshack.hk/..."],
    "barcode_verified": true,
    "identity_confidence": "high",
    "barcode_evidence": "EAN 9003579008331 maps to ...",
    "warnings": [],
    "source": "redis"
  }
}
```

**Response `200` — found but not safely verified**

```json
{
  "success": false,
  "error": "Product not safely identified. No strong source evidence links this barcode to a verified product.",
  "data": { "...partial product fields..." }
}
```

**Errors**

| Status | When |
|--------|------|
| `400` | Invalid barcode format or check digit |
| `404` | No product found |
| `503` | Google Cloud credentials missing or service unavailable |

---

### `POST /api/lookup`

Same behaviour as `GET /api/lookup` with a JSON body.

**Request body**

```json
{
  "barcode": "9003579008331",
  "force_refresh": false
}
```

**Example**

```http
POST /api/lookup
Content-Type: application/json

{"barcode": "4580865350053", "force_refresh": false}
```

---

### `GET /api/stats`

Observability metrics for the monitoring dashboard. Data is aggregated from in-memory events and `data/lookup_metrics.jsonl`.

**Query parameters**

| Name | Type | Required | Default | Description |
|------|------|----------|---------|-------------|
| `hours` | float | no | `24` | Lookback window (1–168 hours) |
| `token` | string | no | — | Required when `STATS_TOKEN` is set in `.env` |

**Response `200`**

```json
{
  "summary": {
    "window_hours": 24,
    "total": 42,
    "successes": 38,
    "failures": 4,
    "success_rate": 90.5,
    "cache_hit_rate": 65.0,
    "avg_response_ms": 1200,
    "uptime_seconds": 86400
  },
  "hourly_volume": [],
  "failure_breakdown": [],
  "provider_stats": [],
  "pipeline_timing": [],
  "top_failing": [],
  "recent": [],
  "system": {
    "redis": "up",
    "pinecone": "up"
  }
}
```

**Errors**

| Status | When |
|--------|------|
| `401` | Missing or invalid `token` when `STATS_TOKEN` is configured |

---

## Web UI (not JSON API)

| Path | Description |
|------|-------------|
| `GET /` | Barcode lookup UI (`frontend/index.html`) |
| `GET /dashboard` | Ops dashboard (`frontend/dashboard.html`) — polls `/api/stats` |
| `GET /assets/*` | Static CSS/JS |

Pre-fill a barcode in the browser: `/?barcode=9003579008331`

---

## Supported barcode formats

| Format | Digits | Example |
|--------|--------|---------|
| EAN-13 | 13 | `9003579008331` |
| UPC-A | 12 | `023100031105` |
| EAN-8 | 8 | `01234565` |

---

## Demo barcodes (HK pet food)

| Barcode | Notes |
|---------|-------|
| `9003579008331` | Royal Canin Medium Puppy **dry** — override + cache |
| `4580865350053` | 長生の秘訣 cat mousse |
| `4894514083033` | VitaPet Supreme tuna & whitebait |
| `45070778` | Purina Mon Petit |
| `0023100031105` | Hill's Science Diet (common demo) |

---

## Rate & latency notes

- **Cache hit** (Redis/Pinecone): typically &lt; 500 ms
- **Live search**: 30–90 s (Gemini + SerpAPI + URL validation); production nginx timeout is 300 s
- Use `force_refresh` sparingly in integrations — it bypasses cache and incurs API cost
