FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1

RUN groupadd --gid 10001 forge \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin forge

WORKDIR /opt/forge-agent

COPY pyproject.toml README.md LICENSE ./
COPY src ./src

RUN python -m pip install --no-cache-dir ".[dev]" \
    && chown -R 10001:10001 /opt/forge-agent

USER 10001:10001
WORKDIR /workspace

ENTRYPOINT ["forge"]
