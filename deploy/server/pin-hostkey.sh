#!/bin/bash
# pin-hostkey.sh - write deploy/server/known_hosts, the host-key file that ONLY wm-ssh.sh uses (Git Bash).
#
#   bash deploy/server/pin-hostkey.sh 'SHA256:<expected ed25519 host key fingerprint>'
#
# What it does:
#   1. asks the Windows ssh for the settings of the alias (ssh.exe -G) to learn host name and port. Those two
#      values are only held in variables. They are never printed, never written to a file.
#   2. fetches the server's ed25519 host key with ssh-keyscan.exe (stderr is thrown away because it names the host).
#      If the Windows build cannot do it (it fails against OpenSSH 9.6), the build from Git for Windows is used.
#   3. replaces the first column by the alias, writes the line to a temporary file, computes its fingerprint with
#      ssh-keygen.exe -lf and compares it, character for character, with the fingerprint you gave.
#   4. only when they are identical the temporary file becomes deploy/server/known_hosts.
#      When they differ (or no key could be fetched) it STOPS and writes nothing.
#
# It does not touch your own ~/.ssh/known_hosts or ~/.ssh/config, and it logs in nowhere.
# Exit: 0 pinned | 1 stopped (fingerprint differs, no key, alias unknown) | 2 usage.

set -u -o pipefail
export MSYS_NO_PATHCONV=1
export MSYS2_ARG_CONV_EXCL='*'

readonly SSH=/c/Windows/System32/OpenSSH/ssh.exe
readonly KEYSCAN=/c/Windows/System32/OpenSSH/ssh-keyscan.exe
readonly KEYSCAN_GIT=/usr/bin/ssh-keyscan.exe
readonly KEYGEN=/c/Windows/System32/OpenSSH/ssh-keygen.exe
readonly ALIAS=wm-server
HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
readonly TARGET="$HERE/known_hosts"

expected=${1:-}
if [[ ! $expected =~ ^SHA256:[A-Za-z0-9+/]{43}$ ]]; then
  echo "usage: bash pin-hostkey.sh 'SHA256:<fingerprint>'" >&2
  exit 2
fi
for x in "$SSH" "$KEYSCAN" "$KEYGEN"; do
  if [[ ! -x $x ]]; then echo "STOP: $x not found (Windows OpenSSH client is needed)" >&2; exit 1; fi
done

tmp=$(mktemp "$HERE/.known_hosts.XXXXXX") || { echo "STOP: cannot create a temporary file" >&2; exit 1; }
trap 'rm -f -- "$tmp"' EXIT

cfg=$("$SSH" -G "$ALIAS" 2>/dev/null)
host=$(awk '$1 == "hostname" { print $2; exit }' <<<"$cfg")
port=$(awk '$1 == "port" { print $2; exit }' <<<"$cfg")
cfg=""
if [[ -z $host || $host == "$ALIAS" ]]; then
  echo "STOP: the ssh alias $ALIAS has no HostName in the ssh configuration" >&2
  exit 1
fi
if [[ $port != 22 ]]; then
  echo "STOP: the port of $ALIAS is not 22; this script only handles port 22 (a non-default port needs the bracket form in known_hosts)" >&2
  exit 1
fi

# The Windows ssh-keyscan (OpenSSH_for_Windows 9.5) stops with "unsupported KEX method sntrup761x25519-sha512"
# against an OpenSSH 9.6 server; the build that ships with Git for Windows does not have that problem.
# The Windows one is tried first, the Git one second. Either way nothing is written before the fingerprint matches.
line=""
used=""
for ks in "$KEYSCAN" "$KEYSCAN_GIT"; do
  [[ -x $ks ]] || continue
  scan=$("$ks" -T 10 -t ed25519 -p "$port" "$host" 2>/dev/null | tr -d '\r')
  line=$(awk -v a="$ALIAS" 'NF >= 3 && $2 == "ssh-ed25519" { print a " " $2 " " $3; exit }' <<<"$scan")
  scan=""
  if [[ -n $line ]]; then used=$ks; break; fi
done
host=""
if [[ -z $line ]]; then
  echo "STOP: could not fetch an ed25519 host key (server unreachable, or it offers no ed25519 key)" >&2
  exit 1
fi
printf '%s\n' "$line" >"$tmp"

got=$("$KEYGEN" -lf "$(cygpath -w "$tmp")" 2>/dev/null | awk 'NR == 1 { print $2 }')
if [[ -z $got ]]; then
  echo "STOP: ssh-keygen could not compute a fingerprint for the fetched key" >&2
  exit 1
fi
if [[ $got != "$expected" ]]; then
  echo "STOP: the fingerprint differs. Nothing was written." >&2
  echo "  expected: $expected" >&2
  echo "  fetched : $got" >&2
  exit 1
fi

mv -f -- "$tmp" "$TARGET"
trap - EXIT
if [[ $used == "$KEYSCAN" ]]; then how="Windows OpenSSH ssh-keyscan"; else how="Git for Windows ssh-keyscan"; fi
echo "OK: fingerprint matches ($got); wrote $(basename "$TARGET") with one line for alias $ALIAS (key fetched with: $how)"
exit 0
