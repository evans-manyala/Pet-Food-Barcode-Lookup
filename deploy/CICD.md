# CI/CD + Custom Domain (`api.mindmycat.com`)

Deploy automatically on every push to `main`, served at **https://api.mindmycat.com**.

---

## Architecture

```
GitHub (push main)
    → GitHub Actions (SSH)
        → GCP VM
            → git pull + docker compose up --build
            → nginx :443 → app :8000 (localhost)
            → Redis (Docker)
```

---

## Part 1 — One-time VM setup (do this before CI/CD)

SSH into the VM and complete the initial deploy:

```bash
gcloud compute ssh pet-food-lookup --zone=us-central1-a

git clone https://github.com/YOUR_USER/Pet-Food-Barcode-Lookup.git ~/pet-food-barcode-lookup
cd ~/pet-food-barcode-lookup
cp deploy/env.production.example .env
nano .env   # API keys + GOOGLE_CLOUD_PROJECT

PLATFORM=gcp APP_PORT=8000 bash deploy/setup-vm.sh
```

---

## Part 2 — DNS (`api.mindmycat.com`)

At your domain registrar (Cloudflare, Namecheap, etc.):

| Type | Name | Value | TTL |
|------|------|-------|-----|
| **A** | `api` | `YOUR_VM_PUBLIC_IP` | 300 |

Get VM IP:

```bash
gcloud compute instances describe pet-food-lookup --zone=us-central1-a \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

Wait for DNS to propagate (often 5–30 min). Check:

```bash
dig +short api.mindmycat.com
```

---

## Part 3 — HTTPS (nginx + Let's Encrypt)

On the VM:

```bash
cd ~/pet-food-barcode-lookup
DOMAIN=api.mindmycat.com SSL_EMAIL=you@mydomain.com bash deploy/setup-domain.sh
```

Also open port 443 on GCP if not already:

```bash
gcloud compute firewall-rules create allow-pet-food-https \
  --project=project-11d80abc-a7c0-43df-9ed \
  --direction=INGRESS \
  --action=ALLOW \
  --rules=tcp:443 \
  --source-ranges=0.0.0.0/0 \
  --target-tags=http-server
```

Test: **https://api.mindmycat.com/api/health**

---

## Part 4 — SSH key for GitHub Actions

On your **Mac** (not the VM), generate a deploy key:

```bash
ssh-keygen -t ed25519 -C "github-actions-deploy" -f ~/.ssh/pet-food-deploy -N ""
```

**Add public key to the VM:**

```bash
# Copy pubkey contents
cat ~/.ssh/pet-food-deploy.pub

# On the VM:
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo "PASTE_PUBLIC_KEY_HERE" >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

Find your VM username (shown when you `gcloud compute ssh` — often your local Mac username):

```bash
gcloud compute ssh pet-food-lookup --zone=us-central1-a --dry-run 2>&1 | grep "ssh"
```

---

## Part 5 — GitHub repository secrets

Repo → **Settings** → **Secrets and variables** → **Actions** → **New repository secret**

| Secret | Value | Example |
|--------|-------|---------|
| `VM_HOST` | VM IP or domain | `api.mindmycat.com` or `34.x.x.x` |
| `VM_USER` | SSH username on VM | `evansmanyala` |
| `VM_SSH_KEY` | Private key (full file) | contents of `~/.ssh/pet-food-deploy` |
| `VM_APP_DIR` | *(optional)* App path | `/home/USER/pet-food-barcode-lookup` |
| `USE_NGINX` | *(optional)* | `true` (default) |

**Private repo:** ensure the VM can `git pull` — either:
- use a **deploy key** on the repo (read-only), or
- store a `GITHUB_TOKEN` / PAT on the VM for git auth

---

## Part 6 — Push to deploy

```bash
git push origin main
```

GitHub Actions → **Deploy to GCP VM** workflow runs → SSH → `deploy/remote-deploy.sh`.

Manual deploy anytime:

```bash
# On the VM
bash ~/pet-food-barcode-lookup/deploy/remote-deploy.sh
```

Or trigger from GitHub: **Actions** → **Deploy to GCP VM** → **Run workflow**.

---

## Tester URLs

| URL | Purpose |
|-----|---------|
| `https://api.mindmycat.com/` | Web UI |
| `https://api.mindmycat.com/?barcode=9003579008331` | Pre-filled demo |
| `https://api.mindmycat.com/api/health` | Health check |

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| Actions SSH fails | Check `VM_HOST`, `VM_USER`, `VM_SSH_KEY`; verify pubkey in VM `authorized_keys` |
| `git pull` fails on VM | Private repo needs deploy key; run `ssh -T git@github.com` on VM |
| Certbot fails | DNS A record must point to VM IP before running certbot |
| 502 Bad Gateway | `docker compose ps` — app must be on `127.0.0.1:8000` |
| Lookup timeout | nginx `proxy_read_timeout` is 300s in the site config |

---

## Order of operations (checklist)

1. [ ] VM created with service account
2. [ ] Step 8 manual deploy + `.env` on VM
3. [ ] DNS A record: `api` → VM IP
4. [ ] `setup-domain.sh` for nginx + SSL
5. [ ] GCP firewall allows 443
6. [ ] GitHub secrets configured
7. [ ] Push to `main` → auto-deploy
