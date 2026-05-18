#!/usr/bin/env bash
# Build Helen-Admin into a distributable --onedir PyInstaller bundle.
set -euo pipefail

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)"
SERVER_SRC="$(cd "${here}/../../CommClient-Server" && pwd)"
cd "${SERVER_SRC}"

PY=${PY:-python3}
if [[ ! -d "venv" ]]; then
    "${PY}" -m venv venv
fi
# shellcheck source=/dev/null
source venv/bin/activate
pip install -q --upgrade pip
pip install -q -r requirements.txt
pip install -q pyinstaller pywebview

echo "==> pyinstaller for admin"
pyinstaller --noconfirm admin_app/Helen-Admin.spec

echo
ls -lh dist/Helen-Admin/Helen-Admin 2>/dev/null
echo
echo "Next: cd Linux-Admin && sudo ./install.sh"
