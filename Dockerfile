# Maahi Operator — headless business brain + command-center.
# Runs the operator only (no macOS voice stack), so it deploys anywhere:
# a VPS, Fly.io, Render, Cloud Run, a Raspberry Pi in your office.
#
#   docker build -t maahi-operator .
#   docker run --env-file .env -p 7777:7777 maahi-operator
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MAAHI_OPERATOR_HOST=0.0.0.0 \
    MAAHI_OPERATOR_PORT=7777 \
    MAAHI_STATE_DIR=/data

WORKDIR /app

COPY requirements-operator.txt .
RUN pip install -r requirements-operator.txt

# Only the bits the operator needs.
COPY maahi/ ./maahi/
COPY config.yaml ./config.yaml

VOLUME ["/data"]
EXPOSE 7777

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"MAAHI_OPERATOR_PORT\",\"7777\")}/healthz')" || exit 1

CMD ["python", "-m", "maahi.operator", "serve"]
