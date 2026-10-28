FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:0.10.9 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_NO_DEV=1 UV_PROJECT_ENVIRONMENT=/opt/tcc/.venv
WORKDIR /opt/tcc
RUN apt-get update && apt-get install -y --no-install-recommends tzdata && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --locked --no-install-project --extra app
COPY src ./src
RUN uv sync --locked --no-editable --extra app
COPY app ./app
ENV PATH=/opt/tcc/.venv/bin:$PATH PYTHONPATH=/opt/tcc/app
RUN python -c "from zoneinfo import ZoneInfo; ZoneInfo('Europe/Amsterdam'); import tcc_app.main; import tcc_app.mcp_server"
RUN useradd --system --uid 10001 tcc && mkdir -p /data /config && chown 10001:10001 /data
USER 10001
VOLUME /data
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=120s CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"]
CMD ["python", "-m", "tcc_app"]
