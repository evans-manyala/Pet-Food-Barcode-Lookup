# 🐾 Pet Food Barcode Lookup

A production-ready Python CLI tool that accepts a pet food barcode, validates it,
searches the web for product details and **Hong Kong retailer pricing**, then
caches results in **Redis** and stores them permanently in **Pinecone**.

---

## Architecture

```
User Input (barcode)
       │
       ▼
┌─────────────────┐
│ Barcode Validator│  EAN-13 / UPC-A / EAN-8 + GS-1 check digit
└────────┬────────┘
         │ valid
         ▼
┌─────────────────┐   HIT ──────────────────────────────────────►  Display
│   Redis Cache   │
└────────┬────────┘
         │ MISS
         ▼
┌─────────────────┐   HIT ──────────────────────────────────────►  Display
│ Pinecone Vector │                                                + re-cache
│   Database      │                                                  in Redis
└────────┬────────┘
         │ MISS
         ▼
┌─────────────────────────────────────────────────────────────┐
│              Perplexity AI  (sonar-pro model)                │
│                                                              │
│  Phase 1 – Product details from manufacturer website         │
│    · Product name, brand, target animal (dog/cat)            │
│    · Product image URL                                       │
│    · Guaranteed / nutritional analysis                       │
│                                                              │
│  Phase 2 – HK retailer search (2–5 stores, prices in HKD)   │
│    · Retailer name + direct product URL                      │
│    · Price in Hong Kong Dollars (HK$)                        │
│    · In-stock status                                         │
└──────────────────────────────┬──────────────────────────────┘
                               │
                   ┌───────────┴───────────┐
                   ▼                       ▼
            Save to Redis           Upsert to Pinecone
            (TTL: 24 h)          (permanent, vector search)
```

---

## Prerequisites

| Dependency | Purpose | Sign-up |
|---|---|---|
| **Perplexity AI** (`sonar-pro`) | Live web search + LLM | [perplexity.ai](https://www.perplexity.ai/) |
| **OpenAI** (`text-embedding-3-small`) | Embeddings for Pinecone | [platform.openai.com](https://platform.openai.com/) |
| **Redis** ≥ 6 | Fast TTL cache | Local or [Redis Cloud](https://redis.com/try-free/) |
| **Pinecone** | Permanent vector storage | [pinecone.io](https://www.pinecone.io/) |
| Python ≥ 3.11 | Runtime | — |

---

## Installation

```bash
# 1. Clone / download the project
cd pet_barcode_lookup

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate       # Windows: .venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure environment variables
cp .env.example .env
# Edit .env and fill in your API keys (see Configuration section)
```

---

## Configuration (`.env`)

```dotenv
# Perplexity AI – live web search LLM
PERPLEXITY_API_KEY=pplx-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# OpenAI – embeddings for Pinecone
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# Redis
REDIS_URL=redis://localhost:6379/0
REDIS_TTL=86400                  # 24 hours (seconds)

# Pinecone
PINECONE_API_KEY=pcsk_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
PINECONE_INDEX_NAME=pet-food-products
PINECONE_DIMENSION=1536
```

> **Redis quick-start (local):**
> ```bash
> docker run -d -p 6379:6379 redis:7-alpine
> ```

---

## Usage

### Interactive mode (recommended)

```bash
python main.py
```

```
╭──────────────────────────────────────╮
│  🐾  Pet Food Barcode Lookup          │
│  Powered by Perplexity AI · Redis · Pinecone │
╰──────────────────────────────────────╯

Type a barcode and press Enter. Type quit or press Ctrl-C to exit.

Barcode ▶ 0023100031105
```

### Single-barcode mode

```bash
python main.py --barcode 0023100031105
```

### Force a fresh web search (bypass cache)

```bash
python main.py --barcode 0023100031105 --force-refresh
```

---

## Supported Barcode Formats

| Format | Length | Example |
|---|---|---|
| **EAN-13** | 13 digits | `0023100031105` |
| **UPC-A** | 12 digits | `023100031105` |
| **EAN-8** | 8 digits | `01234565` |

Validation uses the **GS-1 check digit algorithm** – a mis-keyed barcode is
rejected immediately before any network calls are made.

---

## Hong Kong Retailers Searched

Perplexity searches across (but is not limited to):

- petstation.com.hk
- petsworld.com.hk
- pawsmore.com.hk
- petboo.com.hk
- hktvmall.com / hktv.com.hk
- ipetdog.com
- mrpetshk.com
- petfashion.com.hk
- petscorner.com.hk
- petshop168.com
- pet-city.com.hk
- goopets.com
- petoo.com.hk

All prices are returned in **Hong Kong Dollars (HK$)**. A minimum of 2 and a
maximum of 5 retailers are shown.

---

## Sample Output

```
────────────────────────── Result (source: 🌐 Live web search) ──────────────

╭──────────────────── 🏷  Product Information ───────────────────╮
│  Barcode          0023100031105                                 │
│  Product Name     Hill's Science Diet Adult Large Breed        │
│  Brand            Hill's Pet Nutrition                         │
│  Target Animal    Dog                                          │
│  Manufacturer URL https://www.hillspet.com/…                  │
│  Image URL        https://www.hillspet.com/…/product.jpg      │
╰────────────────────────────────────────────────────────────────╯

╭─────────── 🧪  Nutritional / Guaranteed Analysis ─────────────╮
│  Crude Protein (min)   23.5%                                   │
│  Crude Fat (min)       12.5%                                   │
│  Crude Fiber (max)      4.0%                                   │
│  Moisture (max)        10.0%                                   │
│  Calories              339 kcal/cup                            │
╰────────────────────────────────────────────────────────────────╯

╭──────────────────── 🛒  Hong Kong Online Retailers ────────────╮
│  #  Retailer            Price (HKD)  Stock  URL                │
│  1  PetStation          HK$349.00      ✔    https://…         │
│  2  HKTVmall            HK$329.00      ✔    https://…         │
│  3  PetBoo              HK$355.00      ?    https://…         │
╰────────────────────────────────────────────────────────────────╯
```

---

## Project Structure

```
pet_barcode_lookup/
├── main.py                  # CLI entry point
├── requirements.txt
├── .env.example
└── src/
    ├── __init__.py
    ├── config.py            # Pydantic-settings configuration
    ├── models.py            # ProductInfo, NutritionalInfo, RetailerListing
    ├── barcode_validator.py # EAN-13/UPC-A/EAN-8 validation + GS-1 checksum
    ├── llm_searcher.py      # Perplexity API – two-phase web search
    ├── redis_cache.py       # Redis GET/SET with TTL
    └── pinecone_store.py    # Pinecone upsert + exact-ID + semantic search
```

---

## Cache & Storage Strategy

| Layer | Tool | Key | TTL | Purpose |
|---|---|---|---|---|
| L1 | Redis | `pet_food:<barcode>` | 24 h | Sub-millisecond retrieval |
| L2 | Pinecone | Vector ID = barcode | ∞ | Permanent store, survives Redis expiry |
| L3 | Perplexity | — | — | Live fallback when both caches miss |

On a **Pinecone hit**, the result is automatically re-cached in Redis so
subsequent lookups are fast again.

---

## Troubleshooting

| Problem | Solution |
|---|---|
| `PERPLEXITY_API_KEY not set` | Add key to `.env` |
| `Redis unavailable` | Start Redis: `docker run -d -p 6379:6379 redis:7-alpine` |
| `Pinecone initialisation failed` | Check `PINECONE_API_KEY` and network |
| Barcode rejected | Verify digit count (8/12/13) and check digit |
| `No HK retailers found` | Product may not be sold in HK; try `--force-refresh` |

---

## License

MIT – feel free to adapt for your own use.
