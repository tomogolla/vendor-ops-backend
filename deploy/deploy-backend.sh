#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
    echo "This script must run as root." >&2
    exit 1
fi

ARTIFACT=${1:?Usage: deploy-backend.sh ARTIFACT RELEASE_ID}
RELEASE_ID=${2:?Usage: deploy-backend.sh ARTIFACT RELEASE_ID}
RELEASE_ROOT=/opt/vendor-ops/releases
RELEASE_DIR=${RELEASE_ROOT}/backend-${RELEASE_ID}
CURRENT_LINK=/opt/vendor-ops/backend-current
VENV=/opt/vendor-ops/venv
ENV_FILE=/etc/vendor-ops/backend.env
EMAIL_TIMER=vendor-ops-send-emails.timer
EMAIL_SERVICE=vendor-ops-send-emails.service

[[ -f ${ARTIFACT} ]] || { echo "Artifact not found: ${ARTIFACT}" >&2; exit 1; }
[[ -f ${ENV_FILE} ]] || { echo "Missing ${ENV_FILE}; run bootstrap-droplet.sh first." >&2; exit 1; }

install -d -m 0755 "${RELEASE_DIR}"
tar -xzf "${ARTIFACT}" -C "${RELEASE_DIR}"
ln -sfn "${ENV_FILE}" "${RELEASE_DIR}/.env"
chown -R vendorops:www-data "${RELEASE_DIR}"

if [[ ! -x ${VENV}/bin/python ]]; then
    python3 -m venv "${VENV}"
fi
"${VENV}/bin/pip" install --disable-pip-version-check --upgrade pip
"${VENV}/bin/pip" install --disable-pip-version-check -r "${RELEASE_DIR}/requirements.txt"

sudo -u vendorops "${VENV}/bin/python" "${RELEASE_DIR}/manage.py" check --deploy

# Pause the email timer while the schema changes. A run already in progress is left to finish
# (stopping it mid-send could leave a message in `sending`). If the deploy fails from here on, the
# trap restarts the timer so the previous release keeps sending.
if systemctl list-unit-files "${EMAIL_TIMER}" >/dev/null 2>&1 && systemctl is-enabled --quiet "${EMAIL_TIMER}"; then
    systemctl stop "${EMAIL_TIMER}"
    trap 'systemctl start "${EMAIL_TIMER}" >/dev/null 2>&1 || true' EXIT
fi

sudo -u vendorops "${VENV}/bin/python" "${RELEASE_DIR}/manage.py" migrate --noinput
sudo -u vendorops "${VENV}/bin/python" "${RELEASE_DIR}/manage.py" collectstatic --noinput

ln -sfn "${RELEASE_DIR}" "${CURRENT_LINK}"
systemctl restart vendor-ops-backend.service
systemctl is-active --quiet vendor-ops-backend.service
for attempt in {1..15}; do
    if curl --fail --silent --show-error --unix-socket /run/vendor-ops/gunicorn.sock http://localhost/api/v1/health/ >/dev/null; then
        break
    fi
    if [[ ${attempt} -eq 15 ]]; then
        journalctl -u vendor-ops-backend.service -n 50 --no-pager >&2
        exit 1
    fi
    sleep 1
done

# Install or update the email worker units on every deploy (idempotent) and (re)start the timer.
# It runs `send_due_emails` every minute; nothing is sent unless EMAIL_GLOBAL_MODE and the admin
# settings allow it (both default to disabled).
install -m 0644 "${RELEASE_DIR}/deploy/${EMAIL_SERVICE}" "/etc/systemd/system/${EMAIL_SERVICE}"
install -m 0644 "${RELEASE_DIR}/deploy/${EMAIL_TIMER}" "/etc/systemd/system/${EMAIL_TIMER}"
systemctl daemon-reload
systemctl enable --now "${EMAIL_TIMER}"
trap - EXIT

find "${RELEASE_ROOT}" -mindepth 1 -maxdepth 1 -type d -name 'backend-*' -printf '%T@ %p\n' | sort -nr | tail -n +6 | cut -d' ' -f2- | xargs -r rm -rf
rm -f "${ARTIFACT}"
echo "Backend release ${RELEASE_ID} deployed."
