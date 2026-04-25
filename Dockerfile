FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client sshpass curl procps docker.io && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN pip install --no-cache-dir -e . && \
    mkdir -p /data/notebook/config /root/.ssh && \
    git config --global user.name "OpsAgent" && \
    git config --global user.email "agent@ops" && \
    echo "StrictHostKeyChecking=no" >> /root/.ssh/config

VOLUME /data/notebook

# 入口脚本区分 demo 和正常模式
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
