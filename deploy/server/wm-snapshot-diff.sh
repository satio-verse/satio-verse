#!/bin/bash
# wm-snapshot-diff.sh - compare two wm-snapshot directories (read-only).
#
# Installed by wm-harden.sh as /usr/local/sbin/wm-snapshot-diff (owner root). The snapshots it compares are the
# ones written by "sudo wm-snapshot --save LABEL" (see wm-snapshot.sh). Two outputs of the read-only mode
# (wm-snapshot.sh without options, kept on your own computer) can simply be compared with an ordinary diff.
#
#   sudo wm-snapshot-diff                 compare the two NEWEST snapshots (OLD = older, NEW = newer)
#   wm-snapshot-diff [--all] OLD NEW      compare two given snapshot directories (run as root; both must
#                                         be under /var/lib/wm-ops)
#
# Files whose content changes all the time (disk and memory, fail2ban counters, time sync, update counts,
# ssh failure counts) are only reported as "CHANGED (volatile)" unless --all is given.
# 00-meta.txt is never compared.
#
# Exit: 0 no differences, 1 differences found, 2 usage / not root / missing snapshots.
# Output is plain ASCII.

set -euo pipefail
export LC_ALL=C
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
unset BASH_ENV ENV CDPATH
IFS=$' \t\n'
umask 077

readonly BASE=/var/lib/wm-ops
readonly VOLATILE_RE='^(95-disk|96-fail2ban|97-time|98-updates|99-ssh-failures)\.txt$'
MAX_LINES="${WM_DIFF_MAX_LINES:-60}"

usage() {
  cat <<'EOF'
Usage: wm-snapshot-diff                    compare the two newest snapshots
       wm-snapshot-diff [--all] OLD NEW   compare two snapshot directories under /var/lib/wm-ops
Must run as root (sudo). Exit: 0 same, 1 different, 2 error.
EOF
}

show_all=0
args=()
for a in "$@"; do
  case $a in
    --all) show_all=1 ;;
    -h|--help) usage; exit 0 ;;
    *) args+=("$a") ;;
  esac
done

if (( EUID != 0 )); then echo "ERROR: wm-snapshot-diff must run as root (use sudo)" >&2; exit 2; fi
if [[ ! $MAX_LINES =~ ^[0-9]+$ ]]; then echo "ERROR: WM_DIFF_MAX_LINES must be a number" >&2; exit 2; fi

if (( ${#args[@]} == 0 )); then
  mapfile -t snaps < <(find "$BASE" -mindepth 1 -maxdepth 1 -type d -name '[0-9]*T[0-9]*Z-*' 2>/dev/null | sort)
  if (( ${#snaps[@]} < 2 )); then
    echo "ERROR: need at least two snapshots under $BASE (found ${#snaps[@]})" >&2
    exit 2
  fi
  old=${snaps[${#snaps[@]}-2]}
  new=${snaps[${#snaps[@]}-1]}
elif (( ${#args[@]} == 2 )); then
  old=$(readlink -f -- "${args[0]}")
  new=$(readlink -f -- "${args[1]}")
  for d in "$old" "$new"; do
    case $d in
      "$BASE"/*) ;;
      *) echo "ERROR: $d is not under $BASE" >&2; exit 2 ;;
    esac
    if [[ ! -d $d ]]; then echo "ERROR: not a directory: $d" >&2; exit 2; fi
  done
else
  usage >&2
  exit 2
fi

printf 'OLD: %s\nNEW: %s\n' "$old" "$new"

changed=0
hidden=0
mapfile -t names < <({ ls -1 "$old"; ls -1 "$new"; } | sort -u)
for f in "${names[@]}"; do
  [[ $f == *.txt ]] || continue
  [[ $f == 00-meta.txt ]] && continue
  a="$old/$f"
  b="$new/$f"
  if [[ ! -e $a ]]; then echo "ADDED    $f (only in NEW)"; changed=1; continue; fi
  if [[ ! -e $b ]]; then echo "REMOVED  $f (only in OLD)"; changed=1; continue; fi
  if cmp -s "$a" "$b"; then continue; fi
  if [[ $f =~ $VOLATILE_RE ]] && (( ! show_all )); then
    echo "CHANGED  $f (volatile file, diff hidden; use --all)"
    hidden=$((hidden + 1))
    continue
  fi
  echo "CHANGED  $f"
  changed=1
  diff -u --label "OLD/$f" --label "NEW/$f" "$a" "$b" | head -n "$MAX_LINES" || true
  echo
done

if (( changed == 0 )); then
  echo "NO DIFFERENCES (volatile files that changed: $hidden)"
  exit 0
fi
echo "DIFFERENCES FOUND"
exit 1
