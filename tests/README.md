# tests

Scripts that run against fakes and a temp data dir. Home Assistant is always stubbed, so nothing is created in
HA and no phone is notified. `APP_DIR` points at the code (default `/app`, the path inside the image).

Locally (Python 3.12 with `httpx` and `websockets`), from the repo root:

    APP_DIR=shopsearch/app python tests/test_hunts.py
    APP_DIR=shopsearch/app python tests/test_routing.py
    APP_DIR=shopsearch/app LLM_URL=http://<llm>/v1 LLM_MODEL=<model> python tests/test_review_live.py   # real LLM, ~30-60 s

Inside a plain-Docker container:

    docker cp tests shopsearch:/tmp/tests
    docker exec shopsearch python /tmp/tests/test_hunts.py
