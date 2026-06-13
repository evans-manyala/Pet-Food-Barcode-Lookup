# Deploy Pet Food Barcode Lookup — Step-by-Step (GCP VM)

Execute these steps **in order** on your Mac (`gcloud`) and on the VM (`ssh`).

**Your values** (fill in if different):

| Setting | Value |
|---------|-------|
| GCP project | `project-11d80abc-a7c0-43df-9ed` |
| VM name | `pet-food-lookup` |
| Zone | `us-central1-a` |
| Service account | `barcode-pet-food-lookup@project-11d80abc-a7c0-43df-9ed.iam.gserviceaccount.com` |
| GitHub repo | `git@github.com:evans-manyala/Pet-Food-Barcode-Lookup.git` |
| App path on VM | `~/pet-food-barcode-lookup` |

**Choose a path:**

| Path | URL | Guide |
|------|-----|-------|
| **A — Quick IP demo (Docker)** | `http://VM_IP/` | Steps 1–10 below, Path A |
| **B — Domain + CI/CD (Docker)** | `https://api.mindmycat.com/` | Steps 1–10 below, Path B → [CICD.md](./CICD.md) |
| **C — Native (no Docker)** | `http://VM_IP:8000/` | Steps 1–8 + [Native deploy](#option-c--native-deploy-no-docker) below |

---

## Part 1 — GCP setup (run on your Mac)

### Step 1. Set active project

```bash
gcloud config set project project-11d80abc-a7c0-43df-9ed
```

### Step 2. Enable Vertex AI API

```bash
gcloud services enable aiplatform.googleapis.com
```

### Step 3. Create service account (skip if `barcode-pet-food-lookup` already exists)

```bash
gcloud iam service-accounts create barcode-pet-food-lookup \
  --display-name="Pet Food Barcode Lookup"
```

### Step 4. Grant Vertex AI User role (CLI — use this if the Console role picker doesn't show it)

```bash
gcloud projects add-iam-policy-binding project-11d80abc-a7c0-43df-9ed \
  --member="serviceAccount:barcode-pet-food-lookup@project-11d80abc-a7c0-43df-9ed.iam.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

Verify:

```bash
gcloud projects get-iam-policy project-11d80abc-a7c0-43df-9ed \
  --flatten="bindings[].members" \
  --filter="bindings.members:barcode-pet-food-lookup@project-11d80abc-a7c0-43df-9ed.iam.gserviceaccount.com" \
  --format="table(bindings.role)"
```

Expected: `roles/aiplatform.user`

### Step 5. Create the VM (skip if `pet-food-lookup` already exists)

```bash
gcloud compute instances create pet-food-lookup \
  --project=project-11d80abc-a7c0-43df-9ed \
  --zone=us-central1-a \
  --machine-type=e2-small \
  --image-family=ubuntu-2204-lts \
  --image-project=ubuntu-os-cloud \
  --boot-disk-size=20GB \
  --tags=http-server \
  --service-account=barcode-pet-food-lookup@project-11d80abc-a7c0-43df-9ed.iam.gserviceaccount.com \
  --scopes=https://www.googleapis.com/auth/cloud-platform
```

### Step 6. Open firewall (HTTP)

```bash
gcloud compute firewall-rules create allow-pet-food-http \
  --project=project-11d80abc-a7c0-43df-9ed \
  --direction=INGRESS \
  --priority=1000 \
  --network=default \
  --action=ALLOW \
  --rules=tcp:80 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=http-server
```

> If the rule already exists, gcloud will error — that's fine, continue.

### Step 7. Get the VM public IP (save this)

```bash
gcloud compute instances describe pet-food-lookup \
  --zone=us-central1-a \
  --project=project-11d80abc-a7c0-43df-9ed \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

---

## Part 2 — First deploy on the VM

### Step 8. SSH into the VM

```bash
gcloud compute ssh pet-food-lookup \
  --zone=us-central1-a \
  --project=project-11d80abc-a7c0-43df-9ed
```

Re-connect anytime with the same command.

### Step 9. Clone the repo on the VM

**Private repo** — generate a deploy key on the VM first:

```bash
# On the VM:
ssh-keygen -t ed25519 -C "vm-deploy" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

Add that public key in GitHub → repo **Settings** → **Deploy keys** → **Add deploy key** (read-only).

Then on the VM (SSH config so `git pull` works in non-interactive CI/CD sessions):

```bash
ssh-keyscan github.com >> ~/.ssh/known_hosts 2>/dev/null
chmod 600 ~/.ssh/known_hosts

cat >> ~/.ssh/config <<'EOF'
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/github_deploy
  IdentitiesOnly yes
EOF
chmod 600 ~/.ssh/config ~/.ssh/github_deploy

ssh -T git@github.com   # expect: "successfully authenticated" (exit code 1 is OK)

git clone git@github.com:evans-manyala/Pet-Food-Barcode-Lookup.git ~/pet-food-barcode-lookup
cd ~/pet-food-barcode-lookup
```

### Step 10. Create `.env` on the VM

```bash
cp deploy/env.production.example .env
nano .env
```

**Required values** (copy from your local `.env`):

```dotenv
GOOGLE_CLOUD_PROJECT=project-11d80abc-a7c0-43df-9ed
GOOGLE_CLOUD_LOCATION=us-central1
GEMINI_MODEL=gemini-2.5-flash

OPENROUTER_API_KEY=...
SERPAPI_API_KEY=...
PINECONE_API_KEY=...
PINECONE_INDEX_NAME=pet-food-products
PINECONE_NAMESPACE=pet-food
```

**Optional — copy from Mac instead of typing:**

```bash
# Run on your Mac (not the VM):
gcloud compute scp .env pet-food-lookup:~/pet-food-barcode-lookup/.env \
  --zone=us-central1-a --project=project-11d80abc-a7c0-43df-9ed
```

---

## Part 3 — Start the app

### Path A — Quick IP demo (`http://VM_IP/`)

On the VM:

```bash
cd ~/pet-food-barcode-lookup
PLATFORM=gcp APP_DIR=$HOME/pet-food-barcode-lookup bash deploy/setup-vm.sh
```

After it finishes, open in a browser:

```
http://<VM_IP>/
http://<VM_IP>/?barcode=9003579008331
http://<VM_IP>/api/health
```

**Verify on the VM:**

```bash
curl http://localhost/api/health
sudo docker compose ps
sudo docker compose logs -f app   # Ctrl+C to exit
```

✅ **Done for Path A.** Share the IP URL with testers.

**API docs for testers:** [docs/API.md](../docs/API.md) · **Postman:** import [postman/Pet-Food-Barcode-Lookup.postman_collection.json](../postman/Pet-Food-Barcode-Lookup.postman_collection.json) and [postman/environments/Production.postman_environment.json](../postman/environments/Production.postman_environment.json) (set `base_url` to `http://<VM_IP>`).

---

### Path B — Domain + HTTPS (continue to CICD.md)

On the VM, install Docker but bind the app on port **8000** (nginx will take port 80 later):

```bash
cd ~/pet-food-barcode-lookup
PLATFORM=gcp APP_PORT=8000 APP_DIR=$HOME/pet-food-barcode-lookup bash deploy/setup-vm.sh
```

Smoke-test before nginx:

```bash
curl http://localhost:8000/api/health
```

Then continue with **[CICD.md](./CICD.md)** — DNS, SSL, GitHub Actions.

---

## Option C — Native deploy (no Docker)

Simpler stack: **Python venv + Redis (apt) + systemd**. No Docker install or compose files.

**Advantages:** easier logs (`journalctl`), Vertex AI auth works directly via VM service account, fewer moving parts.

**Trade-off:** app runs on port **8000** by default (use nginx script for port 80/HTTPS).

### C1. Stop Docker if it was running before

```bash
cd ~/pet-food-barcode-lookup
sudo docker compose down 2>/dev/null || true
```

### C2. Clone repo + `.env` (same as Steps 9–10)

```bash
git clone git@github.com:evans-manyala/Pet-Food-Barcode-Lookup.git ~/pet-food-barcode-lookup
cd ~/pet-food-barcode-lookup
cp deploy/env.production.example .env
nano .env
```

Ensure `.env` includes:

```dotenv
GOOGLE_CLOUD_PROJECT=project-11d80abc-a7c0-43df-9ed
REDIS_URL=redis://localhost:6379/0
API_HOST=0.0.0.0
API_PORT=8000
```

### C3. One-command native setup

```bash
bash deploy/setup-native.sh
```

This installs Redis, creates `.venv`, installs pip deps, and starts a `systemd` service.

### C4. Open firewall for port 8000 (on your Mac, if not already)

```bash
gcloud compute firewall-rules create allow-pet-food-8000 \
  --project=project-11d80abc-a7c0-43df-9ed \
  --direction=INGRESS --action=ALLOW --rules=tcp:8000 \
  --source-ranges=0.0.0.0/0 --target-tags=http-server
```

### C5. Test

```bash
curl http://localhost:8000/api/health
```

Browser: `http://<VM_IP>:8000/`

### C6. View logs (no Docker)

```bash
sudo journalctl -u pet-food-lookup -f          # live logs
sudo journalctl -u pet-food-lookup -n 100        # last 100 lines
sudo systemctl status pet-food-lookup
sudo systemctl restart pet-food-lookup           # after .env change
```

### C7. Optional — nginx + HTTPS on `api.mindmycat.com`

```bash
DOMAIN=api.mindmycat.com SSL_EMAIL=you@mindmycat.com bash deploy/setup-native-nginx.sh
```

---

## Useful VM commands

```bash
# Reconnect
gcloud compute ssh pet-food-lookup --zone=us-central1-a --project=project-11d80abc-a7c0-43df-9ed

# Restart app after .env change
cd ~/pet-food-barcode-lookup
sudo docker compose up -d --build app

# View logs
sudo docker compose logs -f app

# Stop everything
sudo docker compose down
```

---

## Manual Docker reference

```bash
cd ~/pet-food-barcode-lookup

# IP demo (port 80)
sudo docker compose up -d --build

# Behind nginx (port 8000 localhost only)
sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| `DefaultCredentialsError` | VM service account needs `roles/aiplatform.user` + `cloud-platform` scope (Steps 4–5) |
| `Permission denied` on `docker` | Run `newgrp docker` or log out/in; or prefix with `sudo` |
| `git clone` fails (private repo) | Add VM deploy key to GitHub (Step 9) |
| Port 80 already in use | `sudo docker compose down`; or use Path B with `APP_PORT=8000` |
| Redis unhealthy | `sudo docker compose ps` — wait for redis healthcheck |
| Slow lookup | Normal for live search — 30–90 seconds |
| Firewall rule exists | Skip Step 6 — rule is already there |

---

## Security notes

- Never commit `.env` or `gcp-sa-key.json`
- Rotate API keys after the demo period
- Live lookups bill Vertex AI + SerpAPI — monitor usage in GCP console
