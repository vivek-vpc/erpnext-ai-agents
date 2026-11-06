# Demo / trial image: the assistant on its built-in demo ERP. No ERPNext, no accounts, nothing leaves your machine.
# For a real ERPNext, mount your own settings file (see docker-compose.yml) instead of the demo one.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 ERP_AGENTS_DATA=/data
WORKDIR /app
COPY pyproject.toml README.md LICENSE ./
COPY src ./src
COPY engine ./engine
COPY docker ./docker
RUN pip install ".[agents]" \
    && useradd --create-home --uid 10001 agent \
    && mkdir /data && chown agent /data \
    && sed -i 's/\r$//' /app/docker/entrypoint.sh && chmod +x /app/docker/entrypoint.sh
USER agent
VOLUME /data
EXPOSE 8100
ENTRYPOINT ["/app/docker/entrypoint.sh"]
