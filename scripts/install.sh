#!/bin/sh
# 一键安装脚本(示意性,生产前请人工 review)
set -e
INSTALL_DIR=/opt/ops-agent
NOTEBOOK_DIR=/var/lib/ops-agent/notebook

mkdir -p "$INSTALL_DIR" "$NOTEBOOK_DIR"
cp -r ./*.py ./prompts ./templates "$INSTALL_DIR/"
cp ops-agent.service /etc/systemd/system/
id ops >/dev/null 2>&1 || useradd -r -d "$INSTALL_DIR" -s /usr/sbin/nologin ops
chown -R ops:ops "$INSTALL_DIR" "$NOTEBOOK_DIR"
systemctl daemon-reload
systemctl enable ops-agent
echo "OK. 启动: systemctl start ops-agent"
