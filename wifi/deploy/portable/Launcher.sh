#!/usr/bin/env bash
# Helen — Portable USB launcher (Linux/macOS side)
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

OS="$(uname -s)"

prompt() {
  echo
  read -rp "Press Enter to continue..."
}

while true; do
  clear
  cat <<EOF
=====================================================
  Helen LAN Suite - Portable Deployment Launcher
=====================================================

  Detected: $OS $(uname -m)
  Location: $PWD

  Choose what to install:

    [1] Helen-Server         (Linux ELF + systemd + firewall)
    [2] Helen-Server         (macOS source bundle)
    [3] Helen-Rendezvous     (Linux)
    [4] Helen Desktop        (.deb / AppImage / .tar.gz)
    [5] Run health check
    [6] Backup existing install
    [7] Restore from backup

    [0] Exit

EOF
  read -rp "Enter selection: " choice

  case "$choice" in
    1)
      echo "Installing Helen-Server (Linux)..."
      sudo bash scripts/install-server.sh linux/helen-server-linux-1.0.0.tar.gz
      prompt
      ;;
    2)
      echo "Installing Helen-Server (macOS)..."
      cd macos
      tar xzf helen-server-macos-1.0.0.tar.gz
      cd helen-server-macos-1.0.0
      ./install.sh
      cd ../..
      prompt
      ;;
    3)
      echo "Installing Helen-Rendezvous (Linux)..."
      sudo bash scripts/install-rendezvous.sh linux/helen-rendezvous-linux-1.0.2.tar.gz
      prompt
      ;;
    4)
      echo "Available client packages:"
      ls -1 desktop/
      read -rp "Enter filename: " pkg
      case "$pkg" in
        *.deb)      sudo dpkg -i "desktop/$pkg" || sudo apt-get -f install -y ;;
        *.AppImage) sudo install -m 755 "desktop/$pkg" /usr/local/bin/helen-desktop ;;
        *.tar.gz)   sudo tar xzf "desktop/$pkg" -C /opt ;;
        *.dmg)      open "desktop/$pkg" ;;
        *)          echo "Unsupported package format" ;;
      esac
      prompt
      ;;
    5)
      bash scripts/health-check.sh
      prompt
      ;;
    6)
      sudo bash scripts/backup.sh /tmp/helen-backups
      prompt
      ;;
    7)
      ls -1 /tmp/helen-backups/ 2>/dev/null || ls -1 /var/backups/helen/
      read -rp "Enter backup filename: " bak
      sudo bash scripts/restore.sh "$bak"
      prompt
      ;;
    0)
      exit 0
      ;;
  esac
done
