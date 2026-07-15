# Build context: repository root (see docker-compose.yml)
FROM python:3.11-slim

RUN groupadd --system atlas && useradd --system --gid atlas --create-home atlas

WORKDIR /app

COPY libs/atlas-sdk ./libs/atlas-sdk
RUN pip install --no-cache-dir ./libs/atlas-sdk

COPY services/sentinel/pyproject.toml ./
COPY services/sentinel/atlas_sentinel ./atlas_sentinel
RUN pip install --no-cache-dir .

RUN mkdir -p /app/data && chown -R atlas:atlas /app
USER atlas

EXPOSE 8600

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import httpx; httpx.get('https://localhost:8600/healthz', verify=False).raise_for_status()"

CMD ["python", "-m", "atlas_sentinel.main"]
