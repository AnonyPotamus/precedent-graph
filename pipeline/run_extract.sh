#!/usr/bin/env bash
# Extract every downloaded year: tar -> PDFs -> text -> work/extracted/<year>.jsonl
# Usage: bash run_extract.sh [years...]   (default: every data/tar/english-*.tar not yet extracted)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
SCRATCH="${SCRATCH:-$HOME/scratch}"
mkdir -p "$ROOT/work/extracted"
years=("$@")
if [ ${#years[@]} -eq 0 ]; then
  for t in "$ROOT"/data/tar/english-*.tar; do y=$(basename "$t" .tar); years+=("${y#english-}"); done
fi
for y in "${years[@]}"; do
  out="$ROOT/work/extracted/$y.jsonl"
  [ -s "$out" ] && [ -z "${FORCE:-}" ] && { echo "skip $y"; continue; }
  rm -rf "$SCRATCH/pdf/$y" "$SCRATCH/txt/$y"; mkdir -p "$SCRATCH/pdf/$y" "$SCRATCH/txt/$y"
  tar -xf "$ROOT/data/tar/english-$y.tar" -C "$SCRATCH/pdf/$y" || echo "warn: tar $y incomplete"
  find "$SCRATCH/pdf/$y" -name '*.pdf' -print0 | xargs -0 -P4 -I{} sh -c 'pdftotext -layout "$1" "$2/$(basename "$1" .pdf).txt" 2>/dev/null || true' _ {} "$SCRATCH/txt/$y"
  python3 "$ROOT/precedent-graph/pipeline/extract.py" "$SCRATCH/txt/$y" "$out"
  rm -rf "$SCRATCH/pdf/$y" "$SCRATCH/txt/$y"
done
