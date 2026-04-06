FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client grep procps curl && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN mkdir -p /data/notebook && \
    git config --global user.name "OpsAgent" && \
    git config --global user.email "agent@ops"

ENV OPS_LLM_PROVIDER=anthropic
ENV OPS_LLM_MODEL=claude-sonnet-4-20250514
# OPS_LLM_API_KEY 通过 docker run -e 传入

ENTRYPOINT ["python", "main.py", "--notebook", "/data/notebook"]
