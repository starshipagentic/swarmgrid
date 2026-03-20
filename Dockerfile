FROM python:3.13-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml .
COPY src/ src/
RUN pip install --no-cache-dir .

RUN mkdir -p /data

EXPOSE 8080

CMD ["python", "-m", "uvicorn", "swarmgrid.cloud.app:app", "--host", "0.0.0.0", "--port", "8080"]
