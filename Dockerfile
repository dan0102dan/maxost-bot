FROM python:3.13-slim AS build
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1
RUN apt-get update && apt-get install -y --no-install-recommends git ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt pyproject.toml ./
COPY maxost ./maxost
RUN python -m venv /opt/venv && /opt/venv/bin/pip install .

FROM python:3.13-slim AS runtime
ENV PATH="/opt/venv/bin:$PATH" PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
RUN groupadd --gid 10001 maxost && useradd --uid 10001 --gid 10001 --no-create-home maxost
COPY --from=build /opt/venv /opt/venv
USER 10001:10001
WORKDIR /app
HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD ["python", "-m", "maxost.healthcheck"]
CMD ["python", "-m", "maxost.main"]
