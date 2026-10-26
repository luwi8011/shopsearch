# tests

Scripts run inside the running container (it has every dependency) against fakes and a temp data dir.
Home Assistant is always stubbed, so nothing is created in HA and no phone is notified.

    docker cp tests shopsearch:/tmp/tests
    docker exec shopsearch python /tmp/tests/test_hunts.py
    docker exec shopsearch python /tmp/tests/test_routing.py
    docker exec shopsearch python /tmp/tests/test_review_live.py   # calls the real local LLM, ~30-60 s

To test uncommitted code without rebuilding: `docker cp app shopsearch:/tmp/app` and add `-e APP_DIR=/tmp/app`
to `docker exec`.
