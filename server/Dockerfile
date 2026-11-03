ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Front-end libraries are downloaded once at build time (no CDN needed at runtime)
COPY scripts/fetch_vendor.py scripts/fetch_vendor.py
RUN python scripts/fetch_vendor.py /app/app/static/vendor

COPY . .
RUN mkdir -p instance

ENV PYTHONUNBUFFERED=1 \
    POWERMANAGER_INSTANCE=/app/instance

EXPOSE 5000

HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=4).status == 200 else 1)"

CMD ["gunicorn", "-c", "gunicorn.conf.py", "wsgi:app"]
