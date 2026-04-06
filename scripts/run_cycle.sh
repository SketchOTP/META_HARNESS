#!/usr/bin/env bash
# Run one Meta-Harness self-improvement cycle.
# Run from the repo root: ./scripts/run_cycle.sh

set -e
cd "$(dirname "$0")/.."

echo "[1/3] Running tests..."
pytest tests/ \
  --junitxml=.metaharness/test_results.xml \
  --cov=. \
  --cov-config=.coveragerc \
  --cov-report=xml:coverage.xml \
  -q || echo "Tests failed — generating metrics for diagnosis anyway"

echo "[2/3] Generating metrics..."
python scripts/generate_metrics.py

echo "[3/3] Running harness cycle..."
metaharness run

echo "Done."
