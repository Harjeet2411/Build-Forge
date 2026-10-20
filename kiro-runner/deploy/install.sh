#!/usr/bin/env bash
# Installs the Build Forge runner, its MCP server and the buildforge-fixer Kiro agent on the Kiro CLI instance.
#   sudo RUN_USER=ubuntu bash kiro-runner/deploy/install.sh
# RUN_USER = the Linux user that is logged in to kiro-cli (default: ubuntu).
set -euo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${RUN_USER:-ubuntu}"
INSTALL_DIR=/opt/buildforge-runner
ENV_FILE=/etc/buildforge-runner/runner.env
PYTHON="$(command -v python3)"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[ -n "$RUN_HOME" ] || { echo "user $RUN_USER does not exist" >&2; exit 1; }

mkdir -p "$INSTALL_DIR" /etc/buildforge-runner
rm -rf "$INSTALL_DIR/kiro"
cp -r "$HERE/kiro" "$INSTALL_DIR/kiro"
cp "$HERE/runner.py" "$HERE/buildforge_mcp.py" "$INSTALL_DIR/"
chown -R "$RUN_USER" "$INSTALL_DIR"

# The Kiro agent definition goes where kiro-cli looks for the user's agents (~/.kiro/agents/).
mkdir -p "$RUN_HOME/.kiro/agents"
sed -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" -e "s#__PYTHON__#$PYTHON#g" \
  "$HERE/kiro/agents/buildforge-fixer.json" > "$RUN_HOME/.kiro/agents/buildforge-fixer.json"
chown -R "$RUN_USER" "$RUN_HOME/.kiro"

if [ ! -f "$ENV_FILE" ]; then
  sed "s/^BF_RUNNER_SECRET=$/BF_RUNNER_SECRET=$(openssl rand -hex 32)/" "$HERE/runner.env.example" > "$ENV_FILE"
  echo "Created $ENV_FILE with a new shared secret (copy it to BF_RUNNER_SECRET on the Jenkins server)."
fi
# systemd's PATH is minimal; kiro-cli often lives in ~/.local/bin, so record where RUN_USER's shell finds it.
KIRO_PATH="$(sudo -u "$RUN_USER" bash -lc 'command -v kiro-cli' 2>/dev/null || true)"
if [ -n "$KIRO_PATH" ] && ! grep -q '^BF_KIRO_BIN=' "$ENV_FILE"; then
  echo "BF_KIRO_BIN=$KIRO_PATH" >> "$ENV_FILE"
  echo "kiro-cli found at $KIRO_PATH (saved as BF_KIRO_BIN)."
fi
chmod 600 "$ENV_FILE"; chown "$RUN_USER" "$ENV_FILE"

sed -e "s#__INSTALL_DIR__#$INSTALL_DIR#g" -e "s#__RUN_USER__#$RUN_USER#g" -e "s#__PYTHON__#$PYTHON#g" \
  "$HERE/deploy/buildforge-runner.service" > /etc/systemd/system/buildforge-runner.service
systemctl daemon-reload
systemctl enable buildforge-runner >/dev/null
systemctl restart buildforge-runner
sleep 1; systemctl --no-pager --lines=5 status buildforge-runner || true
[ -n "$KIRO_PATH" ] || echo "WARNING: kiro-cli was not found for $RUN_USER; set BF_KIRO_BIN=<full path> in $ENV_FILE."
echo
echo "Next: test the agent by hand as $RUN_USER (see INSTALL.md, part A, step A4), then: curl http://127.0.0.1:8796/healthz"
