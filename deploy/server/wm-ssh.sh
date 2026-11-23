#!/bin/bash
# wm-ssh.sh - the ONLY way a conversation reaches the server (Git Bash on the Windows machine).
#
#   bash deploy/server/wm-ssh.sh --script <local script file>   send that script to "bash -s" on the server (standard input)
#   bash deploy/server/wm-ssh.sh -- <one command ...>            run one command on the server
#   bash deploy/server/wm-ssh.sh --put                           copy the fixed set of scripts to ~/wm-init on the server
#
# Every ssh setting is fixed in this file. Anything else on the command line - above all any ssh option - is refused.
#   - the Windows OpenSSH client (ssh.exe and scp.exe), the alias wm-server (host name, user and key live in your own ssh config)
#   - the host key is checked strictly against deploy/server/known_hosts (written by pin-hostkey.sh), and only
#     against that file; your own known_hosts is not read and never changed
#   - no prompts, no agent forwarding, no port forwarding of any kind, short timeouts
# If the host key does not match, or the server cannot be reached (ssh exit status 255), it says so, stops, and returns
# that status unchanged. Do not "fix" it by editing known_hosts: report it.
#
# --put WRITES on the server, so it is only run after the owner has said yes. It takes no arguments and handles exactly
# these fixed files, taken from the folder this script lives in: wm-harden.sh, wm-snapshot.sh, wm-snapshot-diff.sh, wm-batch1.sh,
# wm-verify.sh. Steps: (1) mkdir -p -m 700 ~/wm-init and chmod 700 on it (it is a directory root will run scripts from,
# so nobody else may write there); (2) scp.exe copies those files into it; (3) sha256sum is run on the server and
# every hash is compared with the one computed here. Any difference, or any failing step, ends with a non-zero status.
#
# Every call appends one line to deploy/server/state/ssh-calls.log:
#   local time with zone <TAB> usage <TAB> command text, or script path and SHA-256 (--put: every SHA-256) <TAB> exit status
# The output of the call is never logged.
#
# Exit: the exit status of ssh (and so of the remote command) | 1 --put: a copy or a hash check failed | 2 refused or wrong usage
# (the stop message below is printed for status 255)

set -u -o pipefail
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

readonly SSH=/c/Windows/System32/OpenSSH/ssh.exe
readonly SCP=/c/Windows/System32/OpenSSH/scp.exe
readonly ALIAS=wm-server
readonly PUT_FILES=(wm-harden.sh wm-snapshot.sh wm-snapshot-diff.sh wm-verify.sh wm-batch1.sh coscli-v1.0.9-linux-amd64)
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly KNOWN_HOSTS="$HERE/known_hosts"
readonly STATE_DIR="$HERE/state"
readonly CALL_LOG="$STATE_DIR/ssh-calls.log"

LOG_USAGE="refused"
LOG_DETAIL="$*"
RC=2

# Keep the log one line per call.
oneline() {
  local s=$1
  s=${s//$'\r'/ }
  s=${s//$'\n'/ }
  s=${s//$'\t'/ }
  printf '%s' "$s"
}

log_call() {
  mkdir -p -- "$STATE_DIR" 2>/dev/null || return 0
  printf '%s\t%s\t%s\t%s\n' "$(date +%Y-%m-%dT%H:%M:%S%z)" "$LOG_USAGE" "$(oneline "$LOG_DETAIL")" "$RC" >>"$CALL_LOG" 2>/dev/null || true
}
trap log_call EXIT

usage() {
  cat >&2 <<'EOF'
Usage: bash wm-ssh.sh --script <local script file>
       bash wm-ssh.sh -- <one command ...>
       bash wm-ssh.sh --put
Nothing else is accepted (no ssh options; --put takes no arguments).
EOF
}

refuse() {
  echo "REFUSED: $1" >&2
  usage
  RC=2
  exit 2
}

stop_255() {
  echo "已停止：主机指纹对不上或连不上，不要改 known_hosts，报告组长" >&2
}

if (( $# == 0 )); then refuse "no arguments"; fi

mode=""
script=""
case $1 in
  --script)
    if (( $# != 2 )); then refuse "--script takes exactly one file"; fi
    script=$2
    if [[ ! -f $script ]]; then refuse "not a file: $script"; fi
    mode=script
    ;;
  --put)
    if (( $# != 1 )); then refuse "--put takes no arguments (the file list is fixed)"; fi
    mode=put
    ;;
  --)
    shift
    if (( $# == 0 )); then refuse "-- needs a command"; fi
    if [[ $1 == -* ]]; then refuse "the command must not start with a dash"; fi
    mode=command
    ;;
  *)
    refuse "only '--script FILE', '--put' and '-- COMMAND ...' are accepted"
    ;;
esac

if [[ ! -f $KNOWN_HOSTS ]]; then
  echo "STOP: $KNOWN_HOSTS does not exist. Run pin-hostkey.sh first." >&2
  RC=2
  exit 2
fi
kh=$(cygpath -w "$KNOWN_HOSTS")

OPTS=(
  -o BatchMode=yes
  -o StrictHostKeyChecking=yes
  -o HostKeyAlias=wm-server
  -o "UserKnownHostsFile=$kh"
  -o "GlobalKnownHostsFile=$kh"
  -o UpdateHostKeys=no
  -o ForwardAgent=no
  -o ClearAllForwardings=yes
  -o ConnectTimeout=15
  -o ServerAliveInterval=30
)

if [[ $mode == script ]]; then
  sha=$(sha256sum <"$script" | cut -d' ' -f1)
  LOG_USAGE="--script"
  LOG_DETAIL="$(cd "$(dirname "$script")" && pwd)/$(basename "$script") sha256=$sha"
  "$SSH" "${OPTS[@]}" "$ALIAS" 'bash -s' <"$script"
  RC=$?
elif [[ $mode == command ]]; then
  LOG_USAGE="--"
  LOG_DETAIL="$*"
  "$SSH" "${OPTS[@]}" "$ALIAS" "$@" </dev/null
  RC=$?
else
  # ---- --put: the fixed files to ~/wm-init, then compare the hashes on both sides
  declare -A SHA=()
  LOG_USAGE="--put"
  LOG_DETAIL=""
  for f in "${PUT_FILES[@]}"; do
    if [[ ! -f $HERE/$f ]]; then refuse "missing file: $HERE/$f"; fi
    SHA[$f]=$(sha256sum <"$HERE/$f" | cut -d' ' -f1)
    LOG_DETAIL+="$f=${SHA[$f]} "
  done
  if [[ ! -x $SCP ]]; then echo "STOP: $SCP not found (Windows OpenSSH client is needed)" >&2; RC=2; exit 2; fi
  ssh_win=$(cygpath -w "$SSH")

  echo "put 1/3: creating ~/wm-init (mode 700) on the server"
  "$SSH" "${OPTS[@]}" "$ALIAS" 'mkdir -p -m 700 ~/wm-init && chmod 700 ~/wm-init' </dev/null
  RC=$?
  if (( RC != 0 )); then
    echo "ERROR: could not create ~/wm-init on the server (exit status $RC); nothing was copied" >&2
    if (( RC == 255 )); then stop_255; fi
    exit "$RC"
  fi

  echo "put 2/3: copying ${PUT_FILES[*]}"
  ( cd "$HERE" && "$SCP" "${OPTS[@]}" -S "$ssh_win" "${PUT_FILES[@]}" "$ALIAS:wm-init/" )
  RC=$?
  if (( RC != 0 )); then
    echo "ERROR: scp failed (exit status $RC)" >&2
    if (( RC == 255 )); then stop_255; fi
    exit "$RC"
  fi

  echo "put 3/3: comparing SHA-256 on the server with the local files"
  remote=""
  remote=$("$SSH" "${OPTS[@]}" "$ALIAS" "cd ~/wm-init && sha256sum ${PUT_FILES[*]}" </dev/null)
  RC=$?
  remote=${remote//$'\r'/}
  if (( RC != 0 )); then
    echo "ERROR: could not read the hashes on the server (exit status $RC)" >&2
    if (( RC == 255 )); then stop_255; fi
    exit "$RC"
  fi
  bad=0
  for f in "${PUT_FILES[@]}"; do
    rh=$(awk -v n="$f" '$2 == n { print $1; exit }' <<<"$remote")
    if [[ $rh == "${SHA[$f]}" ]]; then
      echo "same   $f  ${SHA[$f]}"
    else
      echo "DIFFER $f  local=${SHA[$f]}  server=${rh:-<missing>}" >&2
      bad=1
    fi
  done
  if (( bad )); then
    echo "ERROR: at least one file on the server differs from the local one; do not run anything from ~/wm-init" >&2
    RC=1
    exit 1
  fi
  echo "OK: all files are in ~/wm-init on the server and every SHA-256 matches"
  RC=0
fi

if (( RC == 255 )); then
  stop_255
fi
exit "$RC"
