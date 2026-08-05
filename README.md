# Beamline Watchdog

A web-based EPICS PV and process watchdog. Monitors process variables against configurable alarm conditions and system processes, sends email notifications when conditions are violated, and lets authenticated admins start/stop monitored processes directly from the browser.

## Features

- **Live public dashboard** — colour-coded PV and process status; auto-refreshes every 5 s (no login required)
- **Password-protected admin panel** — full CRUD for PVs, email lists, compound rules, process monitors, users, and settings
- **Flexible alarm conditions** — `>`, `<`, `≥`, `≤`, `=`, `≠`, in-range, out-of-range
- **Compound boolean rules** — combine multiple PV alarm states with AND / OR logic
- **Process monitoring** — detect when a system process stops; optional start/stop/kill from the browser
- **Email notifications** — alarm onset, recovery, and periodic repeat (configurable interval); multiple recipient lists
- **Notification log** — paginated history of every alert sent or failed
- **Action audit log** — every browser-initiated start/stop/kill is recorded with timestamp and operator name
- **CSRF protection** — all POST forms are token-protected (Flask-WTF)
- **Rate-limited login** — 20 requests/minute per IP to block brute-force attacks
- **Background watchdog** — APScheduler runs in-process; configurable check interval

---

## Table of Contents

1. [Quick Start (development)](#quick-start-development)
2. [Production Deployment](#production-deployment)
   - [Prerequisites](#prerequisites)
   - [Install the application](#install-the-application)
   - [Configure environment](#configure-environment)
   - [Run with Gunicorn](#run-with-gunicorn)
   - [systemd service](#systemd-service)
   - [nginx reverse proxy (HTTPS)](#nginx-reverse-proxy-https)
   - [First-time setup](#first-time-setup)
3. [EPICS Configuration](#epics-configuration)
4. [Process Control](#process-control)
5. [Compound Rules](#compound-rules)
6. [Project Layout](#project-layout)
7. [Environment Variables](#environment-variables)
8. [Production Security Checklist](#production-security-checklist)

---

## Quick Start (development)

```bash
# With conda
conda create -n watchdog python=3.11 -y && conda activate watchdog
conda install -c conda-forge pyepics -y

# Or with venv
python3 -m venv venv && source venv/bin/activate

# Then, either way:
cd beamline_watchdog
pip install -r requirements.txt
cp .env.example .env          # edit .env — set SECRET_KEY at minimum
PORT=5001 python run.py
```

Open **http://localhost:5001** for the dashboard.  
Go to **http://localhost:5001/admin** — default credentials `admin` / `admin` (change immediately).

---

## Production Deployment

### Prerequisites

| Requirement | Notes |
|---|---|
| Python 3.9+ | Test with `python3 --version` |
| pip | Usually bundled with Python |
| EPICS Base | Only needed if monitoring EPICS PVs; sets `EPICS_CA_ADDR_LIST` |
| nginx (optional) | Recommended for HTTPS termination |
| A dedicated system user | e.g. `controls` — the process runs as this user |

### Install the application

**Option A — virtualenv (recommended for servers)**

```bash
# 1. Clone the repository
git clone https://github.com/nayanbera/beamline_watchdog.git
cd beamline_watchdog

# 2. Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate

# 3. Install Python dependencies
pip install -r requirements.txt
```

**Option B — conda environment**

```bash
# 1. Clone the repository
git clone https://github.com/nayanbera/beamline_watchdog.git
cd beamline_watchdog

# 2. Create and activate a conda environment
conda create -n watchdog python=3.11 -y
conda activate watchdog

# 3. Install Python dependencies
#    pyepics is available on conda-forge; everything else comes from pip
conda install -c conda-forge pyepics -y
pip install -r requirements.txt
```

### Configure environment

```bash
# Copy the example and open it in your editor
cp .env.example .env
nano .env
```

At minimum, set `SECRET_KEY` to a random value:

```bash
# Generate a secure key and paste it into .env
python3 -c "import secrets; print(secrets.token_hex(32))"
```

Your `.env` should look like:

```ini
SECRET_KEY=a1b2c3...64-character-hex-string...
PORT=5001
```

> **Warning:** The app logs a `CRITICAL` message at every startup if `SECRET_KEY` is still the
> insecure default. Admin sessions can be forged without a real key.

### Run with Gunicorn

Gunicorn is the production WSGI server. The included `gunicorn.conf.py` sets `workers = 1`
(required because the APScheduler watchdog must run in a single process).

```bash
# Create the log directory
mkdir -p logs

# Start Gunicorn
source venv/bin/activate
gunicorn -c gunicorn.conf.py wsgi:app
```

To run in the background without a service manager:

```bash
nohup gunicorn -c gunicorn.conf.py wsgi:app > logs/gunicorn.log 2>&1 &
```

### systemd service

Create `/etc/systemd/system/beamline-watchdog.service`.

**If using a virtualenv:**

```ini
[Unit]
Description=Beamline PV Watchdog
After=network.target

[Service]
Type=simple
User=controls
Group=controls
WorkingDirectory=/opt/beamline_watchdog
EnvironmentFile=/opt/beamline_watchdog/.env
ExecStart=/opt/beamline_watchdog/venv/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=always
RestartSec=5
StandardOutput=append:/opt/beamline_watchdog/logs/service.log
StandardError=append:/opt/beamline_watchdog/logs/service.log

[Install]
WantedBy=multi-user.target
```

**If using a conda environment** (replace `/opt/anaconda3` with your actual conda prefix — find it with `conda info --base`):

```ini
[Unit]
Description=Beamline PV Watchdog
After=network.target

[Service]
Type=simple
User=controls
Group=controls
WorkingDirectory=/opt/beamline_watchdog
EnvironmentFile=/opt/beamline_watchdog/.env
Environment=PATH=/opt/anaconda3/envs/watchdog/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin
ExecStart=/opt/anaconda3/envs/watchdog/bin/gunicorn -c gunicorn.conf.py wsgi:app
Restart=always
RestartSec=5
StandardOutput=append:/opt/beamline_watchdog/logs/service.log
StandardError=append:/opt/beamline_watchdog/logs/service.log

[Install]
WantedBy=multi-user.target
```

> **Tip:** Find the full path to gunicorn in your active conda environment with `which gunicorn`
> after running `conda activate watchdog`. Use that exact path in `ExecStart`.

```bash
sudo systemctl daemon-reload
sudo systemctl enable beamline-watchdog
sudo systemctl start beamline-watchdog

# Check status
sudo systemctl status beamline-watchdog
sudo journalctl -u beamline-watchdog -f
```

To restart after a code update:

```bash
git pull
sudo systemctl restart beamline-watchdog
```

### nginx reverse proxy (HTTPS)

Running nginx in front of Gunicorn gives you HTTPS and hides the internal port.

Install nginx and obtain a certificate (e.g. with Let's Encrypt or a self-signed cert for a private network):

```bash
sudo apt install nginx
# For Let's Encrypt on a public server:
sudo apt install certbot python3-certbot-nginx
sudo certbot --nginx -d watchdog.yourbeamline.example.com
```

Create `/etc/nginx/sites-available/beamline-watchdog`:

```nginx
server {
    listen 80;
    server_name watchdog.yourbeamline.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name watchdog.yourbeamline.example.com;

    ssl_certificate     /etc/letsencrypt/live/watchdog.yourbeamline.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/watchdog.yourbeamline.example.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:5001;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto $scheme;
    }
}
```

```bash
sudo ln -s /etc/nginx/sites-available/beamline-watchdog /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

For a **self-signed certificate** on an isolated beamline network:

```bash
sudo openssl req -x509 -nodes -days 3650 -newkey rsa:2048 \
  -keyout /etc/ssl/private/watchdog.key \
  -out /etc/ssl/certs/watchdog.crt \
  -subj "/CN=watchdog.local"
```

Then use those paths in the nginx `ssl_certificate` directives above.

### First-time setup

After the service is running:

1. Open the dashboard URL in your browser.
2. Click **Admin** (top-right) and log in with `admin` / `admin`.
3. Go to **Settings** and:
   - Change **Admin Password** immediately.
   - Set the **Site Name** (appears in the navbar and browser tab).
   - Configure **SMTP** settings for email notifications.
   - Set **EPICS_CA_ADDR_LIST** if your IOCs are on a different subnet.
4. Add PVs, email lists, compound rules, and process monitors as needed.

---

## EPICS Configuration

EPICS Channel Access settings can be set in `.env` or in **Admin → Settings → EPICS Channel Access**.
Settings saved via the admin UI take effect on the next watchdog tick without restarting the server.

```bash
# .env — set before first startup so the CA library initialises correctly
EPICS_CA_ADDR_LIST="192.168.1.255 10.54.3.1"
EPICS_CA_AUTO_ADDR_LIST=NO
```

If `pyepics` is installed but an IOC is unreachable the PV shows **DISCONNECTED**. No alarm is
raised for a disconnected PV. A compound rule that contains a DISCONNECTED PV evaluates to
**UNKNOWN** rather than ALARM, preventing false alerts during network outages.

---

## Process Control

Each process monitor can optionally store a **Start Command** and **Stop Command**. When configured,
**Start / Stop / Kill** buttons appear on the admin Processes page and (for logged-in admins) on the
public dashboard.

- Commands are stored once at configuration time — no free-text input at runtime.
- Executed server-side with `shlex.split` + `subprocess.Popen` (`shell=False`).
- Every action is written to the **Action Log** (`/admin/processes/action-log`).
- The web server process must have OS-level permission to run the configured commands.
  Use `sudo` rules (sudoers) or set file permissions as needed.

> **Important:** Do not monitor the Flask/Gunicorn process itself and assign a Stop command to it —
> stopping it will take down the web interface. Use systemd to manage the server process instead.

---

## Compound Rules

A compound rule sends a notification when a *combination* of PV alarm states is true:

> Alert when **PV_A** is ALARM **AND** **PV_B** is ALARM

> Alert when **PV_A** is OK **OR** **PV_B** is ALARM

Rules are built with the dynamic condition builder in the admin UI — no JSON editing required.
Partial evaluation: if any PV in the rule is DISCONNECTED/UNKNOWN the rule evaluates to UNKNOWN
and no notification is sent.

---

## Project Layout

```
beamline_watchdog/
├── app/
│   ├── __init__.py          # App factory; CSRFProtect, Limiter, context processor
│   ├── models.py            # SQLAlchemy models (PVMonitor, ProcessMonitor, ActionLog, …)
│   ├── auth.py              # Flask-Login user loader
│   ├── watchdog.py          # APScheduler background monitor loop
│   ├── condition_eval.py    # Alarm condition / compound-rule evaluator
│   ├── email_utils.py       # SMTP email sender
│   ├── routes/
│   │   ├── dashboard.py     # Public dashboard + /api/status JSON endpoint
│   │   └── admin.py         # Login-protected CRUD + process control routes
│   ├── static/css/style.css
│   └── templates/           # Jinja2 templates (base, dashboard, admin/*)
├── config.py                # Flask Config — reads from .env
├── run.py                   # Development entry point (Flask dev server)
├── wsgi.py                  # Production entry point (Gunicorn)
├── gunicorn.conf.py         # Gunicorn configuration (workers=1 required)
├── .env.example             # Template for .env
├── .gitignore
├── requirements.txt
└── README.md
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | *(insecure default)* | Flask session secret — **must be changed in production** |
| `PORT` | `5001` | HTTP port Gunicorn/Flask listens on |
| `FLASK_DEBUG` | `false` | Enable Flask debug mode (never use in production) |
| `DATABASE_URL` | `sqlite:///instance/watchdog.db` | SQLAlchemy database URI |
| `EPICS_CA_ADDR_LIST` | — | Space-separated CA broadcast/unicast addresses |
| `EPICS_CA_AUTO_ADDR_LIST` | `YES` | Set to `NO` when using an explicit address list |

Email (SMTP) settings and the watchdog check interval are stored in the database and
configurable from **Admin → Settings**.

---

## Production Security Checklist

- [ ] `SECRET_KEY` set to a random 32-byte hex string in `.env` — no CRITICAL warning at startup
- [ ] Default `admin` password changed via **Admin → Settings → Change Password**
- [ ] Running under a dedicated non-root system user (e.g. `controls`)
- [ ] HTTPS enabled via nginx + certificate (Let's Encrypt or self-signed)
- [ ] Firewall allows only port 443 (HTTPS) externally; port 5001 is localhost-only
- [ ] `FLASK_DEBUG=false` (the default — never run debug mode in production)
- [ ] `logs/` directory exists and is writable by the service user
- [ ] systemd `Restart=always` configured so the server recovers from crashes automatically
- [ ] EPICS CA addresses confirmed correct in **Admin → Settings → EPICS Channel Access**
