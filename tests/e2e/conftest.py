"""Shared fixtures for E2E tests.

Token resolution order:
1. SG_TEST_TOKEN env var
2. /tmp/swarmgrid-e2e-token.txt cache file
3. Generate locally if JWT_SECRET is in .env
4. Generate via fly ssh (slow, may timeout)

To set up: run `make test-token` or manually:
  fly ssh console --app swarmgrid-api -C 'python3 -c "..."' > /tmp/swarmgrid-e2e-token.txt
"""
import os
import subprocess
import time
import pytest

API_URL = "https://swarmgrid-api.fly.dev"
SITE_URL = "https://swarmgrid.org"
TOKEN_CACHE = "/tmp/swarmgrid-e2e-token.txt"
TOKEN_MAX_AGE = 3600 * 24  # 24 hours (tokens are valid 72h)


def _generate_token():
    """Get a valid JWT for E2E tests."""
    # 1. Env var
    env_token = os.environ.get("SG_TEST_TOKEN", "")
    if env_token.startswith("ey"):
        return env_token

    # 2. Cache file
    if os.path.exists(TOKEN_CACHE):
        age = time.time() - os.path.getmtime(TOKEN_CACHE)
        if age < TOKEN_MAX_AGE:
            token = open(TOKEN_CACHE).read().strip()
            if token.startswith("ey"):
                return token

    # 3. Generate locally with JWT_SECRET from .env
    from pathlib import Path
    env_file = Path(__file__).resolve().parent.parent.parent / ".env"
    jwt_secret = None
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("JWT_SECRET="):
                jwt_secret = line.split("=", 1)[1].strip()
    if jwt_secret:
        os.environ["JWT_SECRET"] = jwt_secret
        import jwt as pyjwt
        from datetime import datetime, timedelta, UTC
        payload = {"sub": "1", "github_login": "starshipagentic", "exp": datetime.now(UTC) + timedelta(hours=72)}
        token = pyjwt.encode(payload, jwt_secret, algorithm="HS256")
        with open(TOKEN_CACHE, "w") as f:
            f.write(token)
        return token

    # 4. Fall back to fly SSH
    result = subprocess.run(
        [
            os.path.expanduser("~/.fly/bin/flyctl"),
            "ssh", "console",
            "--app", "swarmgrid-api",
            "-C",
            'python3 -c "from swarmgrid.cloud.auth import create_jwt; from types import SimpleNamespace; print(create_jwt(SimpleNamespace(id=1, github_login=\'starshipagentic\')))"',
        ],
        capture_output=True, text=True, timeout=60,
    )
    for line in result.stdout.strip().splitlines():
        if line.startswith("ey"):
            token = line.strip()
            with open(TOKEN_CACHE, "w") as f:
                f.write(token)
            return token
    raise RuntimeError(
        f"No test token available. Set SG_TEST_TOKEN env var, add JWT_SECRET to .env, "
        f"or run: fly ssh console --app swarmgrid-api -C '...' > {TOKEN_CACHE}"
    )


@pytest.fixture(scope="session")
def auth_token():
    return _generate_token()


@pytest.fixture(scope="session")
def api_url():
    return API_URL


@pytest.fixture(scope="session")
def site_url():
    return SITE_URL
