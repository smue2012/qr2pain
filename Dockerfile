FROM python:3.12-slim

LABEL org.opencontainers.image.title="qr2pain" \
      org.opencontainers.image.description="Swiss QR-Rechnungen aus paperless-ngx als pain.001 exportieren" \
      org.opencontainers.image.source="https://github.com/smue2012/qr2pain"

RUN useradd --system --home /app qr2pain \
 && mkdir -p /data && chown qr2pain /data
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY qr2pain ./qr2pain

USER qr2pain
ENV QR2PAIN_CONFIG=/config/config.toml \
    QR2PAIN_DATA=/data
VOLUME ["/data"]
EXPOSE 8010
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8010/api/health', timeout=4)"
# data_dir in der config.toml weglassen -> /data; Reverse Proxy muss X-Forwarded-Proto setzen
CMD ["uvicorn", "qr2pain.web.app:app", "--host", "0.0.0.0", "--port", "8010", "--workers", "1", "--proxy-headers", "--forwarded-allow-ips", "*"]
