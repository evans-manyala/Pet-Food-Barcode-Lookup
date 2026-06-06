# Deploy Pet Food Barcode Lookup (GCP VM or EC2)

Public demo URL pattern: `http://YOUR_PUBLIC_IP/` — share with testers for UI feedback.

---

## What gets deployed

| Component | Role |
|-----------|------|
| **FastAPI + frontend** | Web UI on port 80 (configurable) |
| **Redis** | Local cache (Docker) |
| **Pinecone** | Permanent vector store (cloud) |
| **Vertex AI / Gemini** | Live product search |

---

## Prerequisites

1. **GCP project** with [Vertex AI API](https://console.cloud.google.com/apis/library/aiplatform.googleapis.com) enabled
2. API keys in `.env`: OpenRouter, Pinecone, SerpAPI (see `deploy/env.production.example`)
3. **Service account** with role **Vertex AI User** (`roles/aiplatform.user`)

---

## Option A — GCP Compute Engine (recommended)

Vertex AI runs in the same cloud; use a VM **service account** (no JSON key needed).

### 1. Create the VM

```bash
gcloud compute instances create pet-food-lookup \
  --project=YOUR_PROJECT_ID \
  --zone=us-central1-a \
  --machine-type=e2-small \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB \
  --tags=http-server \
  --service-account=YOUR_SA@YOUR_PROJECT_ID.iam.gserviceaccount.com \
  --scopes=https://www.googleapis.com/auth/cloud-platform
```

### 2. Open firewall

```bash
gcloud compute firewall-rules create allow-pet-food-http \
  --project=YOUR_PROJECT_ID \
  --direction=INGRESS \
  --priority=1000 \
  --network=default \
  --action=ALLOW \
  --rules=tcp:80 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=http-server
```

### 3. SSH and deploy

```bash
gcloud compute ssh pet-food-lookup --zone=us-central1-a

# On the VM:
git clone https://github.com/YOUR_USER/Pet-Food-Barcode-Lookup.git ~/pet-food-barcode-lookup
cd ~/pet-food-barcode-lookup
cp deploy/env.production.example .env
nano .env   # fill in API keys + GOOGLE_CLOUD_PROJECT

PLATFORM=gcp APP_DIR=$HOME/pet-food-barcode-lookup bash deploy/setup-vm.sh
```

### 4. Share with testers

```bash
gcloud compute instances describe pet-food-lookup --zone=us-central1-a \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

Open `http://NAT_IP/` in a browser.

---

## Option B — AWS EC2

EC2 cannot use GCP metadata auth — you need a **service account JSON key**.

### 1. Create GCP service account key

GCP Console → IAM → Service Accounts → your SA → Keys → Add key → JSON.

Save as `deploy/gcp-sa-key.json` on the server (never commit this file).

### 2. Launch EC2

- **AMI:** Ubuntu 22.04 LTS
- **Type:** `t3.small` (or larger for faster lookups)
- **Storage:** 20 GB
- **Security group inbound:** TCP 22 (SSH), TCP 80 (HTTP) from `0.0.0.0/0`

### 3. SSH and deploy

```bash
ssh -i your-key.pem ubuntu@EC2_PUBLIC_IP

git clone https://github.com/YOUR_USER/Pet-Food-Barcode-Lookup.git ~/pet-food-barcode-lookup
cd ~/pet-food-barcode-lookup

# Upload gcp-sa-key.json (from your laptop):
# scp -i your-key.pem deploy/gcp-sa-key.json ubuntu@EC2_PUBLIC_IP:~/pet-food-barcode-lookup/deploy/

cp deploy/env.production.example .env
nano .env

PLATFORM=ec2 APP_DIR=$HOME/pet-food-barcode-lookup bash deploy/setup-vm.sh
```

Share: `http://EC2_PUBLIC_IP/`

---

## Manual Docker commands

```bash
# GCP VM
docker compose up -d --build

# EC2
docker compose -f docker-compose.yml -f docker-compose.ec2.yml up -d --build

# Logs
docker compose logs -f app

# Restart after .env changes
docker compose up -d --build app
```

---

## Health check

```bash
curl http://localhost/api/health
# {"status":"ok","service":"pet-food-barcode-lookup"}
```

---

## Tester URLs

| URL | Purpose |
|-----|---------|
| `http://IP/` | Main UI |
| `http://IP/?barcode=9003579008331` | Pre-filled lookup |
| `http://IP/api/health` | API status |

---

## Security notes for public demos

- Do **not** commit `.env` or `gcp-sa-key.json`
- Rotate API keys after the demo period
- Consider restricting firewall source ranges to your team's IPs if not fully public
- Live lookups cost Vertex AI + SerpAPI credits — monitor usage in GCP/AWS consoles

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `DefaultCredentialsError` on EC2 | Ensure `gcp-sa-key.json` exists and use `docker-compose.ec2.yml` |
| `DefaultCredentialsError` on GCP | VM service account needs `roles/aiplatform.user` and `cloud-platform` scope |
| Port 80 in use | Set `APP_PORT=8080` in `.env` and open that port in firewall |
| Redis connection refused | `docker compose ps` — redis container should be healthy |
| Slow first lookup | Normal — live Gemini + SerpAPI search takes 30–90s |
