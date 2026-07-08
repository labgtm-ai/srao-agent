#!/usr/bin/env bash
# ============================================================
# deploy.sh — SRAO Agent deployment to Google Cloud
# ============================================================
# Usage:
#   chmod +x deploy.sh
#   ./deploy.sh
#
# Prerequisites:
#   - gcloud CLI installed and authenticated
#   - Docker installed (for local builds) or Artifact Registry access
#   - GitHub token ready

set -euo pipefail

# ── Configuration — EDIT THESE ──────────────────────────────
PROJECT_ID="your-gcp-project-id"
REGION="us-central1"
SERVICE_NAME="srao-agent"
IMAGE_NAME="srao-agent"
REPO_NAME="srao-docker"           # Artifact Registry repo name
GITHUB_TOKEN="ghp_YOUR_TOKEN_HERE"
# ────────────────────────────────────────────────────────────

IMAGE_URI="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO_NAME}/${IMAGE_NAME}:latest"
SA_NAME="srao-agent"
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  SRAO Agent — Google Cloud Deployment"
echo "  Project: ${PROJECT_ID}  |  Region: ${REGION}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# ── Step 1: Set project ─────────────────────────────────────
echo ""
echo "[1/8] Setting GCP project..."
gcloud config set project "${PROJECT_ID}"

# ── Step 2: Enable APIs ─────────────────────────────────────
echo ""
echo "[2/8] Enabling required APIs..."
gcloud services enable \
  aiplatform.googleapis.com \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  cloudbuild.googleapis.com \
  --quiet

echo "      ✓ APIs enabled"

# ── Step 3: Create service account ─────────────────────────
echo ""
echo "[3/8] Creating service account..."
gcloud iam service-accounts create "${SA_NAME}" \
  --display-name="SRAO Agent Service Account" \
  --quiet 2>/dev/null || echo "      (Service account already exists)"

# Grant required roles
for ROLE in \
  "roles/aiplatform.user" \
  "roles/secretmanager.secretAccessor" \
  "roles/logging.logWriter" \
  "roles/monitoring.metricWriter"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --quiet
done
echo "      ✓ Service account configured"

# ── Step 4: Store GitHub token in Secret Manager ────────────
echo ""
echo "[4/8] Storing GitHub token in Secret Manager..."
echo -n "${GITHUB_TOKEN}" | gcloud secrets create github-token \
  --data-file=- \
  --quiet 2>/dev/null || \
echo -n "${GITHUB_TOKEN}" | gcloud secrets versions add github-token \
  --data-file=- \
  --quiet
echo "      ✓ Secret stored"

# ── Step 5: Create Artifact Registry repo ──────────────────
echo ""
echo "[5/8] Creating Artifact Registry repository..."
gcloud artifacts repositories create "${REPO_NAME}" \
  --repository-format=docker \
  --location="${REGION}" \
  --description="SRAO Agent Docker images" \
  --quiet 2>/dev/null || echo "      (Repository already exists)"
echo "      ✓ Artifact Registry ready"

# ── Step 6: Build & push Docker image ──────────────────────
echo ""
echo "[6/8] Building and pushing Docker image..."
gcloud auth configure-docker "${REGION}-docker.pkg.dev" --quiet

gcloud builds submit \
  --tag "${IMAGE_URI}" \
  --machine-type="e2-highcpu-8" \
  --timeout="20m" \
  .

echo "      ✓ Image pushed: ${IMAGE_URI}"

# ── Step 7: Deploy to Cloud Run ─────────────────────────────
echo ""
echo "[7/8] Deploying to Cloud Run..."
gcloud run deploy "${SERVICE_NAME}" \
  --image="${IMAGE_URI}" \
  --region="${REGION}" \
  --service-account="${SA_EMAIL}" \
  --memory="2Gi" \
  --cpu="2" \
  --timeout="3600" \
  --max-instances="5" \
  --set-env-vars="GCP_PROJECT_ID=${PROJECT_ID},GCP_LOCATION=${REGION},MODE=server" \
  --set-secrets="GITHUB_TOKEN=github-token:latest" \
  --no-allow-unauthenticated \
  --quiet

SERVICE_URL=$(gcloud run services describe "${SERVICE_NAME}" \
  --region="${REGION}" \
  --format="value(status.url)")

echo "      ✓ Cloud Run service deployed: ${SERVICE_URL}"

# ── Step 8: Test health endpoint ────────────────────────────
echo ""
echo "[8/8] Testing health endpoint..."
TOKEN=$(gcloud auth print-identity-token)
HTTP_STATUS=$(curl -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer ${TOKEN}" \
  "${SERVICE_URL}/health")

if [ "${HTTP_STATUS}" == "200" ]; then
  echo "      ✓ Health check passed (HTTP ${HTTP_STATUS})"
else
  echo "      ⚠ Health check returned HTTP ${HTTP_STATUS} — check Cloud Run logs"
fi

# ── Done ─────────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Deployment complete!"
echo "  Service URL: ${SERVICE_URL}"
echo ""
echo "  To run locally (interactive mode):"
echo "    python main.py"
echo ""
echo "  To trigger modernisation via API:"
echo "    curl -X POST ${SERVICE_URL}/modernise \\"
echo "      -H 'Authorization: Bearer \$(gcloud auth print-identity-token)' \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -d '{\"repo_url\":\"https://github.com/org/repo.git\",\"branch\":\"main\",\"github_owner\":\"org\",\"github_repo\":\"repo\"}'"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
