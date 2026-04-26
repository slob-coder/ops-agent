FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    git openssh-client sshpass curl procps && \
    rm -rf /var/lib/apt/lists/* && \
    DOCKER_VER="27.5.1" && \
    curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VER}.tgz" \
        | tar xz --strip-components=1 -C /usr/local/bin docker/docker && \
    docker --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN pip install --no-cache-dir -e . && \
    mkdir -p /data/notebook/config /root/.ssh && \
    git config --global user.name "OpsAgent" && \
    git config --global user.email "agent@ops" && \
    echo "StrictHostKeyChecking=no" >> /root/.ssh/config

# 可选：安装 Notebook 扩展包
# 构建时通过 --build-arg 传入，不传则跳过（使用 Basic Notebook）
# 私有仓库：通过 GIT_TOKEN 传入 GitHub PAT 或 SSH key
#   docker compose build --build-arg NOTEBOOK_EXT=git+https://github.com/org/repo.git --build-arg GIT_TOKEN=ghp_xxx
#   或使用 SSH: --build-arg NOTEBOOK_EXT=git+ssh://git@github.com/org/repo.git --mount=type=ssh
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

# 入口脚本区分 demo 和正常模式
COPY scripts/docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
