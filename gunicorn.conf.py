import os

bind = f"0.0.0.0:{os.environ.get('PORT', '5001')}"

# Must stay at 1 — APScheduler watchdog must run in exactly one process.
# If you need concurrency, move the scheduler to a separate worker process.
workers = 1
worker_class = "sync"
timeout = 120

# Logs — directory is created by the start script
accesslog = "logs/access.log"
errorlog = "logs/error.log"
loglevel = "info"
