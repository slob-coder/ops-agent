FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client sshpass curl procps && \
    rm -rf /var/lib/apt/lists/* && \
    DOCKER_VER="27.5.1" && \
    curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VER}.tgz" \
        | tar xz --strip-components=1 -C /usr/local/bin docker/docker && \
    docker --version && \
    COMPOSE_VER="v2.36.1" && \
    mkdir -p /usr/local/lib/docker/cli-plugins && \
    curl -fsSL "https://github.com/docker/compose/releases/download/${COMPOSE_VER}/docker-compose-linux-x86_64" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose && \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose && \
    docker compose version && \
    printf '#!/bin/sh\nexec docker compose "$@"\n' > /usr/local/bin/docker-compose && \
    chmod +x /usr/local/bin/docker-compose

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN pip install --no-cache-dir -e . && \
    mkdir -p /data/notebook/config /root/.ssh && \
    git config --global user.name "OpsAgent" && \
    git config --global user.email "agent@ops" && \
    echo "StrictHostKeyChecking=no" >> /root/.ssh/config

ARG NOTEBOOK_EXT=""
ARG GIT_TOKEN=""
RUN if [ -n "$NOTEBOOK_EXT" ]; then \
        echo "Installing notebook extension: $NOTEBOOK_EXT" && \
        if [ -n "$GIT_TOKEN" ]; then \
            EXT_URL=$(echo "$NOTEBOOK_EXT" | sed "s|https://|https://${GIT_TOKEN}@|"); \
            pip install --no-cache-dir "$EXT_URL"; \
        else \
            pip install --no-cache-dir "$NOTEBOOK_EXT"; \
        fi; \
    else \
        echo "No notebook extension specified, using Basic Notebook"; \
    fi

VOLUME /data/notebook

COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
