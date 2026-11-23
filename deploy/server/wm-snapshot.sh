#!/bin/bash
# wm-snapshot.sh - state snapshot of the server (Ubuntu Server 24.04 LTS, DRAFT). It only looks, it changes nothing.
#
# Two ways to use it:
#
#   1) Baseline / read-only look (the default). Nothing is written on the server and nothing is installed.
#        ssh wm-server 'bash -s' < wm-snapshot.sh > baseline.txt        (README shows the real line, through wm-ssh.sh)
#      Everything goes to standard output, one block per topic ("##### NN-name #####"). What needs root runs
#      through "sudo -n". If sudo -n is refused, that block gets one line starting with the tag NEED_PW below
#      (the Chinese words for "password needed") and the run goes on.
#
#   2) After hardening. wm-harden.sh installs this file as /usr/local/sbin/wm-snapshot (owner root). Then:
#        sudo wm-snapshot --save LABEL
#      writes /var/lib/wm-ops/<UTC time>-<LABEL>/NN-name.txt (directory 0700, files 0600) for wm-snapshot-diff.
#
# Options:  (none) | --stdout   mode 1
#           --save LABEL        mode 2, must run as root; LABEL = 1-40 characters from A-Z a-z 0-9 . _ -
#           -h | --help
# Exit:     0 ok | 2 usage, or --save without root | 3 a secret pattern was found (that block is withheld) | 1 other error
#
# NEVER collected, in either mode:
#   /etc/shadow, private keys, COS config files, process environments, the text of authorized_keys files
#   (only the number of key lines), the contents of /usr/local/qcloud/, /var/lib/cloud/ and /etc/cloud/.
#   No public address and no login source: no "ip addr", no curl to an echo service, no last / lastb / who,
#   no established connections, no list of banned addresses. Address-like text in firewall, ssh, sudoers, cron,
#   nginx and time-sync output is masked (<ipv4>, <ipv6>); the wildcard and loopback forms stay readable.
#   Lines that look like secrets (password, token, secret, ...) are masked in nginx -T and cron output.
#   Every block is checked once more before it is printed; if a private key block, a password hash or
#   public key text shows up, the block is withheld and the exit status is 3.
#
# Reading this script from standard input (bash -s) is safe: all work happens inside main(), which runs with
# standard input taken from /dev/null, so no command can swallow the rest of the script.
#
# Output is ASCII except for the NEED_PW tag.

set -u -o pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
unset BASH_ENV ENV CDPATH
IFS=$' \t\n'

readonly BASE=/var/lib/wm-ops
readonly SRV_ROOT=/srv/worldmodel
# Marks "sudo -n was refused". Chinese on purpose, fixed wording, easy to find with grep.
readonly NEED_PW='[需要密码]'
# A block that matches this is withheld: private key block | password hash | public key text.
readonly SECRET_RE='-----BEGIN [A-Z ]*PRIVATE KEY-----|(^|[[:space:]:])[$](1|5|6|y)[$][^[:space:]:]{8,}|ssh-(rsa|ed25519|dss|ecdsa-sha2-[a-z0-9]+)[[:space:]]+AAAA'

MODE=stdout
LABEL=""
TS=""
DIR=""
SUDO_OK=0
SECRET_HITS=0
MAX_HASH_BYTES="${WM_SNAPSHOT_MAX_HASH_BYTES:-268435456}"

usage() {
  cat <<'EOF'
Usage: wm-snapshot.sh                 print a read-only snapshot on standard output (nothing is written)
       wm-snapshot.sh --stdout        the same
       wm-snapshot.sh --save LABEL    root only: write /var/lib/wm-ops/<UTC time>-<LABEL>/NN-name.txt
LABEL: 1-40 characters from A-Z a-z 0-9 . _ -
EOF
}

# ---------------------------------------------------------------- helpers

have() { command -v "$1" >/dev/null 2>&1; }
is_root() { (( EUID == 0 )); }
can_root() { is_root || (( SUDO_OK )); }
# systemctl, never with a pager.
sctl() { systemctl --no-pager "$@"; }

detect_sudo() {
  SUDO_OK=0
  if is_root; then SUDO_OK=1; return 0; fi
  if have sudo && sudo -n true >/dev/null 2>&1; then SUDO_OK=1; fi
  return 0
}

# as_root COMMAND...   run an external command with root rights. Already root: run it. Otherwise "sudo -n".
# When sudo -n is not possible: print one line with the NEED_PW tag and carry on (status 0).
as_root() {
  if is_root; then "$@"; return; fi
  if (( SUDO_OK )); then sudo -n -- "$@"; return; fi
  printf '%s sudo -n refused (a password is needed), not collected: %s\n' "$NEED_PW" "$*"
  return 0
}

# rd COMMAND...   like as_root, but without sudo the command simply runs as the current user (a file that is not
#                 readable then shows its permission error inside the block).
rd() { if can_root; then as_root "$@"; else "$@"; fi; }

# Mask secret-looking lines. GNU sed only (Ubuntu).
redact() {
  sed -E \
    -e '/-----BEGIN [A-Z ]*PRIVATE KEY-----/,/-----END [A-Z ]*PRIVATE KEY-----/c\<REDACTED private key block>' \
    -e 's/((password|passwd|secret|token|apikey|api_key|authorization|bearer|credential)[A-Za-z0-9_-]*[[:space:]]*[:=]?[[:space:]]*).*/\1<REDACTED>/I'
}

# Mask address-like text. 0.0.0.0, 127.x.x.x, [::] and ::1 stay readable (as 0.0.0.0, 127.0.0.1, [::], ::1).
# Only used on blocks that can hold addresses; never on the package list (version numbers look like addresses).
mask_ips() {
  sed -E \
    -e ':a' -e 's/(^|[^0-9A-Za-z.:])0\.0\.0\.0([^0-9.]|$)/\1@@A4@@\2/' -e 'ta' \
    -e ':b' -e 's/(^|[^0-9A-Za-z.:])127(\.[0-9]{1,3}){3}([^0-9.]|$)/\1@@L4@@\3/' -e 'tb' \
    -e 's/\[::\]/@@A6@@/g' \
    -e ':c' -e 's/(^|[^0-9A-Za-z_:])::1([^0-9A-Fa-f:]|$)/\1@@L6@@\2/' -e 'tc' \
    -e 's/(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])(\.(25[0-5]|2[0-4][0-9]|1[0-9]{2}|[1-9]?[0-9])){3}/<ipv4>/g' \
    -e 's/([0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{0,4}/<ipv6>/g' \
    -e 's/(^|[^0-9A-Za-z_:])([0-9A-Fa-f]{1,4}(:[0-9A-Fa-f]{1,4})*)?::([0-9A-Fa-f]{1,4}(:[0-9A-Fa-f]{1,4})*)?/\1<ipv6>/g' \
    -e 's/@@A4@@/0.0.0.0/g; s/@@L4@@/127.0.0.1/g; s/@@A6@@/[::]/g; s/@@L6@@/::1/g'
}

# unit_state UNIT...   one line per unit: enabled / active state (units that do not exist are skipped).
unit_state() {
  local u
  for u in "$@"; do
    if ! sctl cat "$u" >/dev/null 2>&1; then continue; fi
    printf '%s: enabled=%s active=%s\n' "$u" "$(sctl is-enabled "$u" 2>&1 | head -n 1)" "$(sctl is-active "$u" 2>&1 | head -n 1)"
  done
  return 0
}

# section NAME FUNCTION   run one collector; print it (stdout mode) or write NAME.txt (save mode). Never aborts.
# The block is held in memory first (no temporary file), and checked for secrets before anything is shown.
section() {
  local name=$1 func=$2 out rc=0
  out=$("$func" 2>&1) || rc=$?
  if (( rc != 0 )); then out+=$'\n'"(exit status $rc)"; fi
  if [[ $out =~ $SECRET_RE ]]; then
    SECRET_HITS=$((SECRET_HITS + 1))
    out="(withheld: this block matched a secret pattern - private key block, password hash or public key text)"
  fi
  if [[ $MODE == save ]]; then
    printf '%s\n' "$out" >"$DIR/$name.txt"
  else
    printf '##### %s #####\n%s\n\n' "$name" "$out"
  fi
  return 0
}

# ---------------------------------------------------------------- collectors

collect_meta() {
  echo "tool: wm-snapshot"
  echo "mode: $MODE"
  if [[ $MODE == save ]]; then echo "label: $LABEL"; fi
  echo "utc: $TS"
  echo "running as: $(id -un) (uid $EUID), groups: $(id -nG)"
  if (( SUDO_OK )); then echo "sudo -n: works (passwordless)"; else echo "sudo -n: refused or unavailable"; fi
  echo "max_hash_bytes: $MAX_HASH_BYTES"
  return 0
}

collect_os() {
  cat /etc/os-release
  echo "--- uname -srvmo"
  uname -srvmo
  echo "--- boot time (uptime -s)"
  uptime -s 2>/dev/null
  echo "--- versions"
  ssh -V 2>&1 | head -n 1
  sudo -V 2>/dev/null | head -n 1
  sctl --version | head -n 1
  bash --version | head -n 1
  return 0
}

collect_packages() {
  local list
  list=$(dpkg-query -W -f='${binary:Package}\t${Version}\n' | sort)
  printf 'count: %s\n' "$(printf '%s\n' "$list" | wc -l)"
  printf '%s\n' "$list"
  return 0
}

# Where packages come from: the apt source lines and, for the packages the first batch needs, the candidate version
# and its origin. "apt update" is NOT run; apt-cache only reads the lists that are already there.
collect_apt_sources() {
  local f
  echo "--- /etc/apt/sources.list and /etc/apt/sources.list.d (active lines, masked)"
  for f in /etc/apt/sources.list /etc/apt/sources.list.d/*; do
    [[ -f $f ]] || continue
    echo "-- $f"
    rd grep -Ev '^[[:space:]]*(#|$)' "$f" | redact | mask_ips
  done
  echo "--- apt-cache policy for the first-batch packages"
  apt-cache -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= policy fail2ban python3-systemd chrony ufw unattended-upgrades 2>&1 | mask_ips
  return 0
}

collect_services_running() {
  sctl list-units --type=service --state=running --no-legend --plain | awk '{print $1}' | sort
  return 0
}

collect_services_enabled() {
  sctl list-unit-files --type=service,socket,timer --state=enabled --no-legend | awk '{print $1, $2}' | sort
  return 0
}

collect_services_failed() {
  local out
  out=$(sctl list-units --state=failed --no-legend --plain | awk '{print $1, $2, $3, $4}' | sort)
  if [[ -z $out ]]; then echo "(none)"; else printf '%s\n' "$out"; fi
  return 0
}

collect_timers() {
  sctl list-units --type=timer --all --no-legend --plain | awk '{print $1, $3, $4}' | sort
  return 0
}

# Listening sockets as: proto port bind process. bind = any | loopback | specific. The addresses themselves are not printed.
collect_ports() {
  local raw
  if can_root; then
    raw=$(as_root ss -H -tulnp 2>/dev/null)
  else
    raw=$(ss -H -tuln 2>/dev/null)
    printf '%s sudo -n refused, process names not collected (ports only)\n' "$NEED_PW"
  fi
  printf 'proto port bind process\n'
  printf '%s\n' "$raw" | awk '
    NF >= 5 {
      proto = $1
      addr = $5
      port = addr
      sub(/^.*:/, "", port)
      host = addr
      sub(/:[^:]*$/, "", host)
      bind = "specific"
      if (host == "0.0.0.0" || host == "*" || host == "[::]" || host == "::") bind = "any"
      else if (host ~ /^127\./ || host == "[::1]" || host == "::1") bind = "loopback"
      proc = "-"
      if (match($0, /users:\(\("[^"]+"/)) proc = substr($0, RSTART + 9, RLENGTH - 10)
      print proto, port, bind, proc
    }' | sort -u
  return 0
}

collect_ufw() {
  if ! have ufw; then echo "(ufw is not installed)"; return 0; fi
  unit_state ufw.service
  echo "--- ufw status verbose"
  as_root ufw status verbose | mask_ips
  echo "--- ufw show added"
  as_root ufw show added | mask_ips
  echo "--- /etc/default/ufw (active lines)"
  grep -Ev '^[[:space:]]*(#|$)' /etc/default/ufw
  return 0
}

collect_nft() {
  local out
  if have nft; then
    out=$(as_root nft list ruleset | mask_ips)
    if [[ -n $out ]]; then printf '%s\n' "$out"; else echo "(nft list ruleset: the ruleset is empty)"; fi
  elif have iptables; then
    echo "(nft command not installed; iptables -S instead)"
    as_root iptables -S | mask_ips
    as_root ip6tables -S | mask_ips
  else
    echo "(neither nft nor iptables is installed)"
  fi
  unit_state nftables.service
  return 0
}

collect_sshd_T() {
  as_root sshd -T | mask_ips | sort
  return 0
}

collect_sshd_files() {
  local f lines
  if ! can_root; then printf '%s sudo -n refused, files that only root can read will show a permission error\n' "$NEED_PW"; fi
  for f in /etc/ssh/sshd_config /etc/ssh/sshd_config.d/*; do
    [[ -e $f ]] || continue
    printf '== %s  (mode/owner %s, sha256 %s)\n' "$f" "$(rd stat -c '%a %U:%G' "$f")" "$(rd sha256sum "$f" | cut -d' ' -f1)"
    lines=$(rd grep -Ev '^[[:space:]]*(#|$)' "$f" | mask_ips)
    if [[ -n $lines ]]; then printf '%s\n' "$lines"; else echo "(no active lines)"; fi
  done
  return 0
}

collect_ssh_socket() {
  unit_state ssh.socket ssh.service sshd.service
  echo "--- systemctl cat ssh.socket"
  sctl cat ssh.socket 2>&1 | mask_ips
  return 0
}

collect_nginx() {
  if have nginx; then
    echo "(nginx -T is a configuration test; run only because nginx is installed)"
    as_root nginx -T 2>&1 | redact | mask_ips
  else
    echo "(nginx is not installed)"
  fi
  return 0
}

collect_crontabs() {
  local f d user out
  echo "== /etc/crontab (active lines, masked)"
  if [[ -r /etc/crontab ]]; then grep -Ev '^[[:space:]]*(#|$)' /etc/crontab | redact | mask_ips; fi
  echo "== /etc/cron.d/* (active lines, masked)"
  if ! can_root; then printf '%s sudo -n refused, files that only root can read will show a permission error\n' "$NEED_PW"; fi
  for f in /etc/cron.d/*; do
    [[ -f $f ]] || continue
    echo "-- $f"
    rd grep -Ev '^[[:space:]]*(#|$)' "$f" | redact | mask_ips
  done
  echo "== /etc/cron.{hourly,daily,weekly,monthly}: sha256 and name only"
  for d in /etc/cron.hourly /etc/cron.daily /etc/cron.weekly /etc/cron.monthly; do
    for f in "$d"/*; do
      [[ -f $f ]] || continue
      printf '%s  %s\n' "$(sha256sum <"$f" | cut -d' ' -f1)" "$f"
    done
  done
  echo "== per-user crontabs (masked)"
  if can_root; then
    while IFS= read -r user; do
      out=$(as_root crontab -l -u "$user" 2>/dev/null) || continue
      if [[ -n $out ]]; then
        echo "-- $user"
        printf '%s\n' "$out" | redact | mask_ips
      fi
    done < <(getent passwd | awk -F: '($3 >= 1000 && $3 != 65534) || $3 == 0 || $7 !~ /(nologin|false)$/ { print $1 }' | sort -u)
  else
    printf '%s sudo -n refused, only the crontab of %s is shown\n' "$NEED_PW" "$(id -un)"
    out=$(crontab -l 2>/dev/null) || out=""
    if [[ -n $out ]]; then printf '%s\n' "$out" | redact | mask_ips; fi
  fi
  echo "== systemd timers: see block 33-timers"
  return 0
}

collect_sudoers() {
  local f note size
  if ! can_root; then
    printf '%s sudo -n refused, /etc/sudoers and /etc/sudoers.d are not readable without root\n' "$NEED_PW"
    return 0
  fi
  echo "== /etc/sudoers (active lines; #include and #includedir count as active)"
  as_root cat /etc/sudoers | grep -Ev '^[[:space:]]*$' | grep -Ev '^[[:space:]]*#([^i]|$|i[^n])' | mask_ips
  echo "== /etc/sudoers.d: every file with its mode, owner, sha256 and full text"
  while IFS= read -r f; do
    [[ -n $f ]] || continue
    note=""
    case ${f##*/} in
      *.*|*~) note="  [IGNORED by sudo: the name contains a dot or ends with ~]" ;;
    esac
    printf -- '-- %s  (%s, sha256 %s)%s\n' "$f" "$(as_root stat -c '%a %U:%G' "$f")" "$(as_root sha256sum "$f" | cut -d' ' -f1)" "$note"
    size=$(as_root stat -c '%s' "$f")
    if [[ $size =~ ^[0-9]+$ ]] && (( size <= 8192 )); then
      as_root cat "$f" | mask_ips
    else
      echo "(larger than 8 KiB: active lines only)"
      as_root grep -Ev '^[[:space:]]*(#|$)' "$f" | mask_ips
    fi
  done < <(as_root find /etc/sudoers.d -maxdepth 1 -type f | sort)
  return 0
}

collect_users_groups() {
  local g
  echo "== accounts that can log in (name:uid:gid:home:shell)"
  getent passwd | awk -F: '$7 !~ /(nologin|false)$/ && $7 != "/bin/sync" && $7 != "/sbin/halt" && $7 != "/sbin/shutdown" { print $1 ":" $3 ":" $4 ":" $6 ":" $7 }' | sort
  echo "== number of accounts in total: $(getent passwd | wc -l)"
  echo "== accounts with uid 0"
  getent passwd | awk -F: '$3 == 0 { print $1 }'
  echo "== groups that matter (name:x:gid:members)"
  for g in sudo admin adm docker lxd systemd-journal wm; do
    getent group "$g" || echo "$g: (absent)"
  done
  return 0
}

# Number of key lines only. No key text, no fingerprints, no comments.
collect_authkeys() {
  local user home f n
  echo "user file key_lines"
  if ! can_root; then printf '%s sudo -n refused, only the files of %s can be counted\n' "$NEED_PW" "$(id -un)"; fi
  while IFS=: read -r user home; do
    for f in "$home/.ssh/authorized_keys" "$home/.ssh/authorized_keys2"; do
      if can_root; then
        n=$(as_root grep -Evc '^[[:space:]]*(#|$)' "$f" 2>/dev/null)
      elif [[ $user == "$(id -un)" ]]; then
        n=$(grep -Evc '^[[:space:]]*(#|$)' "$f" 2>/dev/null)
      else
        continue
      fi
      [[ $n =~ ^[0-9]+$ ]] || continue
      echo "$user $f $n"
      # sshd ignores a key file when the home, .ssh or the file itself is writable by others (mode owner:group).
      echo "  permissions: home $(rd stat -c '%a %U:%G' "$home") | .ssh $(rd stat -c '%a %U:%G' "$home/.ssh") | file $(rd stat -c '%a %U:%G' "$f")"
    done
  done < <(getent passwd | awk -F: '$7 !~ /(nologin|false)$/ { print $1 ":" $6 }')
  return 0
}

# The server's own SSH host keys: size, fingerprint, type. The key comment is dropped on purpose.
collect_host_keys() {
  local f
  for f in /etc/ssh/ssh_host_*_key.pub; do
    [[ -f $f ]] || continue
    printf '%s  ' "$f"
    ssh-keygen -l -f "$f" 2>&1 | awk '{print $1, $2, $NF}'
  done
  return 0
}

# Cloud-vendor agents: process name, owner and systemd service only. Never the command line.
collect_agents() {
  local p pid comm user unit
  echo "process | owner | service"
  if can_root; then
    while IFS= read -r p; do
      [[ -n $p ]] || continue
      pid=${p#/proc/}
      comm=$(cat "/proc/$pid/comm" 2>/dev/null) || continue
      user=$(stat -c '%U' "/proc/$pid" 2>/dev/null) || user="?"
      unit=$(sed -n 's#^.*/\([^/]*\.service\)$#\1#p' "/proc/$pid/cgroup" 2>/dev/null | tail -n 1)
      printf '%s | %s | %s\n' "$comm" "$user" "${unit:--}"
    done < <(as_root find /proc -maxdepth 2 -path '/proc/[0-9]*/exe' -lname '/usr/local/qcloud/*' -printf '%h\n' 2>/dev/null | sort -u)
  else
    printf '%s sudo -n refused, falling back to a name match on the process list\n' "$NEED_PW"
    ps -eo comm=,user=,unit= 2>/dev/null | awk 'tolower($1) ~ /tat_agent|^yd|barad|sgagent|stargate|qcloud/ { print $1 " | " $2 " | " $3 }' | sort -u
  fi
  echo "--- unit files that look like cloud-vendor agents (enabled / active)"
  sctl list-unit-files --type=service --no-legend 2>/dev/null | awk '{print $1}' \
    | grep -Ei 'tat_agent|yunjing|barad|stargate|sgagent|qcloud|lighthouse' | while read -r p; do unit_state "$p"; done
  return 0
}

collect_srv_listing() {
  if [[ ! -d $SRV_ROOT ]]; then echo "(no $SRV_ROOT)"; return 0; fi
  echo "== type mode owner:group size path"
  find "$SRV_ROOT" -xdev -printf '%y %m %u:%g %s %p\n' | sort -k5
  return 0
}

collect_srv_hashes() {
  if [[ ! -d $SRV_ROOT ]]; then echo "(no $SRV_ROOT)"; return 0; fi
  echo "== files with a sensitive-looking name: listed, NOT hashed"
  find "$SRV_ROOT" -xdev -type f \( -name '.cos.yaml' -o -name '*.pem' -o -name '*.key' -o -name 'id_*' \
    -o -name '*secret*' -o -name '.env' -o -name '.env.*' -o -name '*.env' \) -printf 'NOT-HASHED(name) %s %p\n' | sort -k3
  echo "== files larger than $MAX_HASH_BYTES bytes: listed, NOT hashed"
  find "$SRV_ROOT" -xdev -type f -size +"${MAX_HASH_BYTES}"c -printf 'NOT-HASHED(size) %s %p\n' | sort -k3
  echo "== symlinks"
  find "$SRV_ROOT" -xdev -type l -printf '%p -> %l\n' | sort
  echo "== sha256 of the remaining regular files"
  find "$SRV_ROOT" -xdev -type f ! -size +"${MAX_HASH_BYTES}"c ! \( -name '.cos.yaml' -o -name '*.pem' -o -name '*.key' \
    -o -name 'id_*' -o -name '*secret*' -o -name '.env' -o -name '.env.*' -o -name '*.env' \) -print0 \
    | sort -z | xargs -0 -r sha256sum
  return 0
}

collect_wmops() {
  local f n=0
  for f in /usr/local/sbin/wm-*; do
    [[ -e $f ]] || continue
    n=$((n + 1))
    printf '%s %s %s\n' "$(stat -c '%a %U:%G' "$f")" "$(sha256sum <"$f" | cut -d' ' -f1)" "$f"
  done
  if (( n == 0 )); then echo "(no wm-* tools installed in /usr/local/sbin)"; fi
  return 0
}

collect_journald() {
  echo "--- effective journald configuration (active lines, with the file each part comes from)"
  systemd-analyze cat-config systemd/journald.conf 2>&1 | grep -E '^(\[|[A-Za-z]|# /)'
  echo "--- /var/log/journal (mode owner group)"
  if [[ -d /var/log/journal ]]; then ls -ld /var/log/journal | awk '{print $1, $3, $4}'; else echo "(absent: the journal is kept in memory only)"; fi
  unit_state rsyslog.service systemd-journald.service
  return 0
}

collect_autoupdate_config() {
  local f
  echo "--- /etc/apt/apt.conf.d/20auto-upgrades"
  if [[ -r /etc/apt/apt.conf.d/20auto-upgrades ]]; then cat /etc/apt/apt.conf.d/20auto-upgrades; else echo "(absent)"; fi
  echo "--- /etc/apt/apt.conf.d/50unattended-upgrades (active lines)"
  if [[ -r /etc/apt/apt.conf.d/50unattended-upgrades ]]; then
    grep -Ev '^[[:space:]]*(//|$)' /etc/apt/apt.conf.d/50unattended-upgrades
  else
    echo "(absent)"
  fi
  echo "--- files in /etc/apt/apt.conf.d (sha256, name)"
  for f in /etc/apt/apt.conf.d/*; do
    [[ -f $f ]] || continue
    printf '%s  %s\n' "$(sha256sum <"$f" | cut -d' ' -f1)" "$f"
  done
  echo "--- effective apt-config (APT::Periodic and Unattended-Upgrade keys)"
  apt-config dump 2>&1 | grep -E '^(APT::Periodic|Unattended-Upgrade)::' | sort
  echo "--- units"
  unit_state apt-daily.timer apt-daily-upgrade.timer unattended-upgrades.service
  echo "--- package"
  dpkg-query -W -f='${Package} ${Version} ${Status}\n' unattended-upgrades 2>&1
  return 0
}

collect_disk() {
  df -hT -x tmpfs -x devtmpfs -x squashfs -x overlay
  echo "--- memory (free -m)"
  free -m
  echo "--- swap"
  swapon --show 2>&1
  echo "--- cpus: $(nproc)"
  echo "--- journal size"
  rd journalctl --no-pager --disk-usage 2>&1
  echo "--- du (sizes of the places we write to or care about)"
  as_root du -sh "$SRV_ROOT" /var/log "$BASE" 2>/dev/null
  return 0
}

collect_fail2ban() {
  if ! have fail2ban-client; then echo "(fail2ban is not installed)"; return 0; fi
  unit_state fail2ban.service
  as_root fail2ban-client status 2>&1 | grep -v 'Banned IP' | mask_ips
  as_root fail2ban-client status sshd 2>&1 | grep -v 'Banned IP' | mask_ips
  echo "(the list of banned addresses is left out on purpose)"
  return 0
}

collect_time() {
  local f
  timedatectl 2>&1
  echo "--- units (enabled / active)"
  unit_state chrony.service chronyd.service systemd-timesyncd.service ntpsec.service ntp.service
  echo "--- chrony configuration files (active lines, addresses masked)"
  for f in /etc/chrony/chrony.conf /etc/chrony/conf.d/* /etc/chrony/sources.d/*; do
    [[ -f $f ]] || continue
    echo "-- $f"
    rd grep -Ev '^[[:space:]]*([#!;%]|$)' "$f" | mask_ips
  done
  if have chronyc; then
    echo "--- chronyc tracking"
    as_root chronyc tracking 2>&1 | mask_ips
    echo "--- chronyc sources"
    as_root chronyc sources 2>&1 | mask_ips
  fi
  if sctl is-active --quiet systemd-timesyncd.service; then
    echo "--- timedatectl timesync-status"
    timedatectl timesync-status 2>&1 | mask_ips
  fi
  return 0
}

# apt update is NOT run. The apt package cache files are switched off, so even as root apt writes nothing.
collect_updates() {
  local n
  echo "--- timers (next / last run)"
  sctl list-timers apt-daily.timer apt-daily-upgrade.timer --all 2>&1
  echo "--- /var/lib/apt/periodic (date time name)"
  ls -l --time-style=long-iso /var/lib/apt/periodic 2>&1 | awk 'NR > 1 { print $6, $7, $8 }'
  echo "--- unattended-upgrades log, last 15 lines"
  as_root tail -n 15 /var/log/unattended-upgrades/unattended-upgrades.log 2>&1
  echo "--- reboot required"
  if [[ -e /var/run/reboot-required ]]; then
    echo "present"
    cat /var/run/reboot-required.pkgs 2>/dev/null
  else
    echo "absent"
  fi
  echo "--- apt list --upgradable (count only)"
  n=$(apt -o Dir::Cache::pkgcache= -o Dir::Cache::srcpkgcache= list --upgradable 2>/dev/null | grep -c 'upgradable from' || true)
  echo "upgradable packages: $n"
  return 0
}

# Counts only. No log line and no address ever leaves this function.
collect_ssh_failures() {
  local raw
  if ! have journalctl; then echo "(journalctl not found)"; return 0; fi
  if can_root; then
    raw=$(as_root journalctl -u ssh.service -u ssh.socket --since '24 hours ago' --no-pager -q -o cat 2>/dev/null)
  else
    raw=$(journalctl -u ssh.service -u ssh.socket --since '24 hours ago' --no-pager -q -o cat 2>/dev/null)
    printf '%s sudo -n refused, reading the journal as %s (empty if that account may not read it)\n' "$NEED_PW" "$(id -un)"
  fi
  echo "window: last 24 hours"
  printf '%s\n' "$raw" | awk '
    NF { total++ }
    /Failed password/ { fp++ }
    /Failed publickey/ { fk++ }
    /Invalid user/ { iu++ }
    /authentication failure/ { af++ }
    /Connection closed by (invalid|authenticating) user/ { cc++ }
    /maximum authentication attempts/ { ma++ }
    END {
      printf "log lines read: %d\nfailed password: %d\nfailed publickey: %d\ninvalid user: %d\nauthentication failure: %d\nclosed before login: %d\nmax attempts exceeded: %d\n", total, fp, fk, iu, af, cc, ma
    }'
  return 0
}

# ---------------------------------------------------------------- run

run_all() {
  section 00-meta               collect_meta
  section 10-os                 collect_os
  section 20-packages           collect_packages
  section 21-apt-sources        collect_apt_sources
  section 30-services-running   collect_services_running
  section 31-services-enabled   collect_services_enabled
  section 32-services-failed    collect_services_failed
  section 33-timers             collect_timers
  section 40-ports              collect_ports
  section 50-ufw                collect_ufw
  section 51-nftables           collect_nft
  section 60-sshd-T             collect_sshd_T
  section 61-sshd-files         collect_sshd_files
  section 62-ssh-socket         collect_ssh_socket
  section 70-nginx-T            collect_nginx
  section 80-crontabs           collect_crontabs
  section 81-sudoers            collect_sudoers
  section 82-users-groups       collect_users_groups
  section 83-authorized-keys    collect_authkeys
  section 84-host-keys          collect_host_keys
  section 85-agents             collect_agents
  section 90-srv-listing        collect_srv_listing
  section 91-srv-hashes         collect_srv_hashes
  section 92-wm-ops             collect_wmops
  section 93-journald           collect_journald
  section 94-autoupdate-config  collect_autoupdate_config
  section 95-disk               collect_disk
  section 96-fail2ban           collect_fail2ban
  section 97-time               collect_time
  section 98-updates            collect_updates
  section 99-ssh-failures       collect_ssh_failures
  return 0
}

main() {
  while (( $# )); do
    case $1 in
      --stdout) MODE=stdout ;;
      --save) MODE=save; shift || true; LABEL=${1:-} ;;
      -h|--help) usage; return 0 ;;
      *) echo "ERROR: unknown option: $1" >&2; usage >&2; return 2 ;;
    esac
    shift || true
  done
  if [[ ! $MAX_HASH_BYTES =~ ^[0-9]+$ ]]; then echo "ERROR: WM_SNAPSHOT_MAX_HASH_BYTES must be a number" >&2; return 2; fi
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  detect_sudo
  if [[ $MODE == save ]]; then
    if (( EUID != 0 )); then echo "ERROR: --save must run as root (use sudo)" >&2; return 2; fi
    if [[ ! $LABEL =~ ^[A-Za-z0-9._-]{1,40}$ ]]; then echo "ERROR: bad or missing LABEL" >&2; usage >&2; return 2; fi
    umask 077
    install -d -m 0700 -o root -g root "$BASE" || return 1
    DIR="$BASE/$TS-$LABEL"
    mkdir -m 0700 -- "$DIR" || return 1
  fi
  run_all
  if [[ $MODE == save ]]; then
    chmod -R go-rwx "$DIR"
    printf '%s\n' "$DIR"
  else
    printf '##### END (secret_hits=%d) #####\n' "$SECRET_HITS"
  fi
  if (( SECRET_HITS > 0 )); then
    echo "ERROR: $SECRET_HITS block(s) matched a secret pattern and were withheld" >&2
    return 3
  fi
  return 0
}

main "$@" </dev/null; exit $?
