#!/usr/bin/env bash
# Helen-Server — macOS native installer
# يعمل على Intel و Apple Silicon (M1/M2/M3/M4) بدون أي اتصال إنترنت بعد التثبيت
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================="
echo "  Helen-Server 1.0.0 — macOS Installer"
echo "============================================="
echo

# 1. تحقق من Python 3.11+
if ! command -v python3 >/dev/null 2>&1; then
  echo "[ERROR] Python 3 غير مثبّت."
  echo "        ثبّت Python 3.11+ من: https://www.python.org/downloads/macos/"
  echo "        أو عبر Homebrew: brew install python@3.12"
  exit 1
fi

PYVER=$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
PYMAJ=$(python3 -c 'import sys; print(sys.version_info.major)')
PYMIN=$(python3 -c 'import sys; print(sys.version_info.minor)')

if [ "$PYMAJ" -lt 3 ] || { [ "$PYMAJ" -eq 3 ] && [ "$PYMIN" -lt 11 ]; }; then
  echo "[ERROR] Python $PYVER غير مدعوم. مطلوب Python 3.11 أو أحدث."
  exit 1
fi
echo "[OK] Python $PYVER detected"

# 2. أنشئ virtual environment
if [ ! -d "venv" ]; then
  echo "[..] Creating virtual environment..."
  python3 -m venv venv
fi
echo "[OK] venv ready"

# 3. تفعيل venv وتثبيت deps
# shellcheck disable=SC1091
source venv/bin/activate
echo "[..] Upgrading pip..."
python -m pip install --upgrade pip --quiet

echo "[..] Installing Helen-Server dependencies..."
pip install -r requirements.txt --quiet

# 4. أنشئ .env إذا غير موجود
if [ ! -f ".env" ] && [ -f ".env.example" ]; then
  cp .env.example .env
  # أنشئ JWT_SECRET قوي
  JWT_SECRET=$(python -c 'import secrets; print(secrets.token_hex(32))')
  if grep -q "^JWT_SECRET=" .env; then
    sed -i.bak "s|^JWT_SECRET=.*|JWT_SECRET=$JWT_SECRET|" .env && rm -f .env.bak
  else
    echo "JWT_SECRET=$JWT_SECRET" >> .env
  fi
  echo "[OK] .env created with random JWT_SECRET"
fi

# 5. أنشئ data directories
mkdir -p data data/backups data/uploads data/avatars logs

echo
echo "============================================="
echo "  ✅ Installation complete"
echo "============================================="
echo
echo "  لتشغيل السيرفر:"
echo "    ./start.sh"
echo
echo "  أو لتثبيته كخدمة launchd (يبدأ تلقائياً):"
echo "    ./install-service.sh"
echo
