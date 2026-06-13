# CTG Terminal — container image
# NOTE: NSE blocks many datacenter IPs; for live NSE data run on a residential
# network/host. The container is ideal for the dashboard, tests and CI.
FROM python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    WEB_HOST=0.0.0.0 \
    WEB_PORT=8799

COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY . .

EXPOSE 8799
CMD ["python", "run.py"]
