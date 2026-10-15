# Beginner’s Guide: Deploy Vendor Ops to DigitalOcean

This guide deploys the application to:

**https://ops.thegoodflea.com**

The project has two separate GitHub repositories:

- `vendor-ops-backend`: Django, PostgreSQL migrations, Gunicorn, systemd, and the Nginx configuration.
- `vendor-ops-frontend`: the Svelte application compiled into static files.

Nginx serves the frontend and sends `/api/` and `/admin/` requests to Django. The frontend and backend therefore use the same domain, which keeps login and CSRF cookies straightforward.

## Before you start

Commands in this guide are labelled by where they should be run:

- **Local computer** means your Windows development machine.
- **Droplet** means the terminal after you SSH into DigitalOcean.
- **GitHub** means the repository’s Settings or Actions page in your browser.

Replace placeholders such as `DROPLET_IP` and `DEPLOY_USER` with real values. Do not type the angle brackets themselves. Never commit `.env`, database passwords, Django secret keys, or SSH private keys.

Because this application was previously installed, some data may still exist even though the application folders and Nginx files were removed. This guide checks that state before installing anything.

---

## Step 1 — Save and push the deployment files

CI/CD runs from GitHub, so the changes in both repositories must be committed and pushed first.

On your **local computer**, review and commit the backend repository:

```sh
cd backend
git status
git add .env.example config/settings.py .github deploy DEPLOYMENT.md
git commit -m "Add DigitalOcean backend deployment"
git push origin main
```

Then commit the frontend repository:

```sh
cd ../frontend
git status
git add .github deploy
git commit -m "Add DigitalOcean frontend deployment"
git push origin main
```

If `git status` shows other application changes, review them before committing. Do not use `git add .` unless you intend to include every displayed change.

## Step 2 — Confirm the domain points to the droplet

In the DNS manager for `thegoodflea.com`, create or verify this record:

| Type | Host/name | Value |
|---|---|---|
| A | `ops` | The droplet’s public IPv4 address |

Only add an `AAAA` record if the droplet has working public IPv6.

On your **local computer**, check the result:

```sh
nslookup ops.thegoodflea.com
```

The returned address should match the droplet. DNS changes can take time to propagate; do not request an SSL certificate until this is correct.

## Step 3 — Connect to the droplet and inspect the old installation

On your **local computer**:

```sh
ssh DEPLOY_USER@DROPLET_IP
```

You are now running commands on the **droplet**. Check for leftovers from the previous installation:

```sh
sudo systemctl status vendor-ops-backend --no-pager
sudo systemctl list-unit-files | grep vendor-ops
sudo ls -la /etc/nginx/sites-available
sudo ls -la /etc/nginx/sites-enabled
sudo certbot certificates
sudo ls -la /opt/vendor-ops /var/www/vendor-ops /etc/vendor-ops
```

“Unit could not be found” or “No such file or directory” is acceptable because the old files were deleted. If `certbot certificates` still lists `ops.thegoodflea.com`, keep it; Certbot can reconnect that certificate to the new Nginx configuration later.

Do not delete the PostgreSQL database. Check whether PostgreSQL is running:

```sh
sudo systemctl status postgresql --no-pager
sudo -u postgres psql -c '\l'
```

Confirm that the expected database still appears in the list. Type `q` if a command opens a pager.

## Step 4 — Verify required server software

On the **droplet**:

```sh
nginx -v
python3 --version
psql --version
curl --version
```

Also verify Python can create a virtual environment:

```sh
python3 -m venv /tmp/vendor-ops-venv-check
rm -rf /tmp/vendor-ops-venv-check
```

If that command fails on Ubuntu, install the missing packages:

```sh
sudo apt update
sudo apt install python3-venv python3-pip curl nginx postgresql-client
```

Nginx and PostgreSQL are already installed according to the current server setup, so only install packages that are actually missing.

## Step 5 — Create a dedicated CI deployment login

GitHub Actions needs an SSH user that can upload releases and run the deployment scripts. A dedicated user keeps this separate from your normal login.

On the **droplet**:

```sh
sudo adduser --disabled-password --gecos "" vendor-deploy
sudo usermod -aG sudo vendor-deploy
sudo install -d -m 700 -o vendor-deploy -g vendor-deploy /home/vendor-deploy/.ssh
```

The workflows run non-interactively, so this dedicated deployment user requires passwordless `sudo`. It should be used only by GitHub Actions:

```sh
echo 'vendor-deploy ALL=(ALL) NOPASSWD: ALL' | sudo tee /etc/sudoers.d/vendor-deploy
sudo chmod 440 /etc/sudoers.d/vendor-deploy
sudo visudo -cf /etc/sudoers.d/vendor-deploy
```

The final command must report that the file parsed successfully.

## Step 6 — Create the GitHub deployment SSH key

Exit the droplet so you are back on your **local computer**:

```sh
exit
```

Create a dedicated key. When prompted for a passphrase, leave it empty because GitHub Actions cannot answer an interactive prompt:

```sh
ssh-keygen -t ed25519 -C "github-actions-vendor-ops" -f ~/.ssh/vendor_ops_deploy
```

This creates:

- `~/.ssh/vendor_ops_deploy` — private key; store only in GitHub Secrets.
- `~/.ssh/vendor_ops_deploy.pub` — public key; install on the droplet.

Copy the public key to the new account:

```sh
ssh-copy-id -i ~/.ssh/vendor_ops_deploy.pub vendor-deploy@DROPLET_IP
```

Test the key before continuing:

```sh
ssh -i ~/.ssh/vendor_ops_deploy vendor-deploy@DROPLET_IP
sudo -n true
exit
```

Both commands should succeed without asking for a password.

## Step 7 — Bootstrap the droplet

The bootstrap script installs the systemd service and initial Nginx site. It also copies the existing backend `.env` into a protected location that future releases cannot overwrite.

From the project root on your **local computer**, upload only the bootstrap files and existing backend environment file:

```sh
scp -i ~/.ssh/vendor_ops_deploy -r backend/deploy vendor-deploy@DROPLET_IP:/tmp/vendor-ops-deploy
scp -i ~/.ssh/vendor_ops_deploy backend/.env vendor-deploy@DROPLET_IP:/tmp/vendor-ops-backend.env
```

Connect to the **droplet**:

```sh
ssh -i ~/.ssh/vendor_ops_deploy vendor-deploy@DROPLET_IP
```

Confirm the two upload targets before running the setup:

```sh
ls -la /tmp/vendor-ops-deploy
ls -l /tmp/vendor-ops-backend.env
```

Run the bootstrap:

```sh
sudo bash /tmp/vendor-ops-deploy/bootstrap-droplet.sh /tmp/vendor-ops-backend.env
```

The script creates these permanent locations:

- `/etc/vendor-ops/backend.env` — protected production credentials and settings.
- `/opt/vendor-ops/` — backend virtual environment and releases.
- `/var/www/vendor-ops/` — frontend releases.
- `/etc/systemd/system/vendor-ops-backend.service` — Gunicorn service.
- `/etc/nginx/sites-available/ops.thegoodflea.com` — Nginx site.

After bootstrap succeeds, remove the temporary credential copy:

```sh
rm -f /tmp/vendor-ops-backend.env
```

## Step 8 — Configure the production environment

On the **droplet**, open the protected environment file:

```sh
sudoedit /etc/vendor-ops/backend.env
```

Keep the existing `DJANGO_SECRET_KEY`, PostgreSQL credentials, and business-hour settings. Change or confirm these values:

```dotenv
DJANGO_DEBUG=false
DJANGO_ALLOWED_HOSTS=ops.thegoodflea.com,127.0.0.1,localhost
DATABASE_URL=postgres://DATABASE_USER:DATABASE_PASSWORD@127.0.0.1:5432/DATABASE_NAME
CORS_ALLOWED_ORIGINS=
CSRF_TRUSTED_ORIGINS=https://ops.thegoodflea.com
DJANGO_SECURE_COOKIES=false
DJANGO_SECURE_SSL_REDIRECT=false
DJANGO_HSTS_SECONDS=0
```

Use the same database name, username, and password that previously worked. If the password contains special URL characters such as `@`, `:`, `/`, `#`, or `%`, it must be URL-encoded in `DATABASE_URL`.

Secure cookies and forced HTTPS remain disabled only for the first HTTP deployment. They are enabled after Certbot is confirmed working.

Check the protected file permissions without printing its contents:

```sh
sudo chown root:vendorops /etc/vendor-ops/backend.env
sudo chmod 640 /etc/vendor-ops/backend.env
sudo ls -l /etc/vendor-ops/backend.env
```

## Step 9 — Add secrets to both GitHub repositories

First collect the droplet’s SSH host key on your **local computer**:

```sh
ssh-keyscan -H DROPLET_IP > vendor-ops-known-hosts.txt
ssh-keygen -lf vendor-ops-known-hosts.txt
```

Compare that fingerprint with the droplet’s host key fingerprint, obtained on the **droplet** with:

```sh
sudo ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

In **each GitHub repository**:

1. Open **Settings → Environments**.
2. Click **New environment**.
3. Name it exactly `production`.
4. Open the new environment and add these environment secrets:

| Secret | Value |
|---|---|
| `DROPLET_HOST` | Droplet IP address |
| `DROPLET_USER` | `vendor-deploy` |
| `DROPLET_SSH_PORT` | Usually `22` |
| `DROPLET_SSH_PRIVATE_KEY` | Entire contents of `~/.ssh/vendor_ops_deploy`, including BEGIN/END lines |
| `DROPLET_SSH_KNOWN_HOSTS` | Entire contents of `vendor-ops-known-hosts.txt` |

Add these secrets to both `vendor-ops-backend` and `vendor-ops-frontend`. Do not add the backend `.env` to GitHub; it already lives at `/etc/vendor-ops/backend.env` on the droplet.

## Step 10 — Run the first backend deployment

In the **backend GitHub repository**:

1. Open the **Actions** tab.
2. Select **Backend CI/CD**.
3. Click **Run workflow**.
4. Select the `main` branch and confirm.

The workflow will:

1. Start a temporary PostgreSQL service inside GitHub Actions.
2. Install Python dependencies.
3. Run Django checks and tests.
4. Check for missing migrations.
5. Upload the backend release to the droplet.
6. Install dependencies into `/opt/vendor-ops/venv`.
7. Apply database migrations and collect Django static files.
8. Start Gunicorn with systemd and call the database-backed health endpoint.

The first run may take several minutes. If it fails, open the failed step in GitHub Actions and read its final error lines.

On the **droplet**, verify the backend:

```sh
sudo systemctl status vendor-ops-backend --no-pager
sudo journalctl -u vendor-ops-backend -n 50 --no-pager
curl --fail --unix-socket /run/vendor-ops/gunicorn.sock http://localhost/api/v1/health/
```

The final command should return JSON containing `"status":"ok"`.

## Step 11 — Run the first frontend deployment

In the **frontend GitHub repository**:

1. Open the **Actions** tab.
2. Select **Frontend CI/CD**.
3. Click **Run workflow**.
4. Select `main` and confirm.

The workflow checks the Svelte application, runs unit tests, builds the static site, uploads it, changes the `/var/www/vendor-ops/current` symlink, validates Nginx, and makes an HTTP request to the site.

On the **droplet**, verify it:

```sh
sudo nginx -t
curl -I -H 'Host: ops.thegoodflea.com' http://127.0.0.1/
curl -H 'Host: ops.thegoodflea.com' http://127.0.0.1/api/v1/health/
```

The first command should report that the Nginx configuration is successful. The second should return an HTTP `200` response, and the third should return the health JSON.

At this point, `http://ops.thegoodflea.com` should load, but login cookies are not yet forced to HTTPS.

## Step 12 — Reconnect or issue the SSL certificate

Check whether the old certificate still exists:

```sh
sudo certbot certificates
```

If `ops.thegoodflea.com` appears and is not expired, run Certbot’s Nginx installer to reconnect it to the new site. If it does not appear, the same command requests a new certificate:

```sh
sudo certbot --nginx -d ops.thegoodflea.com
```

Choose the redirect-to-HTTPS option if Certbot asks. Then test:

```sh
curl -I https://ops.thegoodflea.com/
curl --fail https://ops.thegoodflea.com/api/v1/health/
```

Do not continue until both commands work without certificate errors.

## Step 13 — Enable Django’s HTTPS protections

On the **droplet**:

```sh
sudoedit /etc/vendor-ops/backend.env
```

Change these values:

```dotenv
DJANGO_SECURE_COOKIES=true
DJANGO_SECURE_SSL_REDIRECT=true
DJANGO_HSTS_SECONDS=3600
```

Restart Django and test login in a private browser window:

```sh
sudo systemctl restart vendor-ops-backend
sudo systemctl status vendor-ops-backend --no-pager
curl --fail https://ops.thegoodflea.com/api/v1/health/
```

After the site has worked correctly over HTTPS for several days, you may increase HSTS to one year:

```dotenv
DJANGO_HSTS_SECONDS=31536000
```

HSTS tells browsers to insist on HTTPS, so only increase it after confirming the certificate renews correctly:

```sh
sudo certbot renew --dry-run
```

## Step 14 — Confirm automatic deployments

After initial setup, every push to `main` deploys automatically after tests pass:

- Backend changes deploy from the backend repository.
- Frontend changes deploy from the frontend repository.
- Pull requests only run checks and tests; they do not deploy.

Watch deployments under each repository’s **Actions** tab. A failed test prevents deployment and leaves the currently running release untouched.

## Routine commands

Run these on the **droplet** when troubleshooting:

```sh
# Service state
sudo systemctl status vendor-ops-backend --no-pager
sudo systemctl status nginx --no-pager
sudo systemctl status postgresql --no-pager

# Recent backend logs
sudo journalctl -u vendor-ops-backend -n 100 --no-pager

# Follow backend logs live; press Ctrl+C to stop
sudo journalctl -u vendor-ops-backend -f

# Validate and reload Nginx
sudo nginx -t
sudo systemctl reload nginx

# Verify application and database health
curl --fail https://ops.thegoodflea.com/api/v1/health/

# Email worker (runs every minute; sends nothing unless EMAIL_GLOBAL_MODE allows it — see README "Email")
sudo systemctl list-timers vendor-ops-send-emails.timer
sudo journalctl -u vendor-ops-send-emails.service -n 50 --no-pager
sudo -u vendorops /opt/vendor-ops/venv/bin/python /opt/vendor-ops/backend-current/manage.py send_due_emails --dry-run
sudo systemctl stop vendor-ops-send-emails.timer   # emergency stop; the next deploy starts it again

# List retained releases
ls -1dt /opt/vendor-ops/releases/backend-*
ls -1dt /var/www/vendor-ops/releases/frontend-*
```

## Roll back to an earlier release

The deployment scripts retain the five latest releases. First list them and copy the complete path of the release you want:

```sh
ls -1dt /opt/vendor-ops/releases/backend-*
ls -1dt /var/www/vendor-ops/releases/frontend-*
```

Backend rollback example:

```sh
sudo ln -sfn /opt/vendor-ops/releases/backend-RELEASE_ID /opt/vendor-ops/backend-current
sudo systemctl restart vendor-ops-backend
```

Frontend rollback example:

```sh
sudo ln -sfn /var/www/vendor-ops/releases/frontend-RELEASE_ID /var/www/vendor-ops/current
sudo systemctl reload nginx
```

Replace `RELEASE_ID` with an actual retained release ID. Database migrations are not automatically reversed. If a release included a database schema change, investigate the migration before rolling backend code backward.

## Common problems

### GitHub reports `Permission denied (publickey)`

- Confirm `DROPLET_USER` is `vendor-deploy`.
- Confirm the public key is in `/home/vendor-deploy/.ssh/authorized_keys`.
- Confirm the private key secret includes its BEGIN and END lines.
- Test the key locally with `ssh -i ~/.ssh/vendor_ops_deploy vendor-deploy@DROPLET_IP`.

### GitHub reports `Host key verification failed`

Regenerate `vendor-ops-known-hosts.txt`, verify its fingerprint, and replace `DROPLET_SSH_KNOWN_HOSTS` in both GitHub environments.

### Nginx returns `502 Bad Gateway`

Gunicorn is not reachable. Check:

```sh
sudo systemctl status vendor-ops-backend --no-pager
sudo journalctl -u vendor-ops-backend -n 100 --no-pager
sudo ls -la /run/vendor-ops/gunicorn.sock
```

### The frontend loads but API requests fail

Check both local proxy routes:

```sh
curl -I -H 'Host: ops.thegoodflea.com' http://127.0.0.1/
curl -H 'Host: ops.thegoodflea.com' http://127.0.0.1/api/v1/health/
```

Then inspect the browser’s Network tab for the failing `/api/v1/...` request.

### Django reports a PostgreSQL authentication error

Recheck only the `DATABASE_URL` in `/etc/vendor-ops/backend.env`. Confirm the database, role, and password still exist. Do not recreate or delete the existing database until you have confirmed whether it contains production data.

### Certbot says a certificate already exists

This is expected after a previous installation. Select the option to reinstall the existing certificate unless it is expired or covers a different domain.
