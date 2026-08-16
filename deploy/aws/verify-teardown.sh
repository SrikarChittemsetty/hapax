#!/usr/bin/env bash
# Confirm `terraform destroy` actually left nothing billable behind.
#
# Terraform reporting success is not the same as the account being clean: a
# final snapshot, an unattached volume or a stray Elastic IP all survive a
# destroy and all keep billing. This asks AWS directly.
#
#   ./verify-teardown.sh [region]
set -uo pipefail

REGION="${1:-us-east-1}"
PROJECT="${PROJECT:-hapax}"
FOUND=0

say() { printf '%-34s %s\n' "$1" "$2"; }

# Fail loudly if we cannot talk to AWS at all. Without this the checks below
# would each fail silently, report an empty result, and print "clean" — a
# teardown verifier that says everything is fine because it could not look is
# considerably worse than no verifier.
if ! aws sts get-caller-identity --region "$REGION" >/dev/null 2>&1; then
  echo "ERROR: cannot authenticate to AWS in $REGION." >&2
  echo "       Configure credentials first; this script must never report" >&2
  echo "       'clean' when it was simply unable to check." >&2
  exit 2
fi

# A query that errors returns empty, and empty must not be read as zero.
run() { # description, aws-command...
  local out
  if ! out=$("${@:2}" 2>/dev/null) || [ -z "$out" ] || [ "$out" = "None" ]; then
    say "$1" "ERROR — could not check"
    FOUND=$((FOUND + 1))
    return
  fi
  if [ "$out" -gt 0 ] 2>/dev/null; then
    say "$1" "FOUND $out  <-- investigate"
    FOUND=$((FOUND + 1))
  else
    say "$1" "clean"
  fi
}

echo "Checking region $REGION for leftovers from project '$PROJECT'"
echo

run "unattached EBS volumes" aws ec2 describe-volumes --region "$REGION" \
  --filters Name=status,Values=available --query 'length(Volumes)' --output text

run "Elastic IPs" aws ec2 describe-addresses --region "$REGION" \
  --query 'length(Addresses)' --output text

run "EC2 instances (incl. stopped)" aws ec2 describe-instances --region "$REGION" \
  --filters "Name=tag:Project,Values=$PROJECT" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'length(Reservations[].Instances[])' --output text

run "RDS instances" aws rds describe-db-instances --region "$REGION" \
  --query "length(DBInstances[?starts_with(DBInstanceIdentifier, '$PROJECT')])" --output text

# Both tabs: manual snapshots outlive the database, automated ones should not.
run "RDS manual snapshots" aws rds describe-db-snapshots --region "$REGION" \
  --snapshot-type manual \
  --query "length(DBSnapshots[?starts_with(DBInstanceIdentifier, '$PROJECT')])" --output text

run "RDS automated snapshots" aws rds describe-db-snapshots --region "$REGION" \
  --snapshot-type automated \
  --query "length(DBSnapshots[?starts_with(DBInstanceIdentifier, '$PROJECT')])" --output text

run "ECR repositories" aws ecr describe-repositories --region "$REGION" \
  --query "length(repositories[?repositoryName=='$PROJECT'])" --output text

run "SSM parameters" aws ssm describe-parameters --region "$REGION" \
  --query "length(Parameters[?starts_with(Name, '/$PROJECT/')])" --output text

run "anything tagged Project=$PROJECT" aws resourcegroupstaggingapi get-resources \
  --region "$REGION" --tag-filters "Key=Project,Values=$PROJECT" \
  --query 'length(ResourceTagMappingList)' --output text

echo
if [ "$FOUND" -eq 0 ]; then
  echo "Clean. Check Billing > Free tier tomorrow to confirm month-to-date stopped climbing."
else
  echo "$FOUND check(s) found something or could not be run."
  echo "Nothing here deletes anything — inspect and remove by hand."
  exit 1
fi
