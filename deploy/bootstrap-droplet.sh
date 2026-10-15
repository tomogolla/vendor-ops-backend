#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "Run as root: sudo bash deploy/bootstrap-droplet.sh /path/to/backend/.env" >&2
    exit 1
fi

ENV_SOURCE=${1:-}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)

if ! id vendorops >/dev/null 2>&1; then
    useradd --system --home /opt/vendor-ops --shell /usr/sbin/nologin vendorops
fi

install -d -m 0755 /opt/vendor-ops/releases /var/www/vendor-ops/releases
install -d -m 0750 -o root -g vendorops /etc/vendor-ops

if [[ ! -f /etc/vendor-ops/backend.env ]]; then
    if [[ -z ${ENV_SOURCE} || ! -f ${ENV_SOURCE} ]]; then
        echo "Provide the existing backend .env on first run." >&2
        exit 1
    fi
    install -m 0640 -o root -g vendorops "${ENV_SOURCE}" /etc/vendor-ops/backend.env
fi

install -m 0644 "${SCRIPT_DIR}/vendor-ops-backend.service" /etc/systemd/system/vendor-ops-backend.service
# The email worker units are installed here but only enabled by deploy-backend.sh, once a release exists.
install -m 0644 "${SCRIPT_DIR}/vendor-ops-send-emails.service" /etc/systemd/system/vendor-ops-send-emails.service
install -m 0644 "${SCRIPT_DIR}/vendor-ops-send-emails.timer" /etc/systemd/system/vendor-ops-send-emails.timer
if [[ ! -f /etc/nginx/sites-available/ops.thegoodflea.com ]]; then
    install -m 0644 "${SCRIPT_DIR}/nginx-ops.thegoodflea.com.conf" /etc/nginx/sites-available/ops.thegoodflea.com
fi
ln -sfn /etc/nginx/sites-available/ops.thegoodflea.com /etc/nginx/sites-enabled/ops.thegoodflea.com
rm -f /etc/nginx/sites-enabled/default

systemctl daemon-reload
systemctl enable vendor-ops-backend.service
nginx -t
systemctl reload nginx

echo "Droplet bootstrap complete. Deploy both repositories, then configure TLS with Certbot."
