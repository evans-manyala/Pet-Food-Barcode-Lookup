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

If that connects, GitHub Actions can SSH into the VM.

---

## Step 13.5. VM deploy key for `git pull` (on the VM)

GitHub Actions SSHs into the VM successfully, but **`deploy/remote-deploy.sh` runs `git pull` on the VM**. The VM must authenticate to GitHub separately (different key from Step 13).

If CI/CD fails with `git@github.com: Permission denied (publickey)`, complete this step.

SSH into the VM, then:

```bash
# 1. Create a deploy key (skip if ~/.ssh/github_deploy already exists)
ssh-keygen -t ed25519 -C "vm-git-pull" -f ~/.ssh/github_deploy -N ""
cat ~/.ssh/github_deploy.pub
```

Copy the public key. On GitHub → repo **Settings** → **Deploy keys** → **Add deploy key** (read-only, no write access needed).

Back on the VM:

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

ssh -T git@github.com
cd ~/pet-food-barcode-lookup && git pull origin main
```

Expected: `Hi evans-manyala/Pet-Food-Barcode-Lookup! You've successfully authenticated…` then a clean pull.

> **Note:** Step 13 (`pet-food-deploy`) lets GitHub Actions reach the VM. Step 13.5 (`github_deploy`) lets the VM reach GitHub. Both keys are required.

---

## Step 14. GitHub repository secrets

GitHub → **evans-manyala/Pet-Food-Barcode-Lookup** → **Settings** → **Secrets and variables** → **Actions**

Add these secrets:

| Secret | Value |
|--------|-------|
| `VM_HOST` | **GCP VM external IP** (not `api.mindmycat.com`) — see below |
| `VM_USER` | your VM username from Step 13c |
| `VM_SSH_KEY` | entire contents of `~/.ssh/pet-food-deploy` (private key) |
| `VM_APP_DIR` | `/home/<VM_USER>/pet-food-barcode-lookup` |
| `USE_NGINX` | `true` |
| `VM_SSH_PORT` | optional; default `22` |

**Important:** `api.mindmycat.com` is behind Cloudflare (HTTP/HTTPS only). GitHub Actions SSH **must** use the VM’s direct public IP, or a DNS-only (grey-cloud) hostname that points at that IP.

Get the VM IP on your Mac:

```bash
gcloud compute instances describe pet-food-lookup \
  --zone=us-central1-a \
  --project=project-11d80abc-a7c0-43df-9ed \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

Set `VM_HOST` to that IP (e.g. `34.133.118.0`). Keep `api.mindmycat.com` for browsers only — not for SSH deploy.

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
| `http://34.133.118.0/api/lookup?barcode=9003579008331` | API demo (IP, until HTTPS is live) |

**API reference:** [docs/API.md](../docs/API.md)

**Postman (share these files):**

1. [postman/Pet-Food-Barcode-Lookup.postman_collection.json](../postman/Pet-Food-Barcode-Lookup.postman_collection.json)
2. [postman/environments/Production.postman_environment.json](../postman/environments/Production.postman_environment.json) — update `base_url` when HTTPS is live

Import instructions: [postman/README.md](../postman/README.md)

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
[ ] Step 13  — GitHub Actions SSH key on VM (Actions → VM)
[ ] Step 13.5 — VM deploy key for git pull (VM → GitHub)
[ ] Step 14 — GitHub secrets set
[ ] Step 15 — git push → Actions deploy green
[ ] Step 16 — Share URL with testers
```

---

## Troubleshooting

| Issue | Fix |
|-------|-----|
| GitHub Actions SSH fails (`dial tcp …:22: i/o timeout`) | `VM_HOST` likely points at Cloudflare (proxied domain). Use the VM **external IP** from `gcloud compute instances describe … natIP`, not `api.mindmycat.com`. Test: `ssh -i ~/.ssh/pet-food-deploy USER@VM_IP` |
| GitHub Actions SSH fails (other) | Check `VM_USER`, `VM_SSH_KEY`; GCP firewall allows `tcp:22`; VM is running |
| `git pull` fails (`Permission denied (publickey)`) | VM → GitHub deploy key missing or not in `~/.ssh/config`. Complete **Step 13.5** (not Step 13 — that key is only for Actions → VM SSH). Test on VM: `ssh -T git@github.com` then `git pull origin main` |
| Certbot fails | DNS must resolve to VM IP first (`dig +short api.mindmycat.com`) |
| 502 Bad Gateway | `sudo docker compose -f docker-compose.yml -f docker-compose.prod.yml ps` — app must be up on `127.0.0.1:8000` |
| `bind: address already in use` on `127.0.0.1:8000` | Duplicate port mappings — use `docker-compose.prod.yml` (nginx) or `docker-compose.direct.yml` (public), not both. Run `compose down` then redeploy. |
| App shows `8000/tcp` only (no `127.0.0.1:8000->8000`) | Port overlay missing — nginx deploy needs `-f docker-compose.yml -f docker-compose.prod.yml`. Do not rely on `ports: !reset` (unsupported on some Compose builds). |
| Port 80 conflict | `sudo docker compose down` then re-run `setup-domain.sh` |
| Lookup timeout in browser / **504** on force refresh | Live search takes 30–90s+. nginx default is **60s** if `proxy_read_timeout` is missing. On VM: `grep proxy_read_timeout /etc/nginx/sites-enabled/*` — must show `300s`. Re-run `setup-domain.sh` or patch nginx (see deploy/nginx/pet-food-lookup.conf.template). Bypass nginx test: `curl -m 180 http://127.0.0.1:8000/api/lookup?barcode=...&force_refresh=true` |
| Cert renewal | certbot installs a cron job automatically; check `sudo certbot renew --dry-run` |

---

## What happens on every `git push main`

1. GitHub Actions connects to VM via SSH
2. Runs `deploy/remote-deploy.sh`:
   - `git pull origin main`
   - `docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build`
   - Health check on `localhost:8000`

No downtime config changes needed — Docker replaces the app container in place.
