#!/usr/bin/env bash
# Pull the latest code on the instance and restart the App, via SSM Run Command (no SSH).
#
#   ./update.sh <instance-id> [git-ref]
set -euo pipefail

REGION="${AWS_REGION:-ap-south-1}"
INSTANCE="${1:?usage: update.sh <instance-id> [git-ref]}"
REF="${2:-upgrade/final}"

aws ssm send-command --region "$REGION" --instance-ids "$INSTANCE" \
  --document-name AWS-RunShellScript \
  --comment "Cerberus update to $REF" \
  --parameters commands="[
    \"cd /opt/cerberus/src\",
    \"sudo -u cerberus git fetch --depth 1 origin $REF\",
    \"sudo -u cerberus git checkout -q FETCH_HEAD\",
    \"sudo -u cerberus /opt/cerberus/venv/bin/pip install -q -r autonomous-pr-fixer/requirements.txt\",
    \"systemctl restart cerberus-app\",
    \"systemctl --no-pager status cerberus-app | head -5\"
  ]" \
  --query Command.CommandId --output text
