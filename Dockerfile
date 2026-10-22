FROM python:3.12-slim
ARG INSTALL_AI=0
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DORMGUARD_DATA=/app/data
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt requirements-ai.txt ./
RUN pip install --no-cache-dir -r requirements.txt && if [ "$INSTALL_AI" = "1" ]; then pip install --no-cache-dir -r requirements-ai.txt; fi
RUN groupadd --gid 10001 dormguard && useradd --uid 10001 --gid 10001 --no-create-home dormguard && mkdir /app/data && chown 10001:10001 /app/data
COPY --chown=10001:10001 dormguard ./dormguard
COPY --chown=10001:10001 migrations ./migrations
COPY --chown=10001:10001 scripts ./scripts
USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=20s --timeout=3s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=2)" || exit 1
CMD ["python", "-m", "uvicorn", "dormguard.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
