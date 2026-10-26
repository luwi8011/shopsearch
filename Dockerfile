FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1 PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN pip install "scrapling[fetchers,rag]==0.5" "mcp==1.30.0" "httpx>=0.27" "selectolax>=0.3" "uvicorn" \
 && python -m patchright install --with-deps chromium \
 && python -c "from tld.utils import update_tld_names; update_tld_names(fail_silently=True)" \
 && apt-get clean && rm -rf /var/lib/apt/lists/*
# Home Assistant WebSocket API (helpers, dashboard edits)
RUN pip install "websockets>=13"

WORKDIR /app
COPY app/ /app/
EXPOSE 8765
CMD ["python", "server.py"]
