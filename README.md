# Beamline Watchdog

A web-based EPICS PV watchdog that monitors process variables against configurable alarm conditions and sends email notifications when conditions are violated.

## Features

- **Live public dashboard** — shows all PV statuses with colour coding; auto-refreshes every 5 s (no login required)
- **Password-protected admin panel** — add/edit/delete PVs, email lists, compound rules, and system settings
- **Flexible alarm conditions** — `>`, `<`, `≥`, `≤`, `=`, `≠`, in-range, out-of-range
- **Compound boolean rules** — combine multiple PV alarm states with AND / OR operators
- **Email notifications** — on alarm onset, recovery, and periodic repeat while in alarm (configurable interval)
- **Multiple email lists** — each PV or rule targets its own list
- **Notification log** — paginated history of every alert sent (or failed)
- **Background watchdog** — APScheduler runs in-process; restart-safe

## Quick Start

```bash
cd beamline_watchdog

# 1. Install dependencies
pip install -r requirements.txt

# 2. (Optional) create .env from the example
cp .env.example .env
# Edit .env — set SECRET_KEY and EPICS_CA_ADDR_LIST at minimum

# 3. Start the server
python run.py
```

Open **http://localhost:5000** for the live dashboard.
Navigate to **http://localhost:5000/admin** to log in (default: `admin` / `admin` — change immediately).

## Running in the Background

```bash
# Using nohup
nohup python run.py > watchdog.log 2>&1 &

# Or with screen
screen -S watchdog
python run.py
# Ctrl-A D to detach

# Or as a systemd service (create /etc/systemd/system/watchdog.service)
# See the systemd example below
```

### systemd service example

```ini
[Unit]
Description=Beamline PV Watchdog
After=network.target

[Service]
User=controls
WorkingDirectory=/path/to/beamline_watchdog
EnvironmentFile=/path/to/beamline_watchdog/.env
ExecStart=/usr/bin/python3 /path/to/beamline_watchdog/run.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable watchdog
sudo systemctl start watchdog
```

## EPICS Configuration

Set the EPICS environment before starting (or in `.env`):

```bash
export EPICS_CA_ADDR_LIST="192.168.1.255 10.0.0.255"
export EPICS_CA_AUTO_ADDR_LIST=NO
```

If `pyepics` is installed but the IOC is unreachable the PV shows **DISCONNECTED** — no alarm is raised (partial evaluation rule: a compound rule containing a DISCONNECTED PV evaluates to UNKNOWN, not ALARM).

## Project Layout

```
beamline_watchdog/
├── app/
│   ├── __init__.py          # Flask app factory
│   ├── models.py            # SQLAlchemy models
│   ├── auth.py              # Flask-Login user loader
│   ├── watchdog.py          # APScheduler background monitor
│   ├── condition_eval.py    # Alarm condition / compound-rule evaluator
│   ├── email_utils.py       # SMTP email sender
│   ├── routes/
│   │   ├── dashboard.py     # Public routes + /api/status JSON
│   │   └── admin.py         # Protected CRUD routes
│   ├── static/css/style.css
│   └── templates/           # Jinja2 templates (base, dashboard, admin/*)
├── config.py                # Flask Config from env
├── run.py                   # Entry point
├── .env.example
└── requirements.txt
```

## Compound Rules

A compound rule lets you send a notification when a *combination* of PV states is true, for example:

> Send email when **PV_A** is in ALARM **AND** **PV_B** is in ALARM

or:

> Send email when **PV_A** is OK **OR** **PV_B** is in ALARM

Rules are built in the admin UI with a dynamic condition builder — no manual JSON editing required.

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SECRET_KEY` | `dev-key-…` | Flask session secret — **change in production** |
| `PORT` | `5000` | HTTP port |
| `FLASK_DEBUG` | `false` | Enable Flask debug mode |
| `DATABASE_URL` | `sqlite:///instance/watchdog.db` | SQLAlchemy database URI |
| `EPICS_CA_ADDR_LIST` | — | Broadcast/unicast EPICS CA addresses |
| `EPICS_CA_AUTO_ADDR_LIST` | — | Set to `NO` when using explicit list |

Email settings are stored in the database and configurable from the admin Settings page.
