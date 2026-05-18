#!/usr/bin/env bash
# Build Helen-Server into a distributable --onedir PyInstaller bundle
# on the current Linux host.
#
# Output:  ../CommClient-Server/dist/Helen-Server/Helen-Server
#
# Compatibility:
#   Build on the OLDEST glibc you need to support. E.g., build on
#   Ubuntu 20.04 / CentOS 7, and the binary runs on everything newer.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
SERVER_SRC="$(cd "${here}/../../CommClient-Server" && pwd)"
cd "${SERVER_SRC}"

PY=${PY:-python3}
VENV_DIR="${SERVER_SRC}/venv"

if [[ ! -d "${VENV_DIR}" ]]; then
    echo "==> creating venv"
    "${PY}" -m venv "${VENV_DIR}"
fi
# shellcheck source=/dev/null
source "${VENV_DIR}/bin/activate"

echo "==> installing build deps"
pip install -q --upgrade pip wheel
pip install -q -r requirements.txt
pip install -q pyinstaller

echo "==> running PyInstaller"
pyinstaller --noconfirm CommClient-Server.spec

echo
echo "==> output: ${SERVER_SRC}/dist/Helen-Server/"
ls -lh "${SERVER_SRC}/dist/Helen-Server/Helen-Server"
echo
echo "Next:"
echo "  cd Linux-Server && sudo ./install.sh"
