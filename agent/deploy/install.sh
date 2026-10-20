#!/usr/bin/env bash
# Installs the Build Forge service on the Jenkins server. From the unzipped folder: sudo bash agent/deploy/install.sh
# Safe to run again for updates: it keeps /etc/buildforge/buildforge.env, the database and the incident logs.
set -euo pipefail
HERE="$(cd "$(dirname "$0")/../.." && pwd)"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' || { echo "Python 3.8+ is required" >&2; exit 1; }
id buildforge >/dev/null 2>&1 || useradd --system --home /var/lib/buildforge --shell /usr/sbin/nologin buildforge
mkdir -p /opt/buildforge /etc/buildforge /var/lib/buildforge /var/log/buildforge/incidents
rm -rf /opt/buildforge/agent && cp -r "$HERE/agent" /opt/buildforge/agent
find /opt/buildforge/agent -name __pycache__ -prune -exec rm -rf {} +
chown -R buildforge:buildforge /var/lib/buildforge /var/log/buildforge
chmod 750 /var/log/buildforge
if [ ! -f /etc/buildforge/buildforge.env ]; then
  sed "s/^BF_SHARED_SECRET=$/BF_SHARED_SECRET=$(openssl rand -hex 32)/" "$HERE/agent/agent.env.example" > /etc/buildforge/buildforge.env
  echo "Created /etc/buildforge/buildforge.env with a new shared secret. Fill in BF_RUNNER_URL and BF_RUNNER_SECRET."
fi
chmod 600 /etc/buildforge/buildforge.env; chown buildforge:buildforge /etc/buildforge/buildforge.env
cp "$HERE/agent/deploy/buildforge.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable buildforge >/dev/null
systemctl restart buildforge
sleep 1; systemctl --no-pager --lines=5 status buildforge || true
echo
echo "Plugin secret (Manage Jenkins > System > Build Forge):  sudo grep BF_SHARED_SECRET /etc/buildforge/buildforge.env"
echo "Check:  sudo -u buildforge bash -c 'set -a; . /etc/buildforge/buildforge.env; cd /opt/buildforge/agent; python3 -m buildforge_agent check'"
