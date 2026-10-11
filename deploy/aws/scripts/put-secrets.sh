#!/usr/bin/env bash
# Store the Cerberus secrets in AWS SSM Parameter Store (SecureString, encrypted with the AWS-managed key).
# Run from your machine with AWS credentials configured. Values are read from files / prompts so they
# never appear in shell history or Terraform state.
#
#   ./put-secrets.sh path/to/github-app.private-key.pem
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
PREFIX="${SSM_PREFIX:-/cerberus}"
PEM="${1:?usage: put-secrets.sh <github-app-private-key.pem>}"

put() { # name value
  aws ssm put-parameter --region "$REGION" --name "$PREFIX/$1" --type SecureString --overwrite --value "$2" >/dev/null
  echo "stored $PREFIX/$1"
}

read -r -p "GitHub App ID: " APP_ID
read -r -s -p "GitHub webhook secret: " WEBHOOK_SECRET; echo
put github_app_id "$APP_ID"
put github_webhook_secret "$WEBHOOK_SECRET"
put github_app_private_key "$(cat "$PEM")"

for name in openrouter_api_key gemini_api_key anthropic_api_key llm_api_key langfuse_public_key langfuse_secret_key; do
  read -r -s -p "$name (blank to skip): " value; echo
  [ -n "$value" ] && put "$name" "$value"
done
echo "Done. The instance reads these at boot; after changing them run ./update.sh."
