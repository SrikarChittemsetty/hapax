# Deploying Hapax on AWS free tier

Terraform for a single-instance deployment: EC2 running the dispatcher in a
container, RDS Postgres holding the tasks, ECR holding the image. No NAT
gateway, no load balancer, no Fargate, nothing above micro.

**Nothing in this repository has been applied to an AWS account.** The
configuration is validated (`terraform validate`, `terraform fmt`) and the
container is built and tested locally, but no resource has been provisioned.
Read the cost section before you change that.

---

## STOP — set a billing alarm first

Do this **before** `terraform apply`, not after. It takes two minutes and it is
the only thing standing between a misconfiguration and a surprise.

Billing metrics live only in **us-east-1**, regardless of where you deploy.

```bash
# 1. Turn on billing alerts (once per account; console-only setting).
#    Billing console > Billing preferences > check "Receive CloudWatch billing
#    alerts" > Save. The metric does not exist until you do this, and it can
#    take ~24h to start publishing.

# 2. Somewhere to send the alarm.
aws sns create-topic --name billing-alerts --region us-east-1
aws sns subscribe \
  --topic-arn "arn:aws:sns:us-east-1:<ACCOUNT_ID>:billing-alerts" \
  --protocol email --notification-endpoint you@example.com --region us-east-1
# Confirm the subscription from your inbox, or the alarm fires into the void.

# 3. Alarm at $1. Not $10 — this stack should cost nothing, so $1 means
#    something is wrong and you want to know on day one, not at month end.
aws cloudwatch put-metric-alarm \
  --alarm-name hapax-billing-over-1-usd \
  --alarm-description "Anything billable in an account that should cost \$0" \
  --namespace AWS/Billing --metric-name EstimatedCharges \
  --dimensions Name=Currency,Value=USD \
  --statistic Maximum --period 21600 --evaluation-periods 1 \
  --threshold 1 --comparison-operator GreaterThanThreshold \
  --alarm-actions "arn:aws:sns:us-east-1:<ACCOUNT_ID>:billing-alerts" \
  --region us-east-1
```

Also set a zero-spend budget, which catches things the metric alarm is slow to
see. Two budgets per account are free:

```bash
cat > /tmp/budget.json <<'JSON'
{
  "BudgetName": "hapax-zero-spend",
  "BudgetLimit": { "Amount": "1", "Unit": "USD" },
  "TimeUnit": "MONTHLY",
  "BudgetType": "COST"
}
JSON
cat > /tmp/notifications.json <<'JSON'
[{
  "Notification": {
    "NotificationType": "ACTUAL",
    "ComparisonOperator": "GREATER_THAN",
    "Threshold": 1,
    "ThresholdType": "ABSOLUTE_VALUE"
  },
  "Subscribers": [{ "SubscriptionType": "EMAIL", "Address": "you@example.com" }]
}]
JSON
aws budgets create-budget \
  --account-id <ACCOUNT_ID> \
  --budget file:///tmp/budget.json \
  --notifications-with-subscribers file:///tmp/notifications.json
```

**Verify the alarm exists before continuing:**

```bash
aws cloudwatch describe-alarms --alarm-names hapax-billing-over-1-usd \
  --region us-east-1 --query 'MetricAlarms[0].StateValue'
```

---

## Which free tier do you actually have?

This determines whether the deployment is free or merely cheap, and it is not a
detail. AWS changed the model on **15 July 2025**.

| Account created | What you get |
|---|---|
| **Before 2025-07-15** | The legacy **12-month** free tier: 750 h/month EC2 t2.micro, 750 h/month RDS db.t3.micro, 20 GB RDS storage, 30 GB EBS, 500 MB ECR, 750 h/month public IPv4 — *for 12 months from account creation, then it stops without warning.* |
| **On/after 2025-07-15** | No 12-month tier. **$100 credits** on sign-up, up to **$200** total, expiring 12 months from creation. The **Free plan closes the account after 6 months** or when credits run out, and you lose access to resources and data (90-day recovery window). The Paid plan bills normally once credits are gone. |

Check yours:

```bash
aws iam list-account-aliases          # any output confirms credentials work
# Console > Billing and Cost Management > Free tier — shows your usage and,
# for legacy accounts, the remaining months.
```

**If your legacy 12 months have already expired, none of this is free.** A
`t2.micro` plus `db.t3.micro` plus storage runs roughly **$25–30/month** at
on-demand rates. Confirm before applying.

---

## Architecture

```
                    Internet
                        │
                 ┌──────┴──────┐
                 │   Internet  │        (free; a NAT gateway would be
                 │   Gateway   │         ~$32/month and is not here)
                 └──────┬──────┘
                        │
   ┌────────────────────┴─────────────────────┐
   │  VPC 10.0.0.0/16                         │
   │                                          │
   │  ┌────────────────┐  ┌────────────────┐  │
   │  │ public subnet  │  │ public subnet  │  │
   │  │ AZ-a           │  │ AZ-b           │  │
   │  │                │  │                │  │
   │  │  ┌──────────┐  │  │   (exists only │  │
   │  │  │ EC2      │  │  │    to satisfy  │  │
   │  │  │ t2.micro │  │  │    the RDS     │  │
   │  │  │ docker   │  │  │    two-AZ      │  │
   │  │  └────┬─────┘  │  │    subnet-group│  │
   │  │       │        │  │    requirement)│  │
   │  └───────┼────────┘  └────────────────┘  │
   │          │ 5432, security-group-scoped   │
   │  ┌───────▼──────────────────────────┐    │
   │  │ RDS Postgres 16, db.t3.micro     │    │
   │  │ Single-AZ, publicly_accessible=  │    │
   │  │ FALSE (no public IPv4, no bill)  │    │
   │  └──────────────────────────────────┘    │
   └──────────────────────────────────────────┘

   ECR ── image pull over the IGW (same region = free egress)
   SSM Parameter Store ── the connection string, SecureString
```

**What runs on the instance, and why it is the dispatcher rather than the MCP
server.** `hapax.server` speaks MCP over **stdio**: an agent host spawns it as a
child process and talks down a pipe. It is not a network service and has no port
to expose — run detached, it would read EOF on stdin and exit. The thing worth
running continuously is the **dispatcher** (`deploy/dispatcher_main.py`), which
claims tasks whose lease has lapsed and runs them. That is the component that
makes a task survive the death of whatever created it, and it is the reason the
deployment needs RDS rather than a dictionary.

---

## Cost breakdown

Assuming a **legacy account inside its 12 months**, one instance running 24/7:

| Resource | Usage | Free-tier allowance | Cost |
|---|---|---|---|
| EC2 t2.micro | 730 h/month | 750 h/month | **$0** |
| Public IPv4 (auto-assigned) | 730 h/month | 750 h/month, EC2 only | **$0** |
| EBS gp3 root | 10 GB | 30 GB | **$0** |
| RDS db.t3.micro, Single-AZ | 730 h/month | 750 h/month | **$0** |
| RDS gp2 storage | 20 GB | 20 GB | **$0** |
| RDS backups | disabled | 20 GB | **$0** |
| ECR private storage | **~48 MB** for the amd64 image (measured) | 500 MB | **$0** |
| ECR → EC2 data transfer | same region | always free | **$0** |
| Internet Gateway | — | no hourly charge | **$0** |
| SSM Parameter Store | 1 standard parameter | standard tier free | **$0** |
| Data transfer out | negligible | 100 GB/month | **$0** |
| **Total** | | | **$0/month** |

### With Aporia and a load balancer

Free tier does not cover this shape, and it is not meant to — it is the
configuration for an account with credits:

| Resource | Cost/month |
|---|---|
| EC2 `t3.small` (2 GB, both services) | ~$15 |
| RDS `db.t3.micro` | $0 while in the free tier, else ~$12 |
| Application Load Balancer | ~$16 + LCU |
| ACM certificate | $0 |
| ECR (605 MB Aporia + 48 MB Hapax) | ~$0.02 past the 500 MB allowance |
| **Total** | **~$31–43/month** |

Against $10,000 of credits that is roughly two years. Two things to hold onto
anyway: credits **mask** real charges, so keep the $1 billing alarm exactly as
it is — with credits applied your actual charges should still read $0 — and put
the credit expiry date in a calendar, because an always-on stack starts billing
silently the day they run out.

Deliberately absent, with what each would have cost:

| Not used | Why | Would cost |
|---|---|---|
| NAT gateway | Public subnet + IGW instead | ~$32/month + data |
| Application Load Balancer | Nothing listens on a port | ~$16/month + LCU |
| Fargate | Not in any free tier | ~$9/month for 0.25 vCPU |
| Elastic IP | Auto-assigned IP instead | $0 attached, **$3.60/month orphaned** |
| Multi-AZ RDS | Single-AZ | doubles the instance |
| RDS storage autoscaling | Fixed 20 GB | silent growth past the allowance |
| Performance Insights / enhanced monitoring | Off | small but easy to forget |
| CloudWatch Logs | journald + `docker logs` instead | free tier is limited; see logging |

**The two settings that matter most for cost, and why:**

1. `publicly_accessible = false` on RDS. A publicly accessible instance gets its
   own public IPv4, and since 1 Feb 2024 **every** public IPv4 costs
   $0.005/hour. The free 750 hours apply to **EC2 only** — an RDS address is not
   covered, so `true` would add ~$3.60/month *and* put the database on the
   internet.
2. **No Elastic IP.** An EIP attached to a running instance is fine, but an EIP
   left behind after teardown bills forever for nothing. Not allocating one
   removes the failure mode. The cost: the public IP changes across a stop/start.

---

## Deploying

Prerequisites: Terraform ≥ 1.5, Docker, AWS CLI with credentials, and the
billing alarm above.

```bash
cd deploy/aws
cp terraform.tfvars.example terraform.tfvars

# Your address only — the config refuses 0.0.0.0/0 on SSH.
echo "ssh_ingress_cidr = \"$(curl -s https://checkip.amazonaws.com)/32\"" >> terraform.tfvars
echo "ssh_public_key   = \"$(cat ~/.ssh/id_ed25519.pub)\"" >> terraform.tfvars

terraform init
terraform plan      # read it; confirm nothing unexpected
terraform apply
```

Then build and push the image, and restart the service to pick it up:

```bash
cd ../..                     # repo root
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=$(cd deploy/aws && terraform output -raw region 2>/dev/null || echo us-east-1)
REPO=$(cd deploy/aws && terraform output -raw ecr_repository_url)

aws ecr get-login-password --region "$REGION" \
  | docker login --username AWS --password-stdin "${ACCOUNT}.dkr.ecr.${REGION}.amazonaws.com"

# --platform matters: EC2 t2/t3.micro are x86_64 and an Apple-silicon laptop
# builds arm64 by default. Skip this and the container will not start.
docker build --platform linux/amd64 -t "$REPO:latest" .
docker push "$REPO:latest"

ssh ec2-user@$(cd deploy/aws && terraform output -raw instance_public_ip) \
  'sudo systemctl restart hapax'
```

Verify it is alive:

```bash
cd deploy/aws
ssh ec2-user@$(terraform output -raw instance_public_ip) 'sudo systemctl status hapax --no-pager'
```

---

## How secrets reach the container

The RDS master password is generated by Terraform (`random_password`), written
to **SSM Parameter Store as a SecureString**, and read at instance boot by the
instance role — which is permitted `ssm:GetParameter` on that one parameter ARN
and nothing else.

```
random_password ──> aws_ssm_parameter (SecureString, KMS-encrypted at rest)
                          │
                          │ instance role, scoped to this one ARN
                          ▼
             user_data: aws ssm get-parameter --with-decryption
                          │
                          ▼
              /etc/hapax/env (0600, root)  ──> systemd EnvironmentFile
                          │
                          ▼
              container env: HAPAX_DATABASE_URL
```

What this avoids:

- **No AWS access keys on the instance.** It assumes a role through an instance
  profile, so there is no static credential to leak or rotate.
- **The password is not in user data.** User data is readable by anything on the
  box that can reach the metadata service, so a secret there is a secret
  published locally.
- **The password is not in the image.** Images get pushed to registries.

**The honest gap:** the generated password *is* in `terraform.tfstate` in
plaintext. That file is gitignored here. For anything beyond a portfolio
deployment it belongs in an encrypted remote backend (S3 + DynamoDB lock, or
Terraform Cloud), and the password should come from Secrets Manager with rotation
rather than being generated once and left alone.

---

## Getting logs off the instance

The dispatcher logs to stdout, Docker captures it, systemd captures Docker.
Nothing is shipped anywhere, which is why it costs nothing.

```bash
cd deploy/aws
IP=$(terraform output -raw instance_public_ip)

ssh ec2-user@$IP 'sudo journalctl -u hapax -f'                # follow
ssh ec2-user@$IP 'sudo journalctl -u hapax --since "1 hour ago"'
ssh ec2-user@$IP 'sudo docker logs --tail 200 hapax'          # container's own view
ssh ec2-user@$IP 'sudo cat /var/log/hapax-bootstrap.log'      # first-boot problems
ssh ec2-user@$IP 'sudo journalctl -u hapax --since today' > hapax.log   # pull a copy down
```

Log rotation is configured in the systemd unit (`max-size=10m`, `max-file=3`), so
a chatty container cannot fill a 10 GB root volume.

**If you want CloudWatch Logs instead:** install the agent and give the instance
role `logs:PutLogEvents`. It is genuinely convenient and it is *not* obviously
free — CloudWatch has its own free allowance for ingestion and storage, separate
from the account free tier, and a log group with no retention policy keeps
paying after teardown. If you add it, set `retention_in_days` and add the log
group to the teardown checklist. This deployment leaves it out for that reason.

---

## Adding Aporia to the same stack

Off by default (`enable_aporia = false`). Turning it on puts Aporia's container
on the same instance as the Hapax dispatcher.

**Sizing is measured, not guessed.** Aporia idles at 716 MB and peaks at 769 MB
(`docker stats`, linux/amd64, 2026-08-17). With the dispatcher (~50 MB) and
Amazon Linux (~200 MB) the instance needs about **1.1 GB**, so:

| instance | RAM | verdict |
|---|---|---|
| `t2.micro` / `t3.micro` | 1 GB | **rejected at plan time** — ~100 MB headroom for a 769 MB process is an OOM waiting to happen |
| `t3.small` | 2 GB | comfortable; the recommended setting |
| `t3.medium` | 4 GB | unnecessary |

A `lifecycle.precondition` on the instance refuses `enable_aporia` on any
`*.micro`, so this fails in `terraform plan` rather than at 3am in a health
check. (Aporia's own DEPLOY.md used to claim 1.5 GB, which would have pushed
this to a `t3.medium` for nothing.)

```hcl
# terraform.tfvars
enable_aporia        = true
instance_type        = "t3.small"
enable_load_balancer = true              # ~$16/month, gives a real https:// URL
domain_name          = "aporia.yourdomain.com"
```

```bash
terraform apply
# ACM validation is DNS-based and this stack does not own your zone, so:
terraform output acm_validation_records   # create these, then apply completes

# Build for the instance's architecture, not your laptop's:
REPO=$(terraform output -raw aporia_ecr_repository_url)
cd /path/to/aporia && docker build --platform linux/amd64 -t "$REPO:latest" . && docker push "$REPO:latest"

# The Anthropic key is created empty so it never enters tfstate:
terraform output -raw set_anthropic_key_command   # then run it
```

Without a key Aporia still serves — it returns passages unclassified rather
than failing, which is the same graceful degradation the local app has.

### Before it is public

`/search` costs money per novel claim: one batched Claude call over `k`
passages. `api/limits.py` bounds that — `k` is clamped to 50, queries to 300
characters, and each client gets 20 searches a minute. The client key comes from
`X-Forwarded-For`, which is **only trustworthy behind the load balancer**; if
you expose port 8080 directly the header is spoofable and the limit is
decorative. That is the real reason to prefer `enable_load_balancer = true` for
anything public, ahead of the nicer URL.

## Teardown

```bash
cd deploy/aws
terraform destroy
```

This stack is written so that destroy leaves nothing billable:

| Usual leftover | Why it does not happen here |
|---|---|
| Unattached EBS volume | `delete_on_termination = true` on the root device |
| Elastic IP | none is ever allocated |
| RDS final snapshot | `skip_final_snapshot = true` — a final snapshot is a *manual* snapshot and survives destroy |
| RDS automated backups | `backup_retention_period = 0`, `delete_automated_backups = true` |
| ECR images | `force_delete = true` on the repository |
| SSM parameter | managed by Terraform, destroyed with the stack |
| CloudWatch log group | none is created |

### Verify in the console afterwards

Terraform reporting success is not the same as the account being clean. Check
each of these in the deployed region:

1. **EC2 → Volumes**, filter **State = Available.** Any volume here is unattached
   and billing. Should be empty.
2. **EC2 → Elastic IPs.** Should be empty. An unattached EIP is $3.60/month.
3. **EC2 → Instances**, including **Stopped** — a stopped instance still bills
   for its EBS.
4. **RDS → Snapshots**, both the **Manual** and **System** tabs. Manual snapshots
   outlive the database.
5. **RDS → Databases**, check for anything in *deleting* that got stuck.
6. **ECR → Repositories.** `hapax` should be gone.
7. **Systems Manager → Parameter Store.** `/hapax/database_url` should be gone.
8. **VPC → Your VPCs.** The `hapax-vpc` should be gone. A leftover VPC costs
   nothing but signals a partial destroy.
9. **Billing → Free tier**, next day. Month-to-date should stop climbing.

Everything is tagged `Project = hapax` and `Teardown = destroy-all-with-this-tag`,
so the **Resource Groups & Tag Editor** ("find resources by tag", all regions)
will list anything left in one query.

Or run the script, which asks AWS about all nine at once:

```bash
./deploy/aws/verify-teardown.sh us-east-1
```

It exits non-zero if anything is left, and — importantly — **also exits non-zero
if it could not check.** The first version of it swallowed AWS errors and printed
"clean" for every line when there were simply no credentials configured, which is
the most dangerous possible output for a script whose entire job is to tell you
nothing is still costing money. It now verifies `sts get-caller-identity` before
checking anything, and treats an empty result as an error rather than a zero.

---

## What is verified, and what is not

**Verified in this session, locally:**

- `terraform validate` passes; `terraform fmt -check` is clean.
- `terraform plan` resolves the entire resource graph and stops only at the AWS
  credential step.
- The `ssh_ingress_cidr = "0.0.0.0/0"` guard rejects as intended.
- The container builds for **linux/amd64** — the architecture EC2 t2/t3.micro
  run — at **~48 MB**, comfortably inside ECR's 500 MB allowance. Measured two
  ways that agree: `docker image inspect` reports 49.8 MB, and
  `docker save | gzip` (the closest local proxy for what ECR stores) reports
  47 MB.
- Both the arm64 and amd64 images run against a real Postgres, claim a task,
  complete it, and write **exactly one** ledger row of $50.
- SIGTERM is handled gracefully: the dispatcher finishes its sweep and reports
  `stopped after N sweeps: ... claimed, ... completed, ... failed, ... abandoned`.

  A note on measurement, since it nearly went into this document wrong: for the
  arm64 build `docker images` reported 237 MB while `docker image inspect`
  reported 49 MB, because the two use different size accounting depending on
  which builder produced the image. The amd64 figures agree with each other, so
  those are the ones quoted.

**Not verified, because nothing has been provisioned:**

- No `terraform apply` has run. Instance sizing, the user-data bootstrap on a
  real Amazon Linux 2023 AMI, and ECR pull-from-instance are unexercised.
- The costs above are derived from published AWS free-tier terms (checked
  2026-08-16), not from an observed bill.
- `deploy/dispatcher_main.py` is exercised locally but not under systemd.
- `verify-teardown.sh` has been run only in its credential-refusal path (it
  correctly exits 2 with no credentials). Its per-resource queries are unexercised
  against a live account.
