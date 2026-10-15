FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 CRIEE_DATA_DIR=/data
WORKDIR /app
COPY pyproject.toml README.md ./
COPY criee/ criee/
RUN pip install --no-cache-dir .
COPY data/ data/
COPY --from=web /web/dist web/dist
RUN useradd -r -u 10001 criee && mkdir -p /data && chown criee /data
USER criee
VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/status',timeout=4)"
CMD ["uvicorn", "criee.api.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
