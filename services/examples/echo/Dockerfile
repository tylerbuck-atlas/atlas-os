FROM python:3.11-slim

RUN groupadd --system atlas && useradd --system --gid atlas --create-home atlas

WORKDIR /app

COPY pyproject.toml ./
COPY atlas_echo ./atlas_echo
RUN pip install --no-cache-dir .

USER atlas

EXPOSE 8100

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import httpx; httpx.get('http://localhost:8100/healthz').raise_for_status()"

CMD ["python", "-m", "atlas_echo.main"]
