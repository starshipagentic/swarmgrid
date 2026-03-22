FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

RUN mkdir -p /data/.ssh && chmod 700 /data/.ssh

EXPOSE 8080

ENV DATABASE_URL="sqlite:////data/swarmgrid.db"

CMD ["python", "-m", "uvicorn", "swarmgrid.cloud.app:app", "--host", "0.0.0.0", "--port", "8080"]
