#!/usr/bin/env bash
# Deploy the shadow worker to Cloud Run Jobs + Cloud Scheduler. Idempotent:
# rerun to roll forward; see docs/deployment.md for setup and rollback.
#
#   PROJECT=my-project ./deploy/gcp/deploy.sh            # deploy
#   DRY_RUN=1 PROJECT=my-project ./deploy/gcp/deploy.sh  # print commands only
#
# Secrets are never passed to this script. Create them once in Secret Manager
# (docs/deployment.md); jobs read them at run time.
set -euo pipefail

: "${PROJECT:?set PROJECT}"
REGION="${REGION:-us-central1}"
REPO="${REPO:-ff-agent}"
TAG="${TAG:-$(git rev-parse --short HEAD)}"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT}/${REPO}/worker:${TAG}"
TICK_SCHEDULE="${TICK_SCHEDULE:-*/10 * * * *}"
HEALTH_SCHEDULE="${HEALTH_SCHEDULE:-5,35 * * * *}"
SCHEDULE_TZ="America/New_York"
RUNTIME_SA="ff-worker@${PROJECT}.iam.gserviceaccount.com"
SCHEDULER_SA="ff-scheduler@${PROJECT}.iam.gserviceaccount.com"
: "${WORKER_USER_ID:?set WORKER_USER_ID (internal users.id UUID)}"
: "${WORKER_TEAM_ID:?set WORKER_TEAM_ID (your ESPN team ID)}"
: "${RAW_ARCHIVE_URL:?set RAW_ARCHIVE_URL (gs://private-bucket/prefix)}"

run() {
    printf '+ %s\n' "$*" >&2
    if [[ "${DRY_RUN:-0}" != 1 ]]; then "$@"; fi
}

exists() { [[ "${DRY_RUN:-0}" != 1 ]] && "$@" >/dev/null 2>&1; }

SECRETS="ESPN_LEAGUE_ID=ESPN_LEAGUE_ID:latest,ESPN_S2=ESPN_S2:latest,ESPN_SWID=ESPN_SWID:latest,DATABASE_URL=DATABASE_URL_RUNTIME:latest"
ENV_VARS="WORKER_USER_ID=${WORKER_USER_ID},WORKER_TEAM_ID=${WORKER_TEAM_ID},RAW_ARCHIVE_URL=${RAW_ARCHIVE_URL},DELIVERY_ENABLED=false,SMS_ENABLED=false,ESPN_ACTIONS_ENABLED=false"

run gcloud config set project "$PROJECT"
run gcloud services enable run.googleapis.com cloudscheduler.googleapis.com \
    secretmanager.googleapis.com artifactregistry.googleapis.com cloudbuild.googleapis.com \
    monitoring.googleapis.com logging.googleapis.com

exists gcloud artifacts repositories describe "$REPO" --location "$REGION" ||
    run gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION"
for sa in ff-worker ff-scheduler; do
    exists gcloud iam service-accounts describe "${sa}@${PROJECT}.iam.gserviceaccount.com" ||
        run gcloud iam service-accounts create "$sa"
done
for secret in ESPN_LEAGUE_ID ESPN_S2 ESPN_SWID DATABASE_URL_RUNTIME; do
    run gcloud secrets add-iam-policy-binding "$secret" \
        --member "serviceAccount:${RUNTIME_SA}" --role roles/secretmanager.secretAccessor
done
run gcloud storage buckets add-iam-policy-binding "gs://$(cut -d/ -f3 <<<"$RAW_ARCHIVE_URL")" \
    --member "serviceAccount:${RUNTIME_SA}" --role roles/storage.objectUser

run gcloud builds submit --tag "$IMAGE" .

deploy_job() {  # name, script, timeout
    run gcloud run jobs deploy "$1" --image "$IMAGE" --region "$REGION" \
        --service-account "$RUNTIME_SA" --args "$2" --tasks 1 --max-retries 0 \
        --task-timeout "$3" --cpu 1 --memory 512Mi \
        --set-secrets "$SECRETS" --set-env-vars "$ENV_VARS"
    run gcloud run jobs add-iam-policy-binding "$1" --region "$REGION" \
        --member "serviceAccount:${SCHEDULER_SA}" --role roles/run.invoker
}
deploy_job ff-tick scripts/scheduled_tick.py 15m
deploy_job ff-health scripts/health_check.py 2m

schedule() {  # job, cron
    local uri="https://run.googleapis.com/v2/projects/${PROJECT}/locations/${REGION}/jobs/$1:run"
    local verb=create
    exists gcloud scheduler jobs describe "$1" --location "$REGION" && verb=update
    run gcloud scheduler jobs "$verb" http "$1" --location "$REGION" --schedule "$2" \
        --time-zone "$SCHEDULE_TZ" --uri "$uri" --http-method POST \
        --oauth-service-account-email "$SCHEDULER_SA"
}
schedule ff-tick "$TICK_SCHEDULE"
schedule ff-health "$HEALTH_SCHEDULE"

exists gcloud logging metrics describe ff_worker_errors ||
    run gcloud logging metrics create ff_worker_errors \
        --description "Failed or unhealthy shadow-worker runs" \
        --log-filter 'resource.type="cloud_run_job" AND resource.labels.job_name=("ff-tick" OR "ff-health") AND severity>=ERROR'
policy_exists() {
    [[ "${DRY_RUN:-0}" != 1 ]] && [[ -n "$(gcloud monitoring policies list \
        --filter 'displayName="ff worker unhealthy"' --format 'value(name)')" ]]
}
if [[ -n "${ALERT_EMAIL:-}" ]] && ! policy_exists; then
    channel="$(run gcloud beta monitoring channels create --display-name "ff worker owner" \
        --type email --channel-labels "email_address=${ALERT_EMAIL}" --format 'value(name)')"
    policy="$(mktemp)"
    sed "s|\"notificationChannels\": \[\]|\"notificationChannels\": [\"${channel:-CHANNEL}\"]|" \
        deploy/gcp/alert-policy.json >"$policy"
    run gcloud monitoring policies create --policy-from-file "$policy"
    rm -f "$policy"
fi
echo "Deployed ${IMAGE}"
