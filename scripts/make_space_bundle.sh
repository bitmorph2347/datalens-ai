#!/usr/bin/env bash
# Collects exactly the files a Hugging Face Space needs into build/space/.
# Run from the project root:  bash scripts/make_space_bundle.sh
set -euo pipefail

OUT="build/space"
mkdir -p "$OUT/data"

cp Dockerfile .dockerignore requirements.txt "$OUT/"
cp deploy/huggingface/README.md "$OUT/README.md"      # the Space card (has the required YAML header)
cp data/players_meta.csv data/prices_sample.csv "$OUT/data/"
cp -R datalens web "$OUT/"
find "$OUT" -name "__pycache__" -type d -prune -exec rm -rf {} +

echo "Bundle ready in $OUT:"
ls -1 "$OUT"
echo
echo "Next: follow docs/DEPLOY.md (upload this folder to your Hugging Face Space)."
