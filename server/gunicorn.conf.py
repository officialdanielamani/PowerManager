import os

bind = "0.0.0.0:5000"
# Exactly one worker: live meter WebSocket connections are kept in this process's memory.
workers = 1
worker_class = "gthread"
threads = int(os.environ.get("GUNICORN_THREADS", "100"))
timeout = 120
graceful_timeout = 10
accesslog = "-"
errorlog = "-"
