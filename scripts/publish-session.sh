#!/usr/bin/env bash
#
# Put a recorded session front and centre in the repo.
#
# Pulls a session off the Pi into data/latest, compresses the CSVs, and
# regenerates the summary and overview plot that the README points at. Run it,
# commit, push — the newest recording is then what people see first.
#
# Usage:
#   ./scripts/publish-session.sh                  # the newest session on the Pi
#   ./scripts/publish-session.sh 20260731-120041_calmfocus
#   PI=pi@192.168.8.2 ./scripts/publish-session.sh
#
# The previous contents of data/latest are replaced. Sessions stay on the Pi,
# so nothing is lost by republishing.

set -euo pipefail

PI="${PI:-pi@192.168.8.2}"
REMOTE_DIR="${REMOTE_DIR:-/var/lib/muse-brain/recordings}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="$REPO/data/latest"

fail() { echo "error: $*" >&2; exit 1; }

name="${1:-}"
if [ -z "$name" ]; then
  echo "Finding the newest session on $PI ..."
  name=$(ssh -o BatchMode=yes "$PI" "sudo ls -1t $REMOTE_DIR | head -1") \
    || fail "could not reach $PI over ssh"
fi
[ -n "$name" ] || fail "no sessions found in $REMOTE_DIR"
echo "Publishing: $name"

rm -rf "$DEST"
mkdir -p "$DEST"

# sudo on the far side: recordings belong to the service user.
ssh -o BatchMode=yes "$PI" "sudo tar cf - -C '$REMOTE_DIR' '$name'" \
  | tar xf - -C "$DEST" --strip-components=1 \
  || fail "could not copy $name from $PI"

[ -f "$DEST/meta.json" ] || fail "$name does not look like a session (no meta.json)"

echo "$name" > "$DEST/SESSION_ID"

# Summarise before compressing: the tooling reads either form, but plotting
# uncompressed is quicker and this only runs once per publish.
PY="$REPO/.venv/bin/python"
[ -x "$PY" ] || PY="python3"
"$PY" "$REPO/analysis/summarize.py" "$DEST" || fail "summary generation failed"

# Compress the bulk streams. EEG is ~1.5 MB per minute uncompressed and this is
# a git repo people will clone; gzip takes roughly 80% off and every tool in
# analysis/ reads .gz transparently.
for f in "$DEST"/*.csv; do
  [ -e "$f" ] || continue
  case "$(basename "$f")" in
    seats.csv|cues.csv|gaps.csv) continue ;;  # tiny, and nice to read in the browser
  esac
  gzip -9 "$f"
done

echo
echo "Published to data/latest:"
ls -lh "$DEST" | awk 'NR>1 {printf "  %-22s %s\n", $9, $5}'
echo
echo "Next: review data/latest/SUMMARY.md, then"
echo "  git add data/latest && git commit -m 'data: publish session $name' && git push"
