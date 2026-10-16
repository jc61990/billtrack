FROM python:3.15-slim-bookworm
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 BT_DB_PATH=/data/billtrack.db \
    FORWARDED_ALLOW_IPS=127.0.0.1
RUN useradd --system --uid 10001 --home /app --shell /usr/sbin/nologin billtrack \
 && mkdir -p /data && chown billtrack /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --upgrade pip && pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY static ./static
USER billtrack
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=3)"
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080", "--proxy-headers", "--no-server-header", "--timeout-graceful-shutdown", "10"]
