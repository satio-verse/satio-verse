#!/bin/bash
# wm-harden.sh - first-batch hardening of the WorldModel server (Ubuntu Server 24.04 LTS, DRAFT).
#
# Run it over ssh as the user "ubuntu" (the console-created key is bound to that user; decided 2026-11-14).
# It re-runs itself through "sudo -n". ubuntu has passwordless sudo - this is checked first: if "sudo -n true"
# fails the script STOPS and says why. It never edits any sudo configuration.
# There is exactly one identity. Whatever runs as ubuntu is root-equivalent (README section 2).
#
#   bash ~/wm-init/wm-harden.sh --check      read-only: report what WOULD change (exit 10 if something would).
#                                            It writes NOTHING on the server: no log directory or file, no lock file,
#                                            no temporary file, no backup. Everything is printed on standard output.
#   bash ~/wm-init/wm-harden.sh --apply      do it (safe to run again; unchanged things are skipped)
#
# What --apply does (in this order; each step backs up the files it changes first):
#   packages    fail2ban python3-systemd chrony ufw unattended-upgrades (only the missing ones, without recommended packages)
#   sshd        /etc/ssh/sshd_config.d/00-wm-hardening.conf, "sshd -t" before reload, effective values re-checked
#   ufw         allow the ssh port FIRST, default deny incoming / allow outgoing, enable
#   fail2ban    sshd jail: mode normal, maxretry 5, findtime 10m, bantime 10m, backend systemd
#   unattended-upgrades   security pockets only, never reboot automatically
#   chrony      time1..time5.tencentyun.com; systemd-timesyncd is stopped and disabled if it is still around
#   journald    stored on disk, size limited
#   directories /srv/worldmodel/{releases,shared} (owner ubuntu, group wm) and the no-login system user "wm"
#   tools       installs wm-harden, wm-snapshot, wm-snapshot-diff, wm-verify into /usr/local/sbin (root-owned)
#   security updates   only with --security-updates-now (see Options)
# Two state snapshots (before-harden / after-harden, "wm-snapshot --save") are taken around --apply.
#
# It never touches the Tencent Cloud components (tat_agent, monitor agent, host-security agent).
# It never changes the SSH port. See README.md for how to do that by hand.
#
# Options:
#   --check | --apply            exactly one is required
#   --no-snapshots               do not take the before/after snapshots
#   --rebaseline                 record the current /etc/ssh/sshd_config as the new baseline
#   --security-updates-now       with --apply: once everything else is done, install the security updates that piled up
#                                NOW: apt-get update; unattended-upgrade --dry-run -d (prints what would be installed);
#                                unattended-upgrade -v; then report whether /var/run/reboot-required exists.
#                                It never reboots; whether and when to reboot is the owner's decision.
#                                Without this option the pile is installed by the next apt-daily-upgrade run (every day
#                                at 06:00 server time plus up to 60 minutes of random delay).
# Environment (all optional; they are passed on through sudo):
#   WM_SSH_PORT            ssh port the firewall must allow (default 22)
#   WM_CHRONY_KEEP_POOLS   1 = keep the stock Ubuntu NTP pools next to the Tencent servers (default 0)
#   WM_JOURNAL_MAX_USE     journald SystemMaxUse (default 1G)
#   WM_JOURNAL_KEEP_FREE   journald SystemKeepFree (default 2G)
#   WM_JOURNAL_RETENTION   journald MaxRetentionSec (default 90day)
#   WM_FAIL2BAN_IGNOREIP   extra addresses fail2ban must never ban, space separated (default none)
#   WM_ALLOW_OTHER_OS      1 = do not stop on an OS other than Ubuntu 24.04 (default 0)
#
# Log (--apply only): /var/log/wm-ops/harden.log     Backups (--apply only): /var/backups/wm-ops/<UTC time>/
# Exit: 0 ok (--check: nothing to change) | 10 --check found changes to make | 1 error | 2 usage | 3 refused for safety
# Output is plain ASCII on purpose.

set -Eeuo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
unset BASH_ENV ENV CDPATH
IFS=$' \t\n'
umask 022

readonly TOOL_VERSION="0.2-draft"
SELF=$(readlink -f -- "${BASH_SOURCE[0]}")
SELF_DIR=$(dirname -- "$SELF")
ORIG_ARGS=("$@")

# The one identity this draft supports (decided 2026-11-14): the key bound in the console belongs to ubuntu.
readonly OPS_USER=ubuntu

readonly LOG_DIR=/var/log/wm-ops
readonly LOGFILE="$LOG_DIR/harden.log"
readonly STATE_DIR=/var/lib/wm-ops
readonly BASELINE_DIR=/var/lib/wm-ops/baseline
readonly BACKUP_ROOT=/var/backups/wm-ops
readonly BIN_DIR=/usr/local/sbin
readonly SRV_ROOT=/srv/worldmodel
readonly SSHD_MAIN=/etc/ssh/sshd_config
readonly SSHD_DROPIN=/etc/ssh/sshd_config.d/00-wm-hardening.conf
readonly F2B_FILE=/etc/fail2ban/jail.d/90-wm-sshd.local
readonly UU_FILE=/etc/apt/apt.conf.d/52wm-unattended-upgrades
readonly CHRONY_CONF=/etc/chrony/chrony.conf
readonly CHRONY_SOURCES=/etc/chrony/sources.d/wm-tencent.sources
readonly JOURNALD_FILE=/etc/systemd/journald.conf.d/00-wm.conf
readonly LOGROTATE_FILE=/etc/logrotate.d/wm-ops

ACTION=""
TAKE_SNAPSHOTS=1
REBASELINE=0
SEC_NOW=0
SSH_PORT="${WM_SSH_PORT:-22}"
CHANGES=0
WARNINGS=0
WROTE=0
BACKUP_DIR=""
TMPFILES=()

usage() {
  sed -n '2,/^# Output is plain ASCII/p' "$SELF" | sed 's/^# \{0,1\}//'
}

# ----------------------------------------------------------------- arguments
while (( $# )); do
  case "$1" in
    --check) ACTION=check ;;
    --apply) ACTION=apply ;;
    --no-snapshots) TAKE_SNAPSHOTS=0 ;;
    --rebaseline) REBASELINE=1 ;;
    --security-updates-now) SEC_NOW=1 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift || true
done

if [[ -z $ACTION ]]; then echo "ERROR: give exactly one of --check or --apply" >&2; usage >&2; exit 2; fi

if [[ ! $SSH_PORT =~ ^[0-9]+$ ]] || (( SSH_PORT < 1 || SSH_PORT > 65535 )); then echo "ERROR: WM_SSH_PORT must be 1-65535" >&2; exit 2; fi
if [[ ! ${WM_JOURNAL_MAX_USE:-1G} =~ ^[0-9]+[KMGT]?$ ]]; then echo "ERROR: bad WM_JOURNAL_MAX_USE" >&2; exit 2; fi
if [[ ! ${WM_JOURNAL_KEEP_FREE:-2G} =~ ^[0-9]+[KMGT]?$ ]]; then echo "ERROR: bad WM_JOURNAL_KEEP_FREE" >&2; exit 2; fi
if [[ ! ${WM_JOURNAL_RETENTION:-90day} =~ ^[0-9]+(s|min|h|day|week|month|year)$ ]]; then echo "ERROR: bad WM_JOURNAL_RETENTION" >&2; exit 2; fi
if [[ -n ${WM_FAIL2BAN_IGNOREIP:-} && ! ${WM_FAIL2BAN_IGNOREIP} =~ ^[0-9A-Fa-f:./\ ]+$ ]]; then echo "ERROR: bad WM_FAIL2BAN_IGNOREIP" >&2; exit 2; fi

# ----------------------------------------------------------------- become root (through sudo, never by editing sudo)
if (( EUID != 0 )); then
  if ! command -v sudo >/dev/null 2>&1; then echo "ERROR: not root and sudo is not installed" >&2; exit 3; fi
  if [[ $(id -un) != "$OPS_USER" ]]; then
    echo "ERROR: run this script as $OPS_USER (it is running as $(id -un))" >&2
    exit 3
  fi
  if ! sudo -n true 2>/dev/null; then
    echo "ERROR: 'sudo -n true' failed, so $(id -un) has no passwordless sudo." >&2
    echo "  This script will NOT change anyone's sudo configuration." >&2
    echo "  Option: run it as root from the console web terminal (README section 8)." >&2
    exit 3
  fi
  # sudo resets the environment, so the WM_* settings are handed over explicitly.
  fwd=()
  for _v in WM_SSH_PORT WM_CHRONY_KEEP_POOLS WM_JOURNAL_MAX_USE WM_JOURNAL_KEEP_FREE WM_JOURNAL_RETENTION WM_FAIL2BAN_IGNOREIP WM_ALLOW_OTHER_OS; do
    if [[ -n ${!_v:-} ]]; then fwd+=("$_v=${!_v}"); fi
  done
  exec sudo -n env "${fwd[@]}" bash "$SELF" "${ORIG_ARGS[@]}"
fi

# ----------------------------------------------------------------- logging helpers (root from here on)
# --check writes nothing on the server: no log directory, no log file, no lock file, no temporary file.
# Its messages only go to standard output. --apply also keeps the log file.
if [[ $ACTION == apply ]]; then
  install -d -m 0750 -o root -g root "$LOG_DIR"
  touch "$LOGFILE"
  chmod 0640 "$LOGFILE"
fi

_log() {
  local lvl=$1 line
  shift
  line="$(date -u +%Y-%m-%dT%H:%M:%SZ) [$lvl] $*"
  printf '%s\n' "$line"
  if [[ $ACTION == apply ]]; then printf '%s\n' "$line" >>"$LOGFILE"; fi
}
info()   { _log INFO "$@"; }
ok()     { _log OK "$@"; }
change() { _log CHANGE "$@"; }
warn()   { WARNINGS=$((WARNINGS + 1)); _log WARN "$@"; }
error()  { _log ERROR "$@" >&2; }
fatal()  { local rc=$1; shift; error "$*"; exit "$rc"; }

on_error() {
  local rc=$1 line=$2
  error "unexpected failure (status $rc) at line $line: $BASH_COMMAND"
}
cleanup() {
  local f
  for f in "${TMPFILES[@]}"; do rm -f -- "$f"; done
}
trap 'on_error $? $LINENO' ERR
trap cleanup EXIT

# Only --apply takes the lock (it creates the lock file). --check takes none, so it creates no file.
if [[ $ACTION == apply ]]; then
  exec 9>/run/lock/wm-harden.lock
  if ! flock -n 9; then fatal 1 "another wm-harden is running (lock /run/lock/wm-harden.lock)"; fi
fi

# ----------------------------------------------------------------- generic helpers
is_check() { [[ $ACTION == check ]]; }

pkg_installed() { dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q 'install ok installed'; }

# run COMMAND...   apply: run it, keep the output in the log, show the tail on failure.  check: only say what it would do.
run() {
  local tmp rc=0
  if is_check; then info "would run: $*"; return 0; fi
  info "run: $*"
  tmp=$(mktemp)
  "$@" >"$tmp" 2>&1 || rc=$?
  cat "$tmp" >>"$LOGFILE"
  if (( rc != 0 )); then
    warn "command failed with status $rc: $*"
    tail -n 20 "$tmp" | sed 's/^/    | /'
  fi
  rm -f "$tmp"
  return "$rc"
}

backup_file() {
  local f=$1
  if is_check; then return 0; fi
  [[ -e $f ]] || return 0
  if [[ -z $BACKUP_DIR ]]; then
    BACKUP_DIR="$BACKUP_ROOT/$(date -u +%Y%m%dT%H%M%SZ)"
    install -d -m 0700 -o root -g root "$BACKUP_ROOT" "$BACKUP_DIR"
  fi
  if [[ ! -e "$BACKUP_DIR$f" ]]; then
    mkdir -p -- "$BACKUP_DIR$(dirname -- "$f")"
    cp -a -- "$f" "$BACKUP_DIR$f"
    info "backup: $f -> $BACKUP_DIR$f"
  fi
}

# Undo a write_file that turned out to be wrong.
rollback_file() {
  local f=$1
  if [[ -n $BACKUP_DIR && -e "$BACKUP_DIR$f" ]]; then
    cp -a -- "$BACKUP_DIR$f" "$f"
    warn "restored $f from $BACKUP_DIR$f"
  else
    rm -f -- "$f"
    warn "removed $f (it did not exist before this run)"
  fi
}

# write_file DEST MODE OWNER:GROUP < content
# Sets WROTE=1 when the file differs (or, in check mode, would be written). Backs up the old file first.
# The new content is held in memory (the added x keeps trailing newlines) and compared through a pipe, so --check
# creates no temporary file. Only --apply writes: a temporary file, a backup, then the destination.
write_file() {
  local dest=$1 mode=$2 owner=$3 content tmp want
  # stat prints the mode without the leading zero (644), the callers write 0644: compare the numbers, not the text.
  want="$(printf '%o' "$((8#$mode))") $owner"
  content=$(cat; printf x)
  content=${content%x}
  WROTE=0
  if [[ -e $dest ]] && cmp -s <(printf '%s' "$content") "$dest" && [[ $(stat -c '%a %U:%G' "$dest") == "$want" ]]; then
    ok "unchanged: $dest"
    return 0
  fi
  WROTE=1
  CHANGES=$((CHANGES + 1))
  if is_check; then
    change "would write: $dest (mode $mode, owner $owner)"
    return 0
  fi
  backup_file "$dest"
  tmp=$(mktemp)
  TMPFILES+=("$tmp")
  printf '%s' "$content" >"$tmp"
  install -D -m "$mode" -o "${owner%%:*}" -g "${owner##*:}" "$tmp" "$dest"
  change "wrote: $dest"
  rm -f -- "$tmp"
}

ensure_dir() {
  local d=$1 mode=$2 owner=$3 group=$4
  if [[ -d $d ]] && [[ $(stat -c '%a %U %G' "$d") == "$mode $owner $group" ]]; then
    ok "directory ok: $d ($mode $owner:$group)"
    return 0
  fi
  CHANGES=$((CHANGES + 1))
  if is_check; then change "would set up directory $d ($mode $owner:$group)"; return 0; fi
  install -d -o "$owner" -g "$group" "$d"
  chmod "$mode" "$d"
  change "directory: $d ($mode $owner:$group)"
}

ops_home() { getent passwd "$OPS_USER" | cut -d: -f6; }

# The ops user must have a usable key and sshd-acceptable permissions, otherwise key-only login locks us out.
ops_key_problem() {
  local home keys p perm
  home=$(ops_home)
  [[ -d $home ]] || { echo "home directory $home does not exist"; return 0; }
  [[ -f $home/.ssh/authorized_keys ]] || { echo "$home/.ssh/authorized_keys does not exist"; return 0; }
  if ! keys=$(ssh-keygen -l -f "$home/.ssh/authorized_keys" 2>/dev/null) || [[ -z $keys ]]; then
    echo "$home/.ssh/authorized_keys holds no valid public key"
    return 0
  fi
  for p in "$home" "$home/.ssh" "$home/.ssh/authorized_keys"; do
    perm=$(stat -c '%a' "$p")
    if (( (8#$perm & 8#022) != 0 )); then echo "$p is writable by group/others (mode $perm); sshd would ignore the key"; return 0; fi
    if [[ $(stat -c '%U' "$p") != "$OPS_USER" ]]; then echo "$p is not owned by $OPS_USER; sshd would ignore the key"; return 0; fi
  done
  return 0
}

take_snapshot() {
  local label=$1 out
  local -a bin
  if is_check || (( ! TAKE_SNAPSHOTS )); then return 0; fi
  if [[ -f $SELF_DIR/wm-snapshot.sh ]]; then
    bin=(bash "$SELF_DIR/wm-snapshot.sh")
  elif [[ -x $BIN_DIR/wm-snapshot ]]; then
    bin=("$BIN_DIR/wm-snapshot")
  else
    warn "no snapshot script found next to wm-harden or in $BIN_DIR; skipping snapshot $label"
    return 0
  fi
  if out=$("${bin[@]}" --save "$label" 2>&1); then info "snapshot $label: $out"; else warn "snapshot $label failed: $out"; fi
}

# ----------------------------------------------------------------- preflight
preflight() {
  local os_id os_ver f problem
  info "wm-harden $TOOL_VERSION action=$ACTION ops_user=$OPS_USER"
  info "sha256 $(sha256sum <"$SELF" | cut -d' ' -f1)  $(basename -- "$SELF")"
  for f in wm-snapshot.sh wm-snapshot-diff.sh wm-verify.sh; do
    if [[ -f $SELF_DIR/$f ]]; then info "sha256 $(sha256sum <"$SELF_DIR/$f" | cut -d' ' -f1)  $f"; fi
  done
  os_id=$(. /etc/os-release && printf '%s' "${ID:-unknown}")
  os_ver=$(. /etc/os-release && printf '%s' "${VERSION_ID:-unknown}")
  if [[ $os_id != ubuntu || $os_ver != 24.04 ]]; then
    if [[ ${WM_ALLOW_OTHER_OS:-0} == 1 ]]; then
      warn "tested only on Ubuntu 24.04, this is $os_id $os_ver (WM_ALLOW_OTHER_OS=1, continuing)"
    else
      fatal 3 "this draft was written for Ubuntu 24.04, this is $os_id $os_ver (set WM_ALLOW_OTHER_OS=1 to override)"
    fi
  fi
  command -v sshd >/dev/null 2>&1 || fatal 3 "sshd is not installed"
  command -v systemctl >/dev/null 2>&1 || fatal 3 "systemctl is not available"
  if ! id "$OPS_USER" >/dev/null 2>&1; then fatal 3 "user $OPS_USER does not exist"; fi
  problem=$(ops_key_problem)
  if [[ -n $problem ]]; then
    fatal 3 "refusing to disable password login: $problem"
  fi
  ok "ops user $OPS_USER has a usable ssh key"
}

# ----------------------------------------------------------------- step: packages
step_packages() {
  local pkgs=(fail2ban python3-systemd chrony ufw unattended-upgrades) missing=() p
  info "== step: packages (${pkgs[*]})"
  for p in "${pkgs[@]}"; do
    if pkg_installed "$p"; then ok "package installed: $p"; else missing+=("$p"); fi
  done
  if (( ${#missing[@]} == 0 )); then return 0; fi
  CHANGES=$((CHANGES + 1))
  if is_check; then
    change "would install: ${missing[*]}"
    info "apt simulation, recommended packages left out (Inst = would be installed, Remv = would be removed; uses the package lists that are already there):"
    # Debug::NoLocking and the two empty cache options keep the simulation from locking or writing anything, even as root.
    { apt-get -s --no-install-recommends -o Debug::NoLocking=1 -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= install "${missing[@]}" 2>&1 || true; } | grep -E '^(Inst|Remv|E:) ' | sed 's/^/    | /' || true
    return 0
  fi
  export DEBIAN_FRONTEND=noninteractive
  # needrestart is installed on this image: list only, never restart services (sshd, tat_agent) behind our back.
  export NEEDRESTART_MODE=l
  run apt-get update -o DPkg::Lock::Timeout=300
  # --no-install-recommends: only what the approved list names (fail2ban would otherwise pull in whois and python3-pyinotify).
  run apt-get install -y --no-install-recommends -o DPkg::Lock::Timeout=300 -o Dpkg::Options::=--force-confold "${missing[@]}"
  change "installed: ${missing[*]}"
}

# ----------------------------------------------------------------- step: sshd
sshd_dropin_content() {
  cat <<'EOF'
# Managed by wm-harden.sh. Do not edit by hand; change the script and run it again.
#
# sshd keeps the FIRST value it reads for a keyword, and /etc/ssh/sshd_config.d/*.conf is included at the top
# of sshd_config. This file sorts before 50-cloud-init.conf and 60-cloudimg-settings.conf, so it wins over
# later drop-ins and over the main file. The console "online reset password" rewrites the main file only;
# that is why these settings live here.
#
# Deliberately NOT set here:
#   AllowUsers / AllowGroups   would also block the user of the console web-terminal SSH channel
#   AllowTcpForwarding         stays on: until the ICP filing is through, the services are tested over an ssh tunnel
#   MaxSessions                stays at its default (10): several ssh / scp connections can be open at the same time
#   Subsystem sftp             stays: scp uses the SFTP protocol, so the SFTP subsystem must keep working
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PubkeyAuthentication yes
PermitEmptyPasswords no
X11Forwarding no
AllowAgentForwarding no
LoginGraceTime 30
EOF
}

report_other_dropins() {
  local f b line bad
  for f in /etc/ssh/sshd_config.d/*.conf; do
    [[ -e $f ]] || continue
    b=$(basename "$f")
    if [[ $b == 00-wm-hardening.conf ]]; then continue; fi
    bad=$(grep -Eis '^[[:space:]]*(passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication|permitrootlogin|pubkeyauthentication|allowusers|allowgroups)[[:space:]]+' "$f" || true)
    if [[ -z $bad ]]; then
      info "other drop-in: $b (no authentication directives)"
      continue
    fi
    while IFS= read -r line; do info "other drop-in: $b: $(printf '%s' "$line" | tr -s '[:space:]' ' ')"; done <<<"$bad"
    if grep -Eiqs '^[[:space:]]*(passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication)[[:space:]]+yes' "$f"; then
      if [[ $b < 00-wm-hardening.conf ]]; then
        warn "$b enables password login AND sorts before 00-wm-hardening.conf, so it would win; rename or remove it"
      else
        warn "$b enables password login; it sorts after 00-wm-hardening.conf, so ours wins (first value wins). Check with: sshd -T"
      fi
    fi
  done
}

reload_sshd() {
  local unit=""
  if systemctl cat ssh.service >/dev/null 2>&1; then unit=ssh.service
  elif systemctl cat sshd.service >/dev/null 2>&1; then unit=sshd.service
  fi
  if [[ -z $unit ]]; then warn "no ssh service unit found; sshd was not reloaded"; return 0; fi
  if systemctl is-active --quiet "$unit"; then
    run systemctl reload "$unit"
    change "reloaded $unit"
  else
    info "$unit is not running (socket activation): the new configuration applies at the next connection"
  fi
}

step_sshd() {
  local eff kv
  info "== step: sshd drop-in ($SSHD_DROPIN)"
  [[ -f $SSHD_MAIN ]] || fatal 3 "$SSHD_MAIN not found"
  if ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' "$SSHD_MAIN"; then
    fatal 3 "$SSHD_MAIN has no 'Include /etc/ssh/sshd_config.d/*.conf'; a drop-in would be ignored. Not changing the main file."
  fi
  report_other_dropins
  write_file "$SSHD_DROPIN" 0644 root:root < <(sshd_dropin_content)
  if (( WROTE )) && ! is_check; then
    if ! sshd -t >>"$LOGFILE" 2>&1; then
      rollback_file "$SSHD_DROPIN"
      fatal 1 "sshd -t rejected the new configuration; the drop-in was rolled back"
    fi
    eff=$(sshd -T 2>/dev/null || true)
    for kv in "passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin no" "pubkeyauthentication yes"; do
      if ! grep -qx "$kv" <<<"$eff"; then
        rollback_file "$SSHD_DROPIN"
        fatal 1 "after the change sshd -T does not show '$kv'; the drop-in was rolled back"
      fi
    done
    ok "sshd -t passed and sshd -T shows the four required values"
    reload_sshd
    info "REMINDER: open a SECOND ssh window and log in with your key BEFORE closing this one."
  fi
  if ! grep -Eq '^[[:space:]]*Subsystem[[:space:]]+sftp' "$SSHD_MAIN"; then
    warn "no 'Subsystem sftp' line in $SSHD_MAIN: scp (it uses the SFTP protocol) will not work"
  fi
}

# ----------------------------------------------------------------- step: ufw
step_ufw() {
  local ports=() p added st
  info "== step: ufw (ssh allowed first, default deny incoming / allow outgoing; Tencent agents untouched)"
  if ! pkg_installed ufw; then
    if is_check; then CHANGES=$((CHANGES + 1)); change "would configure ufw after it is installed"; return 0; fi
    fatal 1 "ufw is not installed"
  fi
  # Every TCP port sshd is configured for or really listens on is allowed, so enabling ufw cannot cut this session.
  mapfile -t ports < <({
    printf '%s\n' "$SSH_PORT"
    sshd -T 2>/dev/null | awk '$1 == "port" { print $2 }'
    ss -H -ltnp 2>/dev/null | awk '/"sshd"/ { n = split($4, a, ":"); print a[n] }'
  } | sort -un)
  added=$(ufw show added 2>/dev/null || true)
  for p in "${ports[@]}"; do
    if grep -Eq "^ufw allow ${p}/tcp( |\$)" <<<"$added"; then
      ok "ufw rule present: allow ${p}/tcp"
    else
      CHANGES=$((CHANGES + 1))
      if is_check; then change "would run: ufw allow ${p}/tcp"; else run ufw allow "${p}/tcp" comment "wm ssh"; change "ufw: allowed ${p}/tcp"; fi
    fi
  done
  if grep -q '^DEFAULT_INPUT_POLICY="DROP"' /etc/default/ufw && grep -q '^DEFAULT_OUTPUT_POLICY="ACCEPT"' /etc/default/ufw; then
    ok "ufw defaults: deny incoming, allow outgoing"
  else
    CHANGES=$((CHANGES + 1))
    if is_check; then
      change "would run: ufw default deny incoming; ufw default allow outgoing"
    else
      run ufw default deny incoming
      run ufw default allow outgoing
      change "ufw defaults set: deny incoming, allow outgoing"
    fi
  fi
  st=$(ufw status 2>&1 | head -n 1 || true)
  if [[ $st == "Status: active" ]]; then
    ok "ufw is active"
  else
    CHANGES=$((CHANGES + 1))
    if is_check; then change "would run: ufw --force enable"; else run ufw --force enable; change "ufw enabled"; fi
  fi
}

# ----------------------------------------------------------------- step: fail2ban
fail2ban_content() {
  cat <<'EOF'
# Managed by wm-harden.sh. Do not edit by hand; change the script and run it again.
# mode normal is the default of fail2ban's sshd filter: only failed logins are counted. The ddos, extra and
# aggressive modes also count connections that drop before login and protocol-level probes (upstream filter.d/sshd.conf).
# Behind a shared proxy exit address one wrong key can ban everybody on it for bantime:
# wait it out, use the web terminal, or add the address with WM_FAIL2BAN_IGNOREIP and run wm-harden --apply.
[sshd]
enabled  = true
mode     = normal
backend  = systemd
maxretry = 5
findtime = 10m
bantime  = 10m
EOF
  if [[ -n ${WM_FAIL2BAN_IGNOREIP:-} ]]; then
    printf 'ignoreip = 127.0.0.1/8 ::1 %s\n' "$WM_FAIL2BAN_IGNOREIP"
  fi
}

wait_fail2ban() {
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if fail2ban-client ping >/dev/null 2>&1; then return 0; fi
    sleep 1
  done
  return 1
}

step_fail2ban() {
  info "== step: fail2ban (sshd jail, mode normal, maxretry 5, findtime 10m, bantime 10m, backend systemd)"
  if ! pkg_installed fail2ban; then
    if is_check; then CHANGES=$((CHANGES + 1)); change "would configure fail2ban after it is installed"; return 0; fi
    fatal 1 "fail2ban is not installed"
  fi
  write_file "$F2B_FILE" 0644 root:root < <(fail2ban_content)
  if is_check; then
    if systemctl is-active --quiet fail2ban; then ok "fail2ban is active"; else CHANGES=$((CHANGES + 1)); change "would enable and start fail2ban"; fi
    return 0
  fi
  if (( WROTE )); then
    if ! fail2ban-client -t >>"$LOGFILE" 2>&1; then
      rollback_file "$F2B_FILE"
      fatal 1 "fail2ban-client -t rejected the configuration; the file was rolled back"
    fi
    run systemctl enable fail2ban
    run systemctl restart fail2ban
  elif ! systemctl is-active --quiet fail2ban; then
    run systemctl enable --now fail2ban
    change "fail2ban started"
  fi
  if wait_fail2ban; then
    info "fail2ban jail sshd:"
    fail2ban-client status sshd 2>&1 | sed 's/^/    | /' | tee -a "$LOGFILE" || true
  else
    warn "fail2ban did not answer within 10 seconds; check: journalctl -u fail2ban"
  fi
}

# ----------------------------------------------------------------- step: unattended-upgrades
uu_content() {
  cat <<'EOF'
// Managed by wm-harden.sh. Loaded after 50unattended-upgrades, so it wins.
// Security pockets only. Never reboot by itself: a pending reboot shows up in wm-verify.
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
#clear Unattended-Upgrade::Allowed-Origins;
Unattended-Upgrade::Allowed-Origins {
        "${distro_id}:${distro_codename}-security";
        "${distro_id}ESMApps:${distro_codename}-apps-security";
        "${distro_id}ESM:${distro_codename}-infra-security";
};
Unattended-Upgrade::Automatic-Reboot "false";
EOF
}

step_unattended() {
  info "== step: unattended-upgrades (security pockets only, no automatic reboot)"
  write_file "$UU_FILE" 0644 root:root < <(uu_content)
  if (( WROTE )); then
    info "NOTE: the image restricts unattended-upgrades to the plain release pocket, so no security update has been installed so far (baseline 2026-11-14: 216 upgradable packages)."
    if (( SEC_NOW )); then
      info "NOTE: --security-updates-now was given: the pile is installed at the end of this run (step: security updates now), not by the timer."
    else
      info "NOTE: without --security-updates-now the pile is installed by the next apt-daily-upgrade run (every day at 06:00 server time plus up to 60 minutes of random delay)."
    fi
    info "NOTE: the pile can contain a kernel, openssh or systemd update. A reboot is never done automatically; whether and when to reboot is the owner's decision. Take a console snapshot before --apply."
  fi
  if (( WROTE )) && ! is_check; then
    if ! apt-config dump >/dev/null 2>>"$LOGFILE"; then
      rollback_file "$UU_FILE"
      fatal 1 "apt-config rejected $UU_FILE; it was rolled back"
    fi
  fi
  if systemctl is-enabled --quiet apt-daily.timer 2>/dev/null && systemctl is-enabled --quiet apt-daily-upgrade.timer 2>/dev/null; then
    ok "apt-daily and apt-daily-upgrade timers are enabled"
  else
    CHANGES=$((CHANGES + 1))
    if is_check; then change "would enable apt-daily.timer and apt-daily-upgrade.timer"; else run systemctl enable --now apt-daily.timer apt-daily-upgrade.timer; change "apt timers enabled"; fi
  fi
}

# ----------------------------------------------------------------- step: chrony
chrony_sources_content() {
  cat <<'EOF'
# Managed by wm-harden.sh. Tencent Cloud NTP servers (check notes D3-a: time1..time5.tencentyun.com).
server time1.tencentyun.com iburst
server time2.tencentyun.com iburst
server time3.tencentyun.com iburst
server time4.tencentyun.com iburst
server time5.tencentyun.com iburst
EOF
}

step_chrony() {
  info "== step: chrony (Tencent servers; stock Ubuntu pools commented out unless WM_CHRONY_KEEP_POOLS=1)"
  if ! pkg_installed chrony; then
    if is_check; then CHANGES=$((CHANGES + 1)); change "would configure chrony after it is installed (installing it also removes systemd-timesyncd, package conflict)"; return 0; fi
    fatal 1 "chrony is not installed"
  fi
  # The Tencent Cloud image already ships chrony with time1..time5.tencentyun.com in chrony.conf (baseline 2026-11-14).
  # A working configuration is not touched: the five servers are only added when one of them is missing.
  local wrote_sources=0 wrote_conf=0 missing=0 n others
  for n in 1 2 3 4 5; do
    if ! grep -Eqs "^[[:space:]]*server[[:space:]]+time${n}\.tencentyun\.com([[:space:]]|\$)" "$CHRONY_CONF" /etc/chrony/conf.d/*.conf /etc/chrony/sources.d/*.sources; then missing=1; fi
  done
  if (( missing )); then
    write_file "$CHRONY_SOURCES" 0644 root:root < <(chrony_sources_content)
    wrote_sources=$WROTE
  else
    ok "time1..time5.tencentyun.com are already configured as servers; the chrony source configuration is left as it is"
  fi
  if [[ ${WM_CHRONY_KEEP_POOLS:-0} != 1 && -f $CHRONY_CONF ]]; then
    # Only the stock pool/server lines are switched off; the Tencent servers stay.
    others=$(grep -E '^[[:space:]]*(pool|server)[[:space:]]' "$CHRONY_CONF" || true)
    if [[ -n $others ]] && grep -Evq 'tencentyun\.com' <<<"$others"; then
      write_file "$CHRONY_CONF" "$(stat -c '%a' "$CHRONY_CONF")" root:root \
        < <(sed -E -e '/^[[:space:]]*(pool|server)[[:space:]]+[^[:space:]]*tencentyun\.com([[:space:]]|$)/b' -e 's/^([[:space:]]*(pool|server)[[:space:]].*)$/# wm-disabled: \1/' "$CHRONY_CONF")
      wrote_conf=$WROTE
    else
      ok "no active pool/server line other than the Tencent servers in $CHRONY_CONF"
    fi
  fi
  if ! is_check; then
    if (( wrote_sources || wrote_conf )); then
      run systemctl enable chrony
      run systemctl restart chrony
      sleep 3
      info "chronyc sources:"
      { chronyc sources 2>&1 || true; } | sed 's/^/    | /' | tee -a "$LOGFILE"
    elif ! systemctl is-active --quiet chrony; then
      run systemctl enable --now chrony
      change "chrony started"
    fi
  elif ! systemctl is-active --quiet chrony; then
    CHANGES=$((CHANGES + 1))
    change "would enable and start chrony"
  fi
  # systemd-timesyncd and chrony must not both run. Installing chrony normally removes timesyncd (package conflict).
  if systemctl cat systemd-timesyncd.service >/dev/null 2>&1; then
    if systemctl is-active --quiet systemd-timesyncd || systemctl is-enabled --quiet systemd-timesyncd 2>/dev/null; then
      CHANGES=$((CHANGES + 1))
      if is_check; then change "would stop and disable systemd-timesyncd"; else run systemctl disable --now systemd-timesyncd; change "systemd-timesyncd stopped and disabled (package left installed, not masked)"; fi
    else
      ok "systemd-timesyncd is installed but not running or enabled"
    fi
  else
    ok "systemd-timesyncd is not installed"
  fi
}

# ----------------------------------------------------------------- step: journald
journald_content() {
  cat <<EOF
# Managed by wm-harden.sh. Journal on disk, size limited, one file per day.
[Journal]
Storage=persistent
SystemMaxUse=${WM_JOURNAL_MAX_USE:-1G}
SystemKeepFree=${WM_JOURNAL_KEEP_FREE:-2G}
SystemMaxFileSize=64M
MaxFileSec=1day
MaxRetentionSec=${WM_JOURNAL_RETENTION:-90day}
EOF
}

step_journald() {
  local restart=0
  info "== step: journald (persistent, size limited)"
  write_file "$JOURNALD_FILE" 0644 root:root < <(journald_content)
  restart=$WROTE
  if [[ -d /var/log/journal ]]; then
    ok "/var/log/journal exists"
  else
    CHANGES=$((CHANGES + 1))
    if is_check; then
      change "would create /var/log/journal"
    else
      install -d -m 2755 -o root -g systemd-journal /var/log/journal
      chmod 2755 /var/log/journal
      change "created /var/log/journal"
      restart=1
    fi
  fi
  if ! is_check && (( restart )); then
    run systemd-tmpfiles --create --prefix=/var/log/journal
    run systemctl restart systemd-journald
    run journalctl --flush
  fi
}

# ----------------------------------------------------------------- step: directories and the service user
step_users_dirs() {
  info "== step: /srv/worldmodel and the no-login service user wm"
  if getent passwd wm >/dev/null; then
    ok "user wm exists (shell $(getent passwd wm | cut -d: -f7))"
  else
    CHANGES=$((CHANGES + 1))
    if is_check; then
      change "would create system user wm (no home directory, shell nologin, password locked)"
    else
      run useradd --system --user-group --no-create-home --home-dir "$SRV_ROOT" --shell /usr/sbin/nologin --comment "WorldModel service user" wm
      change "created system user wm"
    fi
  fi
  if getent group wm >/dev/null || ! is_check; then
    ensure_dir "$SRV_ROOT" 2750 "$OPS_USER" wm
    ensure_dir "$SRV_ROOT/releases" 2750 "$OPS_USER" wm
    ensure_dir "$SRV_ROOT/shared" 2770 "$OPS_USER" wm
  else
    CHANGES=$((CHANGES + 1))
    change "would create $SRV_ROOT, releases/ and shared/ owned by $OPS_USER:wm (modes 2750, 2750, 2770) after user wm exists"
  fi
}

# ----------------------------------------------------------------- step: install the tools
logrotate_content() {
  cat <<'EOF'
# Managed by wm-harden.sh
/var/log/wm-ops/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    create 0640 root root
}
EOF
}

install_tool() {
  local src=$1 dst=$2
  if [[ ! -f $src ]]; then info "source not found ($src): leaving $dst as it is"; return 0; fi
  write_file "$dst" 0755 root:root <"$src"
}

step_tools() {
  info "== step: tools in $BIN_DIR (owned by root)"
  install_tool "$SELF_DIR/wm-harden.sh" "$BIN_DIR/wm-harden"
  install_tool "$SELF_DIR/wm-snapshot.sh" "$BIN_DIR/wm-snapshot"
  install_tool "$SELF_DIR/wm-snapshot-diff.sh" "$BIN_DIR/wm-snapshot-diff"
  install_tool "$SELF_DIR/wm-verify.sh" "$BIN_DIR/wm-verify"
  write_file "$LOGROTATE_FILE" 0644 root:root < <(logrotate_content)
  if is_check; then
    if [[ -e $STATE_DIR ]]; then ok "$STATE_DIR exists"; else CHANGES=$((CHANGES + 1)); change "would create $STATE_DIR (0700, root)"; fi
  else
    install -d -m 0700 -o root -g root "$STATE_DIR" "$BASELINE_DIR"
  fi
  # Baseline of the main sshd_config: wm-verify compares against it (the console password reset rewrites it).
  if [[ ! -e $BASELINE_DIR/sshd_config.sha256 ]] || (( REBASELINE )); then
    CHANGES=$((CHANGES + 1))
    if is_check; then
      change "would record the current $SSHD_MAIN as baseline"
    else
      cp -a -- "$SSHD_MAIN" "$BASELINE_DIR/sshd_config"
      printf '%s  %s\n' "$(sha256sum <"$SSHD_MAIN" | cut -d' ' -f1)" "$SSHD_MAIN" >"$BASELINE_DIR/sshd_config.sha256"
      chmod 0600 "$BASELINE_DIR/sshd_config" "$BASELINE_DIR/sshd_config.sha256"
      change "recorded baseline of $SSHD_MAIN"
    fi
  else
    ok "baseline of $SSHD_MAIN already recorded"
  fi
}

# ----------------------------------------------------------------- step: security updates now (only with --security-updates-now)
# Runs last, after everything else is in place. Order: apt-get update, unattended-upgrade --dry-run -d (what would be
# installed), unattended-upgrade -v (the real run), then the reboot report. It never reboots.
# The real run goes into a transient systemd unit when systemd-run exists, so a dropped ssh connection cannot cut dpkg
# in half; its output is read back from the journal afterwards.
step_security_updates_now() {
  local dry list n rc=0 unit=wm-security-updates
  if (( ! SEC_NOW )); then return 0; fi
  info "== step: security updates now (--security-updates-now)"
  if is_check; then
    CHANGES=$((CHANGES + 1))
    info "would run, in this order: apt-get update; unattended-upgrade --dry-run -d (prints what would be installed); unattended-upgrade -v; then report /var/run/reboot-required. It never reboots."
    return 0
  fi
  command -v unattended-upgrade >/dev/null 2>&1 || fatal 1 "unattended-upgrade is not installed"
  export DEBIAN_FRONTEND=noninteractive
  export NEEDRESTART_MODE=l
  run apt-get update -o DPkg::Lock::Timeout=300
  dry=$(unattended-upgrade --dry-run -d 2>&1) || rc=$?
  printf '%s\n' "$dry" >>"$LOGFILE"
  list=$(sed -n 's/^.*Packages that will be upgraded: //p' <<<"$dry" | tail -n 1)
  if [[ -n $list ]]; then
    n=$(wc -w <<<"$list")
    info "dry run: $n package(s) would be upgraded: $list"
  else
    info "dry run: no package would be upgraded. Last lines of its output: $(grep -E 'No packages found|Allowed origins are' <<<"$dry" | tail -n 2 | tr '\n' ' ')"
  fi
  if (( rc != 0 )); then warn "unattended-upgrade --dry-run exited with status $rc (full output in $LOGFILE)"; fi
  if command -v systemd-run >/dev/null 2>&1; then
    info "installing in the transient unit $unit (survives a dropped ssh connection); follow it with: journalctl -u $unit"
    run systemd-run --unit="$unit" --wait --collect --quiet --setenv=DEBIAN_FRONTEND=noninteractive --setenv=NEEDRESTART_MODE=l unattended-upgrade -v || true
    { journalctl -u "$unit" --no-pager -o cat 2>&1 | tail -n 15 | sed 's/^/    | /' | tee -a "$LOGFILE"; } || true
  else
    run unattended-upgrade -v || true
  fi
  if [[ -e /var/run/reboot-required ]]; then
    info "REBOOT REQUIRED: /var/run/reboot-required exists (packages: $(tr '\n' ' ' </var/run/reboot-required.pkgs 2>/dev/null || true)). Nothing was rebooted; whether and when to reboot is the owner's decision."
  else
    info "no reboot required: /var/run/reboot-required does not exist"
  fi
}

# ----------------------------------------------------------------- main
summary() {
  info "== summary"
  info "changes ($( is_check && echo 'needed' || echo 'made' )): $CHANGES   warnings: $WARNINGS   log: $LOGFILE"
  if is_check; then
    if (( CHANGES == 0 )); then ok "nothing to change"; exit 0; fi
    info "check mode: $CHANGES change(s) would be made. Run again with --apply."
    exit 10
  fi
  if [[ -n $BACKUP_DIR ]]; then info "backups of every replaced file: $BACKUP_DIR"; fi
  info "NEXT 1: open a NEW ssh window and log in with your key. Only then close this one."
  info "NEXT 2: sudo $BIN_DIR/wm-verify          (every line should say PASS; MANUAL lines are for you in the console)"
  info "NEXT 3: console (you): close port 80 in the Lighthouse firewall, take a snapshot, set budget and traffic alerts."
  info "REMINDER: $OPS_USER keeps passwordless sudo, so anything run as $OPS_USER is root-equivalent (README section 2)."
  exit 0
}

preflight
take_snapshot before-harden
step_packages
step_sshd
step_ufw
step_fail2ban
step_unattended
step_chrony
step_journald
step_users_dirs
step_tools
step_security_updates_now
take_snapshot after-harden
summary
