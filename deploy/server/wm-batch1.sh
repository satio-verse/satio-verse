#!/bin/bash
# wm-batch1.sh - server batch 1 (2026-11-16): install coscli and create the directory layout. DRAFT.
#
#   bash ~/wm-init/wm-batch1.sh --check     read-only: print what WOULD change, write nothing (exit 10 if something would)
#   bash ~/wm-init/wm-batch1.sh --apply     do it (safe to run again; unchanged things are skipped)
#
# What --apply does:
#   coscli   v1.0.9 linux-amd64 from the official release page github.com/tencentyun/coscli (Apache-2.0, ~13.6 MB),
#            downloaded on the owner's PC (the server's own download broke off twice), copied next to this script by
#            "wm-ssh.sh --put", checked against the SHA-256 pinned below (taken 2026-11-16 from the release's
#            sha256sum.log over two different network paths), then installed as /usr/local/bin/coscli (root, 0755).
#            A wrong hash stops the script and installs nothing. No key is configured here: the owner runs
#            "coscli config init" himself in the console web terminal.
#   dirs     /srv/worldmodel/site, updates, community, community/store   owner ubuntu, group wm, 2750
#            /srv/worldmodel/community/private                          owner root,   group wm, 0750 (author tokens)
# It changes nothing else: no package manager, no service, no ssh, no firewall.
# Log (apply only): /var/log/wm-ops/batch1.log
# Exit: 0 ok (check: nothing to change) | 10 check found changes | 1 error | 2 usage | 3 refused

set -Eeuo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
umask 022

readonly COSCLI_VERSION=v1.0.9
readonly COSCLI_URL="https://github.com/tencentyun/coscli/releases/download/${COSCLI_VERSION}/coscli-${COSCLI_VERSION}-linux-amd64"
readonly COSCLI_SHA256=a07de5ba2800147a700ed29036b0c76a4229088cee68e1682d0eae19b638a915
readonly COSCLI_BIN=/usr/local/bin/coscli
readonly OPS_USER=ubuntu
readonly SRV=/srv/worldmodel
readonly LOGFILE=/var/log/wm-ops/batch1.log

ACTION=""
case "${1:-}" in
  --check) ACTION=check ;;
  --apply) ACTION=apply ;;
  *) echo "usage: bash wm-batch1.sh --check | --apply" >&2; exit 2 ;;
esac
(( $# == 1 )) || { echo "usage: exactly one of --check / --apply" >&2; exit 2; }

if (( EUID != 0 )); then
  [[ $(id -un) == "$OPS_USER" ]] || { echo "ERROR: run as $OPS_USER" >&2; exit 3; }
  sudo -n true 2>/dev/null || { echo "ERROR: sudo -n true failed" >&2; exit 3; }
  exec sudo -n bash "$(readlink -f -- "${BASH_SOURCE[0]}")" "$@"
fi

if [[ $ACTION == apply ]]; then install -d -m 0750 -o root -g root /var/log/wm-ops; touch "$LOGFILE"; chmod 0640 "$LOGFILE"; fi
log() { local l; l="$(date -u +%Y-%m-%dT%H:%M:%SZ) $*"; printf '%s\n' "$l"; if [[ $ACTION == apply ]]; then printf '%s\n' "$l" >>"$LOGFILE"; fi; }
CHANGES=0
TMP=""
trap '[[ -n $TMP ]] && rm -f -- "$TMP"' EXIT
trap 'log "ERROR at line $LINENO: $BASH_COMMAND"' ERR

log "wm-batch1 action=$ACTION sha256(self)=$(sha256sum <"$(readlink -f -- "${BASH_SOURCE[0]}")" | cut -d' ' -f1)"
getent group wm >/dev/null || { log "ERROR: group wm does not exist (run wm-harden --apply first)"; exit 3; }
[[ -d $SRV ]] || { log "ERROR: $SRV does not exist (run wm-harden --apply first)"; exit 3; }
[[ $(uname -m) == x86_64 ]] || { log "ERROR: this script pins the linux-amd64 build, machine is $(uname -m)"; exit 3; }

# ---- coscli
if [[ -x $COSCLI_BIN ]] && [[ $(sha256sum <"$COSCLI_BIN" | cut -d' ' -f1) == "$COSCLI_SHA256" ]] && [[ $(stat -c '%a %U:%G' "$COSCLI_BIN") == "755 root:root" ]]; then
  log "OK coscli $COSCLI_VERSION already installed at $COSCLI_BIN"
else
  CHANGES=$((CHANGES + 1))
  # 2026-11-16: downloading from GitHub on the server broke off twice (curl 56), so the file is downloaded on the
  # owner's PC from $COSCLI_URL, checked there, and copied next to this script by "wm-ssh.sh --put".
  # It is checked again here against the same pinned SHA-256 before anything is installed.
  src="$(dirname -- "$(readlink -f -- "${BASH_SOURCE[0]}")")/coscli-${COSCLI_VERSION}-linux-amd64"
  [[ -f $src ]] || { log "ERROR: $src not found (copy it with wm-ssh.sh --put first)"; exit 1; }
  got=$(sha256sum <"$src" | cut -d' ' -f1)
  if [[ $got != "$COSCLI_SHA256" ]]; then log "ERROR: SHA-256 of $src is $got, expected $COSCLI_SHA256; nothing installed"; exit 1; fi
  if [[ $ACTION == check ]]; then
    log "CHANGE would install $src (SHA-256 $got, matches the pinned value) as $COSCLI_BIN (root, 0755)"
  else
    install -m 0755 -o root -g root "$src" "$COSCLI_BIN"
    log "CHANGE installed $COSCLI_BIN ($($COSCLI_BIN --version 2>&1 | head -n 1))"
  fi
fi

# ---- directories
ensure_dir() {
  local d=$1 mode=$2 owner=$3 group=$4
  if [[ -d $d ]] && [[ $(stat -c '%a %U %G' "$d") == "$mode $owner $group" ]]; then log "OK $d ($mode $owner:$group)"; return 0; fi
  CHANGES=$((CHANGES + 1))
  if [[ $ACTION == check ]]; then log "CHANGE would set up $d ($mode $owner:$group)"; return 0; fi
  install -d -o "$owner" -g "$group" "$d"; chmod "$mode" "$d"
  log "CHANGE directory $d ($mode $owner:$group)"
}
ensure_dir "$SRV/site" 2750 "$OPS_USER" wm
ensure_dir "$SRV/updates" 2750 "$OPS_USER" wm
ensure_dir "$SRV/community" 2750 "$OPS_USER" wm
ensure_dir "$SRV/community/store" 2750 "$OPS_USER" wm
ensure_dir "$SRV/community/private" 750 root wm

log "summary action=$ACTION changes=$CHANGES"
if [[ $ACTION == check && $CHANGES -gt 0 ]]; then exit 10; fi
exit 0
