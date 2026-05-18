#!/usr/bin/env bash
# Pre-flight checks. Run BEFORE install.sh, or called automatically by it.
# Exits 0 on pass, 1 on any blocker, 2 on warning-only issues.
set -euo pipefail

PASS=0; WARN=0; FAIL=0
CYAN=$'\033[36m'; GREEN=$'\033[32m'; RED=$'\033[31m'; YELLOW=$'\033[33m'; RST=$'\033[0m'
_ok()   { PASS=$((PASS+1)); printf '  %s✓%s %s\n' "${GREEN}" "${RST}" "$*"; }
_warn() { WARN=$((WARN+1)); printf '  %s⚠%s %s\n' "${YELLOW}" "${RST}" "$*"; }
_fail() { FAIL=$((FAIL+1)); printf '  %s✗%s %s\n' "${RED}" "${RST}" "$*"; }
_section() { printf '\n%s▸ %s%s\n' "${CYAN}" "$*" "${RST}"; }

# 1. Kernel + init
_section "system basics"
kver="$(uname -r)"
if [[ -d /run/systemd/system ]]; then _ok "systemd detected"
else _fail "systemd not running — this installer targets systemd distros only"; fi

if [[ "$(uname -s)" = "Linux" ]]; then _ok "kernel: ${kver}"
else _fail "not Linux — aborting"; fi

# 2. Required binaries
_section "required tools"
for t in curl python3 openssl systemctl journalctl useradd install; do
    if command -v "$t" >/dev/null 2>&1; then _ok "$t"
    else _fail "missing $t"; fi
done

# 3. Optional but recommended
_section "recommended tools"
for t in jq logrotate firewall-cmd ufw; do
    if command -v "$t" >/dev/null 2>&1; then _ok "$t"
    else _warn "no $t (install for better experience)"; fi
done

# 4. Python version
_section "python"
pyv="$(python3 -c 'import sys;print("%d.%d" % sys.version_info[:2])' 2>/dev/null || echo 0)"
if awk "BEGIN{exit !( ${pyv} >= 3.9 )}"; then _ok "python ${pyv}"
else _fail "python ${pyv} — need 3.9+"; fi

# 5. Disk space
_section "disk"
free_mb="$(df -m /var 2>/dev/null | awk 'NR==2{print $4}' || echo 0)"
if [[ ${free_mb:-0} -gt 1024 ]]; then _ok "/var free: ${free_mb} MB"
else _warn "/var free: ${free_mb} MB (recommend >1 GB)"; fi

# 6. Memory
_section "memory"
mem_mb="$(free -m 2>/dev/null | awk 'NR==2{print $2}' || echo 0)"
if [[ ${mem_mb:-0} -gt 1024 ]]; then _ok "ram: ${mem_mb} MB"
else _warn "ram: ${mem_mb} MB (recommend >1 GB)"; fi

# 7. Ports
_section "ports (want 3000, 3443, 5173 free)"
for p in 3000 3443 5173; do
    if command -v ss >/dev/null 2>&1; then
        if ss -lntuH "( sport = :${p} )" 2>/dev/null | grep -q :${p}; then
            _warn "port ${p} already in use"
        else _ok "port ${p} free"; fi
    else
        _warn "ss not installed — skipping port check"
        break
    fi
done

# 8. UDP receive buffer (SFU/relay benefit from large buffers)
_section "kernel tunables"
for k in net.core.rmem_max net.core.wmem_max net.core.netdev_max_backlog; do
    v="$(sysctl -n "${k}" 2>/dev/null || echo 0)"
    case "${k}" in
        net.core.rmem_max|net.core.wmem_max)
            if [[ ${v} -ge 4194304 ]]; then _ok "${k} = ${v}"
            else _warn "${k} = ${v} (low — consider 4194304 for media)"; fi;;
        *)
            if [[ ${v} -ge 1000 ]]; then _ok "${k} = ${v}"
            else _warn "${k} = ${v}"; fi;;
    esac
done

# 9. File descriptor limit
_section "limits"
hard="$(ulimit -Hn)"
if [[ ${hard} -ge 65536 ]]; then _ok "open file limit: ${hard}"
else _warn "open file limit: ${hard} (systemd unit bumps to 65536)"; fi

# 10. SELinux / AppArmor
_section "mandatory access control"
if command -v getenforce >/dev/null 2>&1; then
    enf="$(getenforce 2>/dev/null || echo 0)"
    case "${enf}" in
        Enforcing)  _warn "SELinux enforcing — helen-server may need policy adjustments"; ;;
        Permissive) _ok "SELinux permissive";;
        *)          _ok "SELinux disabled or absent";;
    esac
fi
if command -v aa-status >/dev/null 2>&1; then
    if aa-status --enabled 2>/dev/null; then _ok "AppArmor enabled (profile available in apparmor/)"
    else _ok "AppArmor not enforcing"; fi
fi

# Summary
echo
echo "─────────────────────────────────────────────"
printf "  %spass:%s %d   %swarn:%s %d   %sfail:%s %d\n" \
    "${GREEN}" "${RST}" "${PASS}" \
    "${YELLOW}" "${RST}" "${WARN}" \
    "${RED}" "${RST}" "${FAIL}"
echo "─────────────────────────────────────────────"

if [[ ${FAIL} -gt 0 ]]; then exit 1
elif [[ ${WARN} -gt 0 ]]; then exit 2
else exit 0; fi
