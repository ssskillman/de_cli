#!/usr/bin/env bash
# Sign in to gcloud and print the live Territory Planner Cloud Run revision.
# auth login opens a browser.
set -euo pipefail

ACCOUNT="shawn.skillman@iterable.com"
PROJECT="udi-uat-504519"
SERVICE="udi-territory-planner"
REGION="us-central1"

if ! command -v gcloud >/dev/null 2>&1; then
  echo "ERROR: gcloud not found on PATH." >&2
  exit 1
fi

gcloud auth login "$ACCOUNT"
gcloud config set account "$ACCOUNT"
gcloud config set project "$PROJECT"
gcloud auth list
gcloud run services describe "$SERVICE" \
  --region="$REGION" \
  --format='value(status.latestReadyRevisionName)'
