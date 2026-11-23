#!/bin/bash
# wm-verify.sh - read-only acceptance checks for the first-batch hardening.
#
# Installed by wm-harden.sh as /usr/local/sbin/wm-verify (owner root). Run:  sudo wm-verify
#
# One line per check:  PASS / FAIL, plus INFO (facts) and MANUAL (things only the console can show).
# Exit status: 0 when there is no FAIL, 1 otherwise.
#
# It changes nothing on the system. Only two side effects:
#   - it appends to /var/log/wm-ops/verify.log
#   - it makes ONE password-only login attempt against the local sshd (user name that does not exist)
#     to prove that password login is refused. fail2ban ignores the loopback address by default.
#     For that attempt it creates one temporary file (the local host keys) and removes it right away.
#
# The checks follow the plan page's hardening table ("how to verify" column) and also detect whether the
# main /etc/ssh/sshd_config was changed behind our back (console "online reset password" rewrites it).
#
# Output is plain ASCII.

set -euo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
unset BASH_ENV ENV CDPATH
IFS=$' \t\n'
umask 077

readonly LOG_DIR=/var/log/wm-ops
readonly LOGFILE="$LOG_DIR/verify.log"
readonly BASELINE_DIR=/var/lib/wm-ops/baseline
readonly DROPIN=/etc/ssh/sshd_config.d/00-wm-hardening.conf
readonly DROPIN_NAME=00-wm-hardening.conf
readonly F2B_FILE=/etc/fail2ban/jail.d/90-wm-sshd.local
readonly SRV_ROOT=/srv/worldmodel

# The one identity this draft supports (decided 2026-11-14).
readonly OPS_USER=ubuntu

PASS_N=0
FAIL_N=0
INFO_N=0
MANUAL_N=0
SSHD_T=""

if (( EUID != 0 )); then echo "ERROR: wm-verify must run as root (use sudo)" >&2; exit 2; fi
install -d -m 0750 -o root -g root "$LOG_DIR"
touch "$LOGFILE"
chmod 0640 "$LOGFILE"

emit() {
  local line
  line=$(printf '%-6s %-26s %s' "$1" "$2" "$3")
  printf '%s\n' "$line"
  printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$line" >>"$LOGFILE"
}
pass()   { PASS_N=$((PASS_N + 1));     emit PASS   "$1" "$2"; }
fail()   { FAIL_N=$((FAIL_N + 1));     emit FAIL   "$1" "$2"; }
info()   { INFO_N=$((INFO_N + 1));     emit INFO   "$1" "$2"; }
manual() { MANUAL_N=$((MANUAL_N + 1)); emit MANUAL "$1" "$2"; }

sshd_val() {
  awk -v k="$1" '$1 == k { $1 = ""; sub(/^ /, ""); print; exit }' <<<"$SSHD_T"
}

# ------------------------------------------------------------------ ssh
chk_ssh() {
  local pair k want got port probe probe_kh f b bad line cur base_hash
  if ! SSHD_T=$(sshd -T 2>&1); then
    fail ssh.sshd-T "sshd -T failed: $(printf '%s' "$SSHD_T" | head -n 1)"
    return 0
  fi
  for pair in passwordauthentication:no kbdinteractiveauthentication:no permitrootlogin:no pubkeyauthentication:yes; do
    k=${pair%%:*}
    want=${pair##*:}
    got=$(sshd_val "$k")
    if [[ $got == "$want" ]]; then pass "ssh.$k" "$k $got"; else fail "ssh.$k" "$k is '${got:-unset}', want '$want'"; fi
  done

  if grep -Eq '^(allowusers|allowgroups|denyusers|denygroups) ' <<<"$SSHD_T"; then
    fail ssh.no-allowusers "AllowUsers/AllowGroups/DenyUsers/DenyGroups is set: it can block the web-terminal SSH channel"
  else
    pass ssh.no-allowusers "no user/group restriction in sshd (the web-terminal SSH channel keeps working)"
  fi
  if grep -Eq '^subsystem sftp( |$)' <<<"$SSHD_T"; then pass ssh.sftp "sftp subsystem present (scp uses the SFTP protocol)"; else fail ssh.sftp "no sftp subsystem: scp will not work"; fi
  got=$(sshd_val maxsessions)
  if [[ $got =~ ^[0-9]+$ ]] && (( got >= 2 )); then pass ssh.maxsessions "maxsessions $got"; else fail ssh.maxsessions "maxsessions is '${got:-unset}', need 2 or more"; fi

  if grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config; then
    pass ssh.include "sshd_config includes sshd_config.d/*.conf"
  else
    fail ssh.include "sshd_config has no Include for sshd_config.d: our drop-in is ignored"
  fi
  if [[ -f $DROPIN ]]; then pass ssh.dropin "$DROPIN present"; else fail ssh.dropin "$DROPIN missing"; fi

  for f in /etc/ssh/sshd_config.d/*.conf; do
    [[ -e $f ]] || continue
    b=$(basename "$f")
    if [[ $b == "$DROPIN_NAME" ]]; then continue; fi
    bad=$(grep -Eis '^[[:space:]]*(passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication|permitrootlogin|pubkeyauthentication|allowusers|allowgroups)[[:space:]]+' "$f" || true)
    if [[ -z $bad ]]; then
      info ssh.other-dropin "$b: no authentication directives"
    else
      while IFS= read -r line; do
        info ssh.other-dropin "$b: $(printf '%s' "$line" | tr -s '[:space:]' ' ')"
      done <<<"$bad"
      if grep -Eiqs '^[[:space:]]*(passwordauthentication|kbdinteractiveauthentication|challengeresponseauthentication)[[:space:]]+yes' "$f"; then
        if [[ $b < $DROPIN_NAME ]]; then
          fail ssh.other-dropin "$b sorts BEFORE $DROPIN_NAME and enables password login: sshd keeps the first value it reads"
        else
          info ssh.other-dropin "$b enables password login but sorts after $DROPIN_NAME, so ours wins (first value wins)"
        fi
      fi
    fi
  done

  # The main file: console "online reset password" rewrites PasswordAuthentication there (check notes B2-b).
  if grep -Eis '^[[:space:]]*PasswordAuthentication[[:space:]]+yes' /etc/ssh/sshd_config >/dev/null; then
    fail ssh.main-pwauth "main sshd_config has an active 'PasswordAuthentication yes' (console online password reset?); harmless only while $DROPIN_NAME sorts first"
  else
    pass ssh.main-pwauth "main sshd_config has no active 'PasswordAuthentication yes'"
  fi
  base_hash="$BASELINE_DIR/sshd_config.sha256"
  cur=$(sha256sum </etc/ssh/sshd_config | cut -d' ' -f1)
  if [[ -r $base_hash ]]; then
    if [[ $(cut -d' ' -f1 "$base_hash") == "$cur" ]]; then
      pass ssh.main-unchanged "main sshd_config is identical to the baseline recorded by wm-harden"
    else
      fail ssh.main-unchanged "main sshd_config differs from the baseline; compare: diff $BASELINE_DIR/sshd_config /etc/ssh/sshd_config  (a package upgrade or the console password reset can cause this; wm-harden --apply --rebaseline accepts it)"
    fi
  else
    info ssh.main-unchanged "no baseline recorded yet (wm-harden --apply writes it)"
  fi

  if systemctl is-active --quiet ssh.service || systemctl is-active --quiet ssh.socket || systemctl is-active --quiet sshd.service; then
    pass ssh.service "ssh service or socket is active"
  else
    fail ssh.service "neither ssh.service nor ssh.socket is active"
  fi

  port=$(sshd_val port | head -n 1)
  if [[ ! $port =~ ^[0-9]+$ ]]; then port=22; fi
  # The probe only talks to this machine (localhost). Its own host keys are read first into a throw-away file, so
  # the probe runs with the normal host key check and no relaxed option.
  probe_kh=$(mktemp)
  timeout 10 ssh-keyscan -p "$port" localhost >"$probe_kh" 2>/dev/null || true
  probe=$(timeout 20 ssh -F /dev/null -o BatchMode=yes -o PreferredAuthentications=password -o PubkeyAuthentication=no \
    -o UserKnownHostsFile="$probe_kh" -o GlobalKnownHostsFile=/dev/null -o LogLevel=ERROR -o ConnectTimeout=8 \
    -p "$port" wm-verify-probe@localhost true 2>&1 || true)
  rm -f -- "$probe_kh"
  case $probe in
    *"Permission denied (publickey)"*)
      pass ssh.password-probe "local password-only login attempt was refused; the server offers publickey only" ;;
    *"Permission denied"*)
      fail ssh.password-probe "the server offered more than publickey: $(printf '%s' "$probe" | tail -n 1)" ;;
    *"Connection refused"*|*"timed out"*|*"Could not resolve"*|*"No route"*)
      info ssh.password-probe "could not reach the local sshd on port $port, probe skipped: $(printf '%s' "$probe" | tail -n 1)" ;;
    *)
      info ssh.password-probe "unexpected probe result: $(printf '%s' "$probe" | tail -n 1)" ;;
  esac
  return 0
}

# ------------------------------------------------------------------ firewall and listening ports
chk_firewall() {
  local out rules extra addr host p bad port
  if ! command -v ufw >/dev/null 2>&1; then fail firewall.ufw "ufw is not installed"; return 0; fi
  out=$(ufw status verbose 2>&1 || true)
  port=$(sshd_val port | head -n 1)
  if [[ ! $port =~ ^[0-9]+$ ]]; then port=22; fi
  if grep -q '^Status: active' <<<"$out"; then pass firewall.active "ufw is active"; else fail firewall.active "ufw is not active"; fi
  if grep -Eq '^Default: deny \(incoming\), allow \(outgoing\)' <<<"$out"; then
    pass firewall.defaults "default deny incoming, allow outgoing"
  else
    fail firewall.defaults "ufw defaults are not 'deny (incoming), allow (outgoing)'"
  fi
  rules=$(awk '/^--/ { f = 1; next } f && NF' <<<"$out" | sed -E 's/#.*//; s/[[:space:]]+/ /g; s/ +$//' | sed '/^$/d')
  extra=$(grep -Ev "^${port}/tcp( \\(v6\\))? ALLOW IN Anywhere( \\(v6\\))?\$" <<<"$rules" || true)
  if [[ -z $rules ]]; then
    fail firewall.rules "no ufw rules: ${port}/tcp is not allowed"
  elif [[ -z $extra ]]; then
    pass firewall.rules "ufw allows only ${port}/tcp inbound"
  else
    fail firewall.rules "ufw has rules besides ${port}/tcp: $(printf '%s' "$extra" | tr '\n' ';')"
  fi

  # Listening TCP sockets that are reachable from outside: only the ssh port is allowed.
  bad=""
  while read -r addr; do
    [[ -n $addr ]] || continue
    host=${addr%:*}
    p=${addr##*:}
    case $host in
      127.*|'[::1]'|'::1') continue ;;
    esac
    if [[ $p != "$port" ]]; then bad="$bad $addr"; fi
  done < <(ss -H -tln | awk '{print $4}')
  if [[ -z $bad ]]; then
    pass firewall.listeners "no TCP listener reachable from outside except port $port"
  else
    fail firewall.listeners "TCP listeners reachable from outside:$bad"
  fi
  info firewall.udp "non-loopback UDP listeners: $(ss -H -uln | awk '{print $4}' | grep -Ev '^(127\.|\[::1\]|::1)' | tr '\n' ' ')"
  manual firewall.console "Lighthouse console firewall must allow only port 22 as well (close the default port 80 first); the server cannot see it"
  return 0
}

# ------------------------------------------------------------------ automatic security updates
chk_updates() {
  local cfg origins lastlog up key want
  if dpkg-query -W -f='${Status}' unattended-upgrades 2>/dev/null | grep -q 'install ok installed'; then
    pass updates.package "unattended-upgrades is installed"
  else
    fail updates.package "unattended-upgrades is not installed"
    return 0
  fi
  if ! cfg=$(apt-config dump 2>&1); then fail updates.apt-config "apt-config dump failed (broken apt configuration file?)"; return 0; fi
  for key in 'APT::Periodic::Update-Package-Lists:1' 'APT::Periodic::Unattended-Upgrade:1' 'Unattended-Upgrade::Automatic-Reboot:false'; do
    want=${key##*:}
    key=${key%:*}
    if grep -Fxq "$key \"$want\";" <<<"$cfg"; then pass "updates.${key##*::}" "$key = $want"; else fail "updates.${key##*::}" "$key is not $want"; fi
  done
  origins=$(grep '^Unattended-Upgrade::Allowed-Origins::' <<<"$cfg" || true)
  if [[ -z $origins ]]; then
    fail updates.origins "no Allowed-Origins configured"
  elif grep -Eq -- '-(updates|proposed|backports)"' <<<"$origins" || grep -Fq -- '"${distro_id}:${distro_codename}";' <<<"$origins"; then
    fail updates.origins "a non-security origin is allowed: $(printf '%s' "$origins" | tr '\n' ' ')"
  else
    pass updates.origins "only security origins are allowed"
  fi
  if systemctl is-enabled --quiet apt-daily.timer && systemctl is-enabled --quiet apt-daily-upgrade.timer; then
    pass updates.timers "apt-daily and apt-daily-upgrade timers are enabled"
  else
    fail updates.timers "apt-daily / apt-daily-upgrade timers are not both enabled"
  fi
  lastlog=/var/log/unattended-upgrades/unattended-upgrades.log
  if [[ -f $lastlog ]] && [[ -n $(find "$lastlog" -mmin -2880 2>/dev/null) ]]; then
    pass updates.recent-run "unattended-upgrades wrote its log within the last 48 hours"
  else
    up=$(cut -d. -f1 /proc/uptime)
    if (( up < 172800 )); then
      info updates.recent-run "no run record yet; the machine has been up under 48 hours and the timers run once a day"
    else
      fail updates.recent-run "no unattended-upgrades log entry within 48 hours"
    fi
  fi
  if [[ -f /var/run/reboot-required ]]; then
    info updates.reboot-required "a reboot is pending (automatic reboot is off): $(tr '\n' ' ' </var/run/reboot-required.pkgs 2>/dev/null || true)"
  fi
  return 0
}

# ------------------------------------------------------------------ fail2ban
chk_fail2ban() {
  local st pair k want got
  if ! command -v fail2ban-client >/dev/null 2>&1; then fail fail2ban.installed "fail2ban-client not found"; return 0; fi
  if systemctl is-active --quiet fail2ban; then pass fail2ban.service "fail2ban is active"; else fail fail2ban.service "fail2ban is not active"; return 0; fi
  if ! st=$(fail2ban-client status sshd 2>&1); then fail fail2ban.jail "jail sshd is not running: $(printf '%s' "$st" | head -n 1)"; return 0; fi
  pass fail2ban.jail "jail sshd is running"
  if grep -q 'Journal matches' <<<"$st"; then pass fail2ban.backend "jail sshd reads the systemd journal"; else fail fail2ban.backend "jail sshd is not using the systemd backend"; fi
  for pair in maxretry:5 findtime:600 bantime:600; do
    k=${pair%%:*}
    want=${pair##*:}
    got=$(fail2ban-client get sshd "$k" 2>/dev/null || true)
    if [[ $got == "$want" ]]; then pass "fail2ban.$k" "$k $got"; else fail "fail2ban.$k" "$k is '${got:-unset}', want '$want'"; fi
  done
  if [[ -f $F2B_FILE ]] && grep -Eq '^mode[[:space:]]*=[[:space:]]*normal[[:space:]]*$' "$F2B_FILE"; then
    pass fail2ban.mode "mode = normal in $F2B_FILE"
  else
    fail fail2ban.mode "mode = normal not found in $F2B_FILE"
  fi
  info fail2ban.status "$(printf '%s' "$st" | tr -s '[:space:]' ' ')"
  return 0
}

# ------------------------------------------------------------------ time
chk_time() {
  local src trk n off cnt
  if systemctl is-active --quiet chrony; then pass time.chrony "chrony is active"; else fail time.chrony "chrony is not active"; fi
  if systemctl is-active --quiet systemd-timesyncd 2>/dev/null; then
    fail time.timesyncd "systemd-timesyncd is active together with chrony"
  else
    pass time.timesyncd "systemd-timesyncd is not active"
  fi
  if ! src=$(chronyc sources 2>&1); then fail time.sources "chronyc sources failed: $(printf '%s' "$src" | head -n 1)"; return 0; fi
  # chronyc shows the resolved addresses, not the configured names, so the names are looked up in the configuration.
  for n in 1 2 3 4 5; do
    if grep -Eqs "^[[:space:]]*server[[:space:]]+time${n}\.tencentyun\.com([[:space:]]|\$)" /etc/chrony/chrony.conf /etc/chrony/conf.d/*.conf /etc/chrony/sources.d/*.sources; then
      pass "time.server$n" "time${n}.tencentyun.com is configured as a server"
    else
      fail "time.server$n" "time${n}.tencentyun.com is not configured as a server"
    fi
  done
  cnt=$(grep -Ec '^[\^=][*+?x~-] ' <<<"$src" || true)
  if [[ $cnt =~ ^[0-9]+$ ]] && (( cnt >= 3 )); then pass time.sources "chrony knows $cnt time sources"; else fail time.sources "chrony knows ${cnt:-0} time source(s), want 3 or more"; fi
  if grep -Eq '^\^[*+] ' <<<"$src"; then pass time.selected "at least one source is selected (^* or ^+)"; else fail time.selected "no source is selected yet (^* or ^+); give it a minute after a restart"; fi
  trk=$(chronyc tracking 2>&1 || true)
  off=$(awk '/^System time/ { print $4 }' <<<"$trk")
  if [[ $off =~ ^[0-9.]+$ ]] && awk -v o="$off" 'BEGIN { exit !(o < 0.1) }'; then
    pass time.offset "system time is ${off}s from NTP time (limit 0.1s)"
  else
    fail time.offset "offset is '${off:-unknown}' (limit 0.1s)"
  fi
  if grep -q '^Leap status *: Normal' <<<"$trk"; then pass time.leap "leap status Normal"; else fail time.leap "leap status is not Normal"; fi
  if [[ $(timedatectl show -p NTPSynchronized --value 2>/dev/null || true) == yes ]]; then
    pass time.synchronized "timedatectl: NTPSynchronized=yes"
  else
    fail time.synchronized "timedatectl: NTPSynchronized is not yes"
  fi
  return 0
}

# ------------------------------------------------------------------ logs
chk_logs() {
  local cfg
  if [[ -d /var/log/journal ]]; then pass logs.journal-dir "/var/log/journal exists (journal is stored on disk)"; else fail logs.journal-dir "/var/log/journal is missing (journal is volatile)"; fi
  cfg=$(systemd-analyze cat-config systemd/journald.conf 2>/dev/null || true)
  if grep -Eq '^Storage=persistent' <<<"$cfg"; then pass logs.storage "journald Storage=persistent"; else fail logs.storage "journald Storage=persistent not found"; fi
  if grep -Eq '^SystemMaxUse=' <<<"$cfg"; then pass logs.size-limit "journald SystemMaxUse is set"; else fail logs.size-limit "journald SystemMaxUse is not set"; fi
  info logs.disk-usage "$(journalctl --disk-usage 2>&1 | head -n 1)"
  if [[ -f /etc/logrotate.d/wm-ops ]]; then pass logs.rotate "logrotate config for /var/log/wm-ops present"; else fail logs.rotate "/etc/logrotate.d/wm-ops missing"; fi
  return 0
}

# ------------------------------------------------------------------ accounts and sudo
chk_accounts() {
  local keys home st lst shell uid whome
  if ! id "$OPS_USER" >/dev/null 2>&1; then fail accounts.ops-user "user $OPS_USER does not exist"; return 0; fi
  pass accounts.ops-user "user $OPS_USER exists"
  home=$(getent passwd "$OPS_USER" | cut -d: -f6)
  if keys=$(ssh-keygen -l -f "$home/.ssh/authorized_keys" 2>&1) && [[ -n $keys ]]; then
    pass accounts.ops-keys "$OPS_USER has $(printf '%s\n' "$keys" | wc -l) authorized key(s): $(printf '%s' "$keys" | awk '{print $NF}' | tr '\n' ' ')"
  else
    fail accounts.ops-keys "no valid key in $home/.ssh/authorized_keys"
  fi
  # By design (README section 2) ubuntu keeps passwordless sudo, so it is root-equivalent. Reported as a fact, not as a failure.
  lst=$(sudo -l -U "$OPS_USER" 2>&1 | grep -E '^[[:space:]]+\(' | tr -s '[:space:]' ' ' | tr '\n' ';' || true)
  info accounts.ops-sudo "sudo rights of $OPS_USER (accepted by design, README section 2): ${lst:-none listed}"
  info accounts.sudoers-files "files in /etc/sudoers.d: $(find /etc/sudoers.d -maxdepth 1 -type f -printf '%f ' 2>/dev/null)"
  st=$(passwd -S "$OPS_USER" 2>/dev/null | awk '{print $2}' || true)
  info accounts.ops-password "password status of $OPS_USER: ${st:-unknown} (L locked, P set, NP none)"
  info accounts.other-login "other accounts with a login shell (the Tencent Cloud image has lighthouse, with passwordless sudo): $(getent passwd | awk -F: '$7 !~ /(nologin|false)$/ && $7 != "/bin/sync" && $1 != "root" && $1 != "ubuntu" { printf "%s ", $1 }')"

  if id wm >/dev/null 2>&1; then
    shell=$(getent passwd wm | cut -d: -f7)
    uid=$(id -u wm)
    whome=$(getent passwd wm | cut -d: -f6)
    case $shell in
      /usr/sbin/nologin|/sbin/nologin|/bin/false|/usr/bin/false) pass accounts.wm-shell "user wm cannot log in (shell $shell)" ;;
      *) fail accounts.wm-shell "user wm has a login shell: $shell" ;;
    esac
    if (( uid < 1000 )); then pass accounts.wm-system "wm is a system account (uid $uid)"; else fail accounts.wm-system "wm has uid $uid (not a system account)"; fi
    if [[ -e $whome/.ssh/authorized_keys ]]; then fail accounts.wm-keys "wm has an authorized_keys file in $whome/.ssh"; else pass accounts.wm-keys "wm has no authorized_keys"; fi
  else
    fail accounts.wm "user wm does not exist"
  fi
  st=$(passwd -S root 2>/dev/null | awk '{print $2}' || true)
  info accounts.root "root password status: ${st:-unknown} (L means locked); root login over ssh is off anyway"
  return 0
}

# ------------------------------------------------------------------ directories
chk_dirs() {
  local spec d mode got want
  for spec in "$SRV_ROOT:2750" "$SRV_ROOT/releases:2750" "$SRV_ROOT/shared:2770"; do
    d=${spec%%:*}
    mode=${spec##*:}
    if [[ ! -d $d ]]; then fail "dirs.$(basename "$d")" "$d is missing"; continue; fi
    got=$(stat -c '%a %U:%G' "$d")
    want="$mode $OPS_USER:wm"
    if [[ $got == "$want" ]]; then pass "dirs.$(basename "$d")" "$d $got"; else fail "dirs.$(basename "$d")" "$d is '$got', want '$want'"; fi
  done
  return 0
}

# ------------------------------------------------------------------ Tencent Cloud components must stay alone
chk_tencent() {
  if pgrep -x tat_agent >/dev/null 2>&1; then
    pass tencent.tat_agent "tat_agent is running (console web-terminal login and online key/password changes depend on it)"
  else
    fail tencent.tat_agent "tat_agent is not running: the web-terminal emergency path is down"
  fi
  if pgrep -f /usr/local/qcloud/ >/dev/null 2>&1; then
    info tencent.processes "processes under /usr/local/qcloud: $(pgrep -af /usr/local/qcloud/ | awk '{print $2}' | xargs -r -n1 basename | sort -u | tr '\n' ' ')"
  else
    info tencent.processes "no process runs from /usr/local/qcloud"
  fi
  return 0
}

# ------------------------------------------------------------------ our own tools
chk_tools() {
  local t f got
  for t in wm-harden wm-snapshot wm-snapshot-diff wm-verify; do
    f=/usr/local/sbin/$t
    if [[ ! -f $f ]]; then fail "tools.$t" "$f is missing"; continue; fi
    got=$(stat -c '%a %U:%G' "$f")
    if [[ $got == "755 root:root" ]]; then
      pass "tools.$t" "$f $got sha256=$(sha256sum <"$f" | cut -d' ' -f1)"
    else
      fail "tools.$t" "$f is '$got', want '755 root:root'"
    fi
  done
  if [[ -d /var/lib/wm-ops ]] && [[ -n $(find /var/lib/wm-ops -mindepth 1 -maxdepth 1 -type d -name '[0-9]*T[0-9]*Z-*' 2>/dev/null | head -n 1) ]]; then
    info tools.snapshots "$(find /var/lib/wm-ops -mindepth 1 -maxdepth 1 -type d -name '[0-9]*T[0-9]*Z-*' | wc -l) state snapshot(s) in /var/lib/wm-ops"
  else
    fail tools.snapshots "no state snapshot under /var/lib/wm-ops (wm-harden --apply takes one before and one after)"
  fi
  return 0
}

# ------------------------------------------------------------------ main
echo "wm-verify $(date -u +%Y-%m-%dT%H:%M:%SZ) ops_user=$OPS_USER"
chk_ssh
chk_firewall
chk_updates
chk_fail2ban
chk_time
chk_logs
chk_accounts
chk_dirs
chk_tencent
chk_tools
manual console.baseline-snapshot "Console: a baseline snapshot exists, and a new one was taken before this change (instance details -> Snapshots)"
manual console.alerts "Console: monthly budget and traffic alerts are set and a test alert was received"
manual console.mfa "Console: main account has MFA, login protection and operation protection"
manual ssh.second-window "Open a NEW ssh window and confirm key login works before closing the old one"
printf 'SUMMARY pass=%d fail=%d info=%d manual=%d\n' "$PASS_N" "$FAIL_N" "$INFO_N" "$MANUAL_N" | tee -a "$LOGFILE"
if (( FAIL_N > 0 )); then exit 1; fi
exit 0
