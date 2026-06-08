# Domain + CI/CD — `https://api.mindmycat.com`

Continue here **after** completing [DEPLOY.md](./DEPLOY.md) Steps 1–10 using **Path B** (`APP_PORT=8000`).

**Your values:**

| Setting | Value |
|---------|-------|
| Domain | `api.mindmycat.com` |
| VM | `pet-food-lookup` (zone `us-central1-a`) |
| GitHub repo | `evans-manyala/Pet-Food-Barcode-Lookup` |
| App path on VM | `~/pet-food-barcode-lookup` |

---

## Architecture

```
GitHub push (main)
  → GitHub Actions (SSH)
    → VM: git pull + docker compose up --build
      → nginx :443 → app :8000 (localhost)
      → Redis (Docker)
```

---

## Step 11. DNS — point domain to the VM

### 11a. Get VM IP (on your Mac)

```bash
gcloud compute instances describe pet-food-lookup \
  --zone=us-central1-a \
  --project=project-11d80abc-a7c0-43df-9ed \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

### 11b. Add DNS record (at your registrar / Cloudflare)

| Type | Name | Value | TTL |
|------|------|-------|-----|
| **A** | `api` | `<VM_IP from 11a>` | 300 |

### 11c. Wait for propagation, then verify (on your Mac)

```bash
dig +short api.mindmycat.com
```

Must return your VM IP before continuing.

---

## Step 12. HTTPS — nginx + Let's Encrypt (on the VM)

SSH in:

```bash
gcloud compute ssh pet-food-lookup --zone=us-central1-a --project=project-11d80abc-a7c0-43df-9ed
```

Run domain setup (replace email with yours):

```bash
cd ~/pet-food-barcode-lookup
DOMAIN=api.mindmycat.com SSL_EMAIL=you@mindmycat.com bash deploy/setup-domain.sh
```

This will:
- Install nginx + certbot
- Proxy `api.mindmycat.com` → `127.0.0.1:8000`
- Request a TLS certificate
- Move Docker app to localhost-only (production compose)

### 12b. Open GCP firewall for HTTPS (on your Mac)

```bash
gcloud compute firewall-rules create allow-pet-food-https \
  --project=project-11d80abc-a7c0-43df-9ed \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:443 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=http-server
```

> If the rule already exists, skip this step.

### 12c. Verify HTTPS

```bash
curl https://api.mindmycat.com/api/health
```

Expected: `{"status":"ok","service":"pet-food-barcode-lookup"}`

Open in browser: **https://api.mindmycat.com/**

---

## Step 13. SSH deploy key for GitHub Actions (on your Mac)

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/pet-food-deploy -N ""
cat ~/.ssh/pet-food-deploy.pub
```

Copy the public key output.

### 13b. Add public key to the VM

```bash
gcloud compute ssh pet-food-lookup --zone=us-central1-a --project=project-11d80abc-a7c0-43df-9ed
```

On the VM:

```bash
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo "PASTE_PUBLIC_KEY_HERE" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
exit
```

### 13c. Find your VM username (on your Mac)

```bash
gcloud compute ssh pet-food-lookup --zone=us-central1-a --dry-run 2>&1 | grep "ssh"
```

Note the username (often your Mac username, e.g. `evansmanyala`). This is `VM_USER`.

### 13d. Test SSH with the deploy key (on your Mac)

```bash
ssh -i ~/.ssh/pet-food-deploy <VM_USER>@<VM_IP>
```

If that connects, GitHub Actions will work.

---

## Step 14. GitHub repository secrets

GitHub → **evans-manyala/Pet-Food-Barcode-Lookup** → **Settings** → **Secrets and variables** → **Actions**

Add these secrets:

| Secret | Value |
|--------|-------|
| `VM_HOST` | `api.mindmycat.com` |
| `VM_USER` | your VM username from Step 13c |
| `VM_SSH_KEY` | entire contents of `~/.ssh/pet-food-deploy` (private key) |
| `VM_APP_DIR` | `/home/<VM_USER>/pet-food-barcode-lookup` |
| `USE_NGINX` | `true` |

To copy the private key:

```bash
cat ~/.ssh/pet-food-deploy
```

Paste the **full** file including `-----BEGIN` / `-----END` lines into `VM_SSH_KEY`.

---

## Step 15. Push code to trigger first CI/CD deploy (on your Mac)

Ensure the workflow file is on `main`:

```bash
cd ~/Pet-Food-Barcode-Lookup   # your local repo
git add .github/workflows/deploy.yml deploy/
git commit -m "Add CI/CD deploy workflow"
git push origin main
```

Watch progress: GitHub → **Actions** → **Deploy to GCP VM**

### 15b. Manual deploy (alternative)

On the VM anytime:

```bash
bash ~/pet-food-barcode-lookup/deploy/remote-deploy.sh
```

Or from GitHub: **Actions** → **Deploy to GCP VM** → **Run workflow**.

---

## Step 16. Share with testers

| URL | Purpose |
|-----|---------|
| `https://api.mindmycat.com/` | Main UI |
| `https://api.mindmycat.com/?barcode=9003579008331` | Pre-filled demo |
| `https://api.mindmycat.com/api/health` | API health check |

---

## Full checklist

Copy and tick off as you go:

```
Part 1 — GCP (Mac)
[ ] Step 1  — gcloud config set project
[ ] Step 2  — Enable Vertex AI API
[ ] Step 3  — Service account created
[ ] Step 4  — roles/aiplatform.user granted
[ ] Step 5  — VM created
[ ] Step 6  — Firewall port 80
[ ] Step 7  — VM IP saved

Part 2 — VM first deploy
[ ] Step 8  — SSH into VM
[ ] Step 9  — Repo cloned (deploy key if private)
[ ] Step 10 — .env configured

Part 3 — App running
[ ] Path B   — setup-vm.sh with APP_PORT=8000
[ ]          — curl localhost:8000/api/health OK

Part 4 — Domain + CI/CD (this file)
[ ] Step 11 — DNS A record api → VM IP
[ ] Step 12 — setup-domain.sh + firewall 443
[ ]          — https://api.mindmycat.com/api/health OK
[ ] Step 13 — GitHub Actions SSH key on VM
[ ] Step 14 — GitHub secrets set
[ ] Step 15 — git push → Actions deploy green
[ ] Step 16 — Share URL with testers
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| GitHub Actions SSH fails | Check `VM_HOST`, `VM_USER`, `VM_SSH_KEY`; test with `ssh -i ~/.ssh/pet-food-deploy USER@HOST` |
| `git pull` fails in Actions | VM needs deploy key (DEPLOY.md Step 9) — CI/CD only pulls, doesn't clone |
| Certbot fails | DNS must resolve to VM IP first (`dig +short api.mindmycat.com`) |
| 502 Bad Gateway | `sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml ps` — app must be up on `127.0.0.1:8000` |
| Port 80 conflict | `sudo docker compose down` then re-run `setup-domain.sh` |
| Lookup timeout in browser | Normal — live search takes 30–90s; nginx timeout is 300s |
| Cert renewal | certbot installs a cron job automatically; check `sudo certbot renew --dry-run` |

---

## What happens on every `git push main`

1. GitHub Actions connects to VM via SSH
2. Runs `deploy/remote-deploy.sh`:
   - `git pull origin main`
   - `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`
   - Health check on `localhost:8000`

No downtime config changes needed — Docker replaces the app container in place.
