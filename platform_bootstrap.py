#!/usr/bin/env python3
"""
Cloud-Native Platform Bootstrap Script
Interactive: generates and deploys the full stack on AWS
"""

import os
import sys
import subprocess
import json
import shutil
import base64
import signal
from pathlib import Path

# ─── Colors ───────────────────────────────────────────────────────────────────
GREEN = "\033[92m"
YELLOW = "\033[93m"
RED = "\033[91m"
BLUE = "\033[94m"
BOLD = "\033[1m"
RESET = "\033[0m"

def p(msg, color=RESET): print(f"{color}{msg}{RESET}")
def ask(question, default=None):
    hint = f" [{default}]" if default else ""
    val = input(f"{BLUE}{BOLD}{question}{hint}: {RESET}").strip()
    return val if val else default

def ask_bool(question, default=True):
    hint = "Y/n" if default else "y/N"
    val = input(f"{BLUE}{BOLD}{question} [{hint}]: {RESET}").strip().lower()
    if not val:
        return default
    return val in ("y", "yes")

def _sigint_handler(sig, frame):
    p("\n\n⚠ Interrupted. If Terraform was running, check for remaining resources on AWS.", YELLOW)
    p("  https://console.aws.amazon.com/billing/home#/", YELLOW)
    sys.exit(1)

signal.signal(signal.SIGINT, _sigint_handler)

def run(cmd, check=True, capture=False):
    p(f"  → {cmd}", YELLOW)
    import shlex
    args = shlex.split(cmd)
    if capture:
        result = subprocess.run(args, capture_output=True, text=True)
        return result.stdout.strip()
    else:
        result = subprocess.run(args)
        if check and result.returncode != 0:
            p(f"Error running: {cmd}", RED)
            sys.exit(1)
        return result.returncode

def decode_b64(value: str) -> str:
    """Decodes base64 without relying on external binaries (works on Windows)."""
    return base64.b64decode(value).decode("utf-8").strip()

def check_deps(deps):
    p("\n🔍 Checking dependencies...", BOLD)
    missing = []
    for dep in deps:
        if shutil.which(dep):
            p(f"  ✓ {dep}", GREEN)
        else:
            p(f"  ✗ {dep} — NOT found", RED)
            missing.append(dep)
    return missing

# ─── Gather config ────────────────────────────────────────────────────────────
def gather_config():
    p(f"\n{BOLD}{'─'*50}", BLUE)
    p(f"  🚀 Cloud-Native Platform Bootstrap", BLUE)
    p(f"{'─'*50}{RESET}", BLUE)
    p("Answer the following questions to configure your stack.\n")

    cfg = {}

    cfg["project"]    = ask("Project name (letters, numbers, hyphens)", "myplatform")
    cfg["region"]     = ask("AWS region", "us-east-1")
    cfg["gh_user"]    = ask("Your GitHub user/org")
    cfg["repo_name"]  = ask("GitHub repository name", cfg["project"])

    p("\n─── Cluster ──────────────────────────────────────────────────────────────", BOLD)
    cfg["node_type"]  = ask("EKS node instance type", "t3.medium")
    cfg["node_count"] = ask("Number of nodes", "2")

    p("\n─── Optional ─────────────────────────────────────────────────────────────", BOLD)
    cfg["create_repo"]   = ask_bool("Create GitHub repo? (requires GITHUB_TOKEN in env)")
    cfg["observability"] = ask_bool("Install Prometheus + Grafana?")
    cfg["sample_app"]    = ask_bool("Deploy sample FastAPI app?")

    p("\n─── Summary ──────────────────────────────────────────────────────────────", BOLD)
    for k, v in cfg.items():
        p(f"  {k}: {YELLOW}{v}{RESET}")

    if not ask_bool("\nContinue with this configuration?"):
        p("Aborted.", RED)
        sys.exit(0)

    return cfg

# ─── Generate files ───────────────────────────────────────────────────────────
def generate_terraform(cfg, base: Path):
    tf_dir = base / "terraform"
    tf_dir.mkdir(parents=True, exist_ok=True)

    (tf_dir / "main.tf").write_text(f"""
terraform {{
  required_providers {{
    aws = {{ source = "hashicorp/aws", version = "~> 5.0" }}
  }}
}}

provider "aws" {{
  region = "{cfg['region']}"
}}

data "aws_caller_identity" "current" {{}}

module "vpc" {{
  source  = "terraform-aws-modules/vpc/aws"
  version = "~> 5.0"

  name = "{cfg['project']}-vpc"
  cidr = "10.0.0.0/16"

  azs             = ["{cfg['region']}a", "{cfg['region']}b"]
  private_subnets = ["10.0.1.0/24", "10.0.2.0/24"]
  public_subnets  = ["10.0.101.0/24", "10.0.102.0/24"]

  enable_nat_gateway = true
  single_nat_gateway = true

  tags = {{ Project = "{cfg['project']}" }}
}}

module "eks" {{
  source  = "terraform-aws-modules/eks/aws"
  version = "~> 20.0"

  cluster_name    = "{cfg['project']}-cluster"
  cluster_version = "1.32"

  vpc_id     = module.vpc.vpc_id
  subnet_ids = module.vpc.private_subnets

  cluster_endpoint_public_access = true

  # Automatically grant access to the user/role running Terraform
  enable_cluster_creator_admin_permissions = true

  eks_managed_node_groups = {{
    default = {{
      instance_types = ["{cfg['node_type']}"]
      min_size       = 1
      max_size       = {int(cfg['node_count']) + 1}
      desired_size   = {int(cfg['node_count'])}

      # Permissions to pull from ECR
      iam_role_additional_policies = {{
        ecr = "arn:aws:iam::aws:policy/AmazonEC2ContainerRegistryReadOnly"
      }}
    }}
  }}

  tags = {{ Project = "{cfg['project']}" }}
}}

resource "aws_ecr_repository" "app" {{
  name                 = "{cfg['project']}"
  image_tag_mutability = "MUTABLE"
  tags = {{ Project = "{cfg['project']}" }}
}}

output "cluster_name"     {{ value = module.eks.cluster_name }}
output "ecr_url"          {{ value = aws_ecr_repository.app.repository_url }}
output "cluster_endpoint" {{ value = module.eks.cluster_endpoint }}
output "caller_arn"       {{ value = data.aws_caller_identity.current.arn }}
""")
    p("  ✓ terraform/main.tf", GREEN)

def generate_app(cfg, base: Path):
    app_dir = base / "app"
    app_dir.mkdir(parents=True, exist_ok=True)

    (app_dir / "main.py").write_text("""from fastapi import FastAPI
from prometheus_client import Counter, generate_latest
from starlette.responses import Response

app = FastAPI(title="Platform Demo")

requests_total = Counter("http_requests_total", "Total HTTP requests", ["method", "path"])

@app.middleware("http")
async def count_requests(request, call_next):
    response = await call_next(request)
    requests_total.labels(method=request.method, path=request.url.path).inc()
    return response

@app.get("/health")
def health():
    return {"status": "ok"}

@app.get("/items")
def list_items():
    return {"items": ["item1", "item2", "item3"]}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type="text/plain")
""")

    (app_dir / "requirements.txt").write_text(
        "fastapi\nuvicorn\nprometheus-client\n"
    )

    (app_dir / "Dockerfile").write_text("""FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
""")
    p("  ✓ app/main.py + Dockerfile", GREEN)

def generate_helm(cfg, base: Path):
    chart_dir = base / "helm" / cfg["project"]
    (chart_dir / "templates").mkdir(parents=True, exist_ok=True)

    (chart_dir / "Chart.yaml").write_text(f"""apiVersion: v2
name: {cfg['project']}
description: Platform demo chart
version: 0.1.0
appVersion: "latest"
""")

    # values.yaml is written with the real ECR URL in step_app, placeholder here
    (chart_dir / "values.yaml").write_text(f"""image:
  repository: PLACEHOLDER_ECR_URL
  tag: latest
  pullPolicy: Always

service:
  type: ClusterIP
  port: 80
  targetPort: 8000

replicaCount: 1

resources:
  requests:
    cpu: 100m
    memory: 128Mi
  limits:
    cpu: 250m
    memory: 256Mi
""")

    (chart_dir / "templates" / "deployment.yaml").write_text("""apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Chart.Name }}
spec:
  replicas: {{ .Values.replicaCount }}
  selector:
    matchLabels:
      app: {{ .Chart.Name }}
  template:
    metadata:
      labels:
        app: {{ .Chart.Name }}
      annotations:
        prometheus.io/scrape: "true"
        prometheus.io/port: "8000"
        prometheus.io/path: "/metrics"
    spec:
      containers:
        - name: {{ .Chart.Name }}
          image: "{{ .Values.image.repository }}:{{ .Values.image.tag }}"
          imagePullPolicy: {{ .Values.image.pullPolicy }}
          ports:
            - containerPort: 8000
          readinessProbe:
            httpGet:
              path: /health
              port: 8000
            initialDelaySeconds: 5
            periodSeconds: 10
          resources:
            {{- toYaml .Values.resources | nindent 12 }}
""")

    (chart_dir / "templates" / "service.yaml").write_text("""apiVersion: v1
kind: Service
metadata:
  name: {{ .Chart.Name }}
spec:
  selector:
    app: {{ .Chart.Name }}
  ports:
    - port: {{ .Values.service.port }}
      targetPort: {{ .Values.service.targetPort }}
  type: {{ .Values.service.type }}
""")
    p("  ✓ helm chart", GREEN)

def generate_argocd(cfg, base: Path):
    argo_dir = base / "argocd"
    argo_dir.mkdir(parents=True, exist_ok=True)

    (argo_dir / "application.yaml").write_text(f"""apiVersion: argoproj.io/v1alpha1
kind: Application
metadata:
  name: {cfg['project']}
  namespace: argocd
spec:
  project: default
  source:
    repoURL: https://github.com/{cfg['gh_user']}/{cfg['repo_name']}
    targetRevision: HEAD
    path: helm/{cfg['project']}
  destination:
    server: https://kubernetes.default.svc
    namespace: {cfg['project']}
  syncPolicy:
    automated:
      prune: true
      selfHeal: true
    syncOptions:
      - CreateNamespace=true
""")
    p("  ✓ argocd/application.yaml", GREEN)

def generate_github_actions(cfg, base: Path):
    gha_dir = base / ".github" / "workflows"
    gha_dir.mkdir(parents=True, exist_ok=True)

    (gha_dir / "ci.yaml").write_text(f"""name: CI

on:
  push:
    branches: [main]
    paths:
      - 'app/**'

env:
  AWS_REGION: {cfg['region']}
  ECR_REPOSITORY: {cfg['project']}

jobs:
  build-and-push:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - uses: actions/checkout@v4

      - name: Configure AWS credentials
        uses: aws-actions/configure-aws-credentials@v4
        with:
          aws-access-key-id: ${{{{ secrets.AWS_ACCESS_KEY_ID }}}}
          aws-secret-access-key: ${{{{ secrets.AWS_SECRET_ACCESS_KEY }}}}
          aws-region: ${{{{ env.AWS_REGION }}}}

      - name: Login to ECR
        id: login-ecr
        uses: aws-actions/amazon-ecr-login@v2

      - name: Build, tag, push
        env:
          ECR_REGISTRY: ${{{{ steps.login-ecr.outputs.registry }}}}
          IMAGE_TAG: ${{{{ github.sha }}}}
        run: |
          docker build -t $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG app/
          docker push $ECR_REGISTRY/$ECR_REPOSITORY:$IMAGE_TAG

      - name: Update Helm values
        env:
          ECR_REGISTRY: ${{{{ steps.login-ecr.outputs.registry }}}}
          IMAGE_TAG: ${{{{ github.sha }}}}
        run: |
          sed -i "s|tag:.*|tag: $IMAGE_TAG|" helm/{cfg['project']}/values.yaml
          sed -i "s|repository:.*|repository: $ECR_REGISTRY/$ECR_REPOSITORY|" helm/{cfg['project']}/values.yaml
          git config user.name github-actions
          git config user.email github-actions@github.com
          git add helm/{cfg['project']}/values.yaml
          git commit -m "ci: update image tag to $IMAGE_TAG" || echo "No changes"
          git push
""")
    p("  ✓ .github/workflows/ci.yaml", GREEN)

def generate_gitignore(base: Path):
    (base / ".gitignore").write_text(""".terraform/
*.tfstate
*.tfstate.backup
.terraform.lock.hcl
__pycache__/
*.pyc
.env
""")

# ─── Deploy steps ─────────────────────────────────────────────────────────────
def step_terraform(cfg, base: Path):
    p("\n📦 [1/5] Terraform — Provisioning AWS infrastructure...", BOLD)
    tf = base / "terraform"
    run(f"terraform -chdir={tf} init")
    run(f"terraform -chdir={tf} plan")
    if not ask_bool("\nApply this plan?"):
        p("Aborted.", RED)
        sys.exit(0)
    run(f"terraform -chdir={tf} apply -auto-approve")

    ecr_url      = run(f"terraform -chdir={tf} output -raw ecr_url", capture=True)
    cluster_name = run(f"terraform -chdir={tf} output -raw cluster_name", capture=True)
    caller_arn   = run(f"terraform -chdir={tf} output -raw caller_arn", capture=True)
    p(f"  ECR:     {ecr_url}", GREEN)
    p(f"  Cluster: {cluster_name}", GREEN)
    return ecr_url, cluster_name, caller_arn

def step_kubeconfig(cfg, cluster_name):
    p("\n🔗 [2/5] Configuring kubectl...", BOLD)
    run(f"aws eks update-kubeconfig --region {cfg['region']} --name {cluster_name} --force")
    p("  Waiting for nodes to be Ready...", YELLOW)
    run("kubectl wait --for=condition=Ready nodes --all --timeout=300s")

def step_iam_access(cfg, cluster_name, caller_arn):
    """Ensures the current user/role can access the cluster."""
    p("\n🔑 Configuring IAM access to cluster...", BOLD)
    # Try to create access entry (may already exist if Terraform created it)
    run(
        f"aws eks create-access-entry --cluster-name {cluster_name} "
        f"--principal-arn {caller_arn} --type STANDARD",
        check=False
    )
    run(
        f"aws eks associate-access-policy --cluster-name {cluster_name} "
        f"--principal-arn {caller_arn} "
        f"--policy-arn arn:aws:eks::aws:cluster-access-policy/AmazonEKSClusterAdminPolicy "
        f"--access-scope type=cluster",
        check=False
    )
    p("  ✓ IAM access configured", GREEN)

def step_argocd(base: Path):
    p("\n🔄 [3/5] Installing ArgoCD...", BOLD)
    run("kubectl create namespace argocd --dry-run=client -o yaml | kubectl apply -f -", shell=True)
    run("kubectl apply -n argocd -f https://raw.githubusercontent.com/argoproj/argo-cd/stable/manifests/install.yaml")
    run("kubectl wait --for=condition=available --timeout=300s deployment/argocd-server -n argocd")
    p("  ✓ ArgoCD installed", GREEN)

    # Decode password in Python (no external base64 binary — works on Windows)
    b64 = run(
        "kubectl -n argocd get secret argocd-initial-admin-secret "
        "-o jsonpath=\"{.data.password}\"",
        capture=True
    )
    password = decode_b64(b64)
    p(f"  ArgoCD admin password: {YELLOW}{password}{RESET}")
    return password

def step_observability():
    p("\n📊 [4/5] Installing Prometheus + Grafana...", BOLD)
    run("helm repo add prometheus-community https://prometheus-community.github.io/helm-charts")
    run("helm repo update")
    run("kubectl create namespace monitoring --dry-run=client -o yaml | kubectl apply -f -", shell=True)
    run(
        "helm upgrade --install kube-prometheus-stack "
        "prometheus-community/kube-prometheus-stack "
        "--namespace monitoring --wait"
    )
    p("  ✓ kube-prometheus-stack installed", GREEN)

    b64 = run(
        "kubectl get secret --namespace monitoring kube-prometheus-stack-grafana "
        "-o jsonpath=\"{.data.admin-password}\"",
        capture=True
    )
    gf_pass = decode_b64(b64)
    p(f"  Grafana admin password: {YELLOW}{gf_pass}{RESET}")
    return gf_pass

def step_app(cfg, ecr_url, base: Path):
    p("\n🚀 [5/5] Build + push image + ArgoCD deploy...", BOLD)
    registry = ecr_url.split("/")[0]

    # ECR login
    ecr_token = run(
        f"aws ecr get-login-password --region {cfg['region']}",
        capture=True
    )
    proc = subprocess.run(
        ["docker", "login", "--username", "AWS", "--password-stdin", registry],
        input=ecr_token, text=True, check=True
    )

    # Build and push with latest tag
    run(f"docker build -t {ecr_url}:latest {base}/app/")
    run(f"docker push {ecr_url}:latest")

    # Patch values.yaml with the real ECR URL
    values = base / "helm" / cfg["project"] / "values.yaml"
    content = values.read_text()
    content = content.replace("PLACEHOLDER_ECR_URL", ecr_url)
    values.write_text(content)

    # Apply ArgoCD Application
    run(f"kubectl apply -f {base}/argocd/application.yaml")
    p("  ✓ ArgoCD Application applied — syncing...", GREEN)

def create_github_repo(cfg, base: Path):
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        p("  ⚠ GITHUB_TOKEN not found in env. Skipping repo creation.", YELLOW)
        p("  Create the repo manually at https://github.com/new and git push.", YELLOW)
        return False

    p(f"\n🐙 Creating GitHub repo: {cfg['gh_user']}/{cfg['repo_name']}...", BOLD)
    import urllib.request, urllib.error
    is_private = ask_bool("Private GitHub repo?", default=True)
    payload = json.dumps({"name": cfg["repo_name"], "private": is_private, "auto_init": False}).encode()
    req = urllib.request.Request(
        "https://api.github.com/user/repos",
        data=payload,
        headers={"Authorization": f"token {token}", "Content-Type": "application/json"}
    )
    try:
        urllib.request.urlopen(req)
        p("  ✓ Repo created", GREEN)
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        if "already exists" in body:
            p("  ⚠ Repo already exists, continuing...", YELLOW)
        else:
            p(f"  ⚠ {body}", YELLOW)
            return False

    run(f"git -C {base} init")
    run(f"git -C {base} add .")
    run(f"git -C {base} commit -m \"chore: initial bootstrap\"")
    run(f"git -C {base} branch -M main")
    # Add remote only if it doesn't exist
    remotes = run(f"git -C {base} remote", capture=True)
    if "origin" not in remotes:
        run(f"git -C {base} remote add origin https://github.com/{cfg['gh_user']}/{cfg['repo_name']}.git")
    run(f"git -C {base} push -u origin main")
    return True

def print_summary(cfg, ecr_url, cluster_name, argocd_pass, grafana_pass=None):
    p(f"\n{'─'*55}", GREEN)
    p(f"  ✅  Stack deployed successfully", GREEN)
    p(f"{'─'*55}", GREEN)
    p(f"  Cluster:   {cluster_name}")
    p(f"  ECR:       {ecr_url}")
    p(f"\n  ArgoCD:")
    p(f"    kubectl port-forward svc/argocd-server -n argocd 8080:443")
    p(f"    URL:      https://localhost:8080")
    p(f"    User:     admin")
    p(f"    Password: {YELLOW}{argocd_pass}{RESET}")
    if grafana_pass:
        p(f"\n  Grafana:")
        p(f"    kubectl port-forward svc/kube-prometheus-stack-grafana -n monitoring 3000:80")
        p(f"    URL:      http://localhost:3000")
        p(f"    User:     admin")
        p(f"    Password: {YELLOW}{grafana_pass}{RESET}")
    p(f"\n  FastAPI App:")
    p(f"    kubectl port-forward svc/{cfg['project']} -n {cfg['project']} 8000:80")
    p(f"    URL:      http://localhost:8000/docs")
    p(f"\n  Repo:      https://github.com/{cfg['gh_user']}/{cfg['repo_name']}")
    p(f"{'─'*55}\n", GREEN)

# ─── Teardown ─────────────────────────────────────────────────────────────────
def teardown(cfg, base: Path):
    p("\n🧨 Starting full AWS cleanup...", BOLD)

    region = cfg["region"]
    project = cfg["project"]
    cluster = f"{project}-cluster"

    # 1. Empty ECR before Terraform tries to delete it
    p("\n  [1/4] Emptying ECR...", BOLD)
    run(
        f"aws ecr delete-repository --repository-name {project} "
        f"--region {region} --force",
        check=False
    )

    # 2. Terraform destroy
    tf = base / "terraform"
    if (tf / "terraform.tfstate").exists():
        p("\n  [2/4] Terraform destroy...", BOLD)
        run(f"terraform -chdir={tf} destroy -auto-approve")
    else:
        p("\n  [2/4] tfstate not found, cleaning resources manually...", YELLOW)

        # Nodegroups
        nodegroups = run(
            f"aws eks list-nodegroups --cluster-name {cluster} --region {region} "
            f"--query nodegroups --output json",
            capture=True, check=False
        )
        try:
            ngs = json.loads(nodegroups or "[]")
            for ng in ngs:
                p(f"  Deleting nodegroup {ng}...", YELLOW)
                run(f"aws eks delete-nodegroup --cluster-name {cluster} --nodegroup-name {ng} --region {region}", check=False)
            if ngs:
                p("  Waiting for nodegroups to be deleted (~3 min)...", YELLOW)
                for ng in ngs:
                    run(f"aws eks wait nodegroup-deleted --cluster-name {cluster} --nodegroup-name {ng} --region {region}", check=False)
        except Exception as e:
            p(f"  ⚠ Error deleting nodegroups: {e}", YELLOW)

        # Cluster
        run(f"aws eks delete-cluster --name {cluster} --region {region}", check=False)

        # VPC
        vpc_id = run(
            f"aws ec2 describe-vpcs --filters Name=tag:Project,Values={project} "
            f"--region {region} --query Vpcs[0].VpcId --output text",
            capture=True, check=False
        )
        if vpc_id and vpc_id != "None":
            p(f"  Deleting VPC {vpc_id} and its dependencies...", YELLOW)
            # Subnets
            subnets = run(f"aws ec2 describe-subnets --filters Name=vpc-id,Values={vpc_id} --region {region} --query Subnets[].SubnetId --output json", capture=True, check=False)
            for s in json.loads(subnets or "[]"):
                run(f"aws ec2 delete-subnet --subnet-id {s} --region {region}", check=False)
            # Internet Gateways
            igws = run(f"aws ec2 describe-internet-gateways --filters Name=attachment.vpc-id,Values={vpc_id} --region {region} --query InternetGateways[].InternetGatewayId --output json", capture=True, check=False)
            for igw in json.loads(igws or "[]"):
                run(f"aws ec2 detach-internet-gateway --internet-gateway-id {igw} --vpc-id {vpc_id} --region {region}", check=False)
                run(f"aws ec2 delete-internet-gateway --internet-gateway-id {igw} --region {region}", check=False)
            # NAT Gateways
            nats = run(f"aws ec2 describe-nat-gateways --filter Name=vpc-id,Values={vpc_id} Name=state,Values=available --region {region} --query NatGateways[].NatGatewayId --output json", capture=True, check=False)
            for nat in json.loads(nats or "[]"):
                run(f"aws ec2 delete-nat-gateway --nat-gateway-id {nat} --region {region}", check=False)
            # Route tables
            rts = run(f"aws ec2 describe-route-tables --filters Name=vpc-id,Values={vpc_id} --region {region} --query RouteTables[?Associations[0].Main!=`true`].RouteTableId --output json", capture=True, check=False)
            for rt in json.loads(rts or "[]"):
                run(f"aws ec2 delete-route-table --route-table-id {rt} --region {region}", check=False)
            run(f"aws ec2 delete-vpc --vpc-id {vpc_id} --region {region}", check=False)

    # 3. Verify nothing billable remains
    p("\n  [3/4] Verifying resources...", BOLD)
    checks = {
        "EKS clusters":   f"aws eks list-clusters --region {region} --query clusters --output json",
        "EC2 instances":  f"aws ec2 describe-instances --region {region} --filters Name=instance-state-name,Values=running --query Reservations[].Instances[].InstanceId --output json",
        "NAT Gateways":   f"aws ec2 describe-nat-gateways --region {region} --filter Name=state,Values=available --query NatGateways[].NatGatewayId --output json",
        "Load Balancers": f"aws elbv2 describe-load-balancers --region {region} --query LoadBalancers[].LoadBalancerArn --output json",
        "ECR repos":      f"aws ecr describe-repositories --region {region} --query repositories[].repositoryName --output json",
        "EBS volumes":    f"aws ec2 describe-volumes --region {region} --filters Name=status,Values=available --query Volumes[].VolumeId --output json",
    }
    all_clean = True
    for name, cmd in checks.items():
        result = run(cmd, capture=True, check=False)
        try:
            items = json.loads(result or "[]")
        except Exception:
            items = []
        if items:
            p(f"  ⚠ {name}: {items}", YELLOW)
            all_clean = False
        else:
            p(f"  ✓ {name}: clean", GREEN)

    # 4. Result
    p("\n  [4/4] Result:", BOLD)
    if all_clean:
        p("  ✅ AWS is fully clean. No charges will be incurred.", GREEN)
    else:
        p("  ⚠ Some resources are still active. Review them in the AWS console.", YELLOW)
        p(f"  https://us-east-1.console.aws.amazon.com/billing/home#/", YELLOW)

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    p(f"\n{BOLD}{'─'*50}", BLUE)
    p(f"  🚀 Cloud-Native Platform Bootstrap", BLUE)
    p(f"{'─'*50}{RESET}", BLUE)
    p("  1) Deploy stack")
    p("  2) Destroy stack (teardown)")
    mode = input(f"{BLUE}{BOLD}Choose [1]: {RESET}").strip() or "1"

    if mode == "2":
        project  = ask("Project name to destroy", "myplatform")
        region   = ask("AWS region", "us-east-1")
        gh_user  = ask("GitHub user (for summary)", "")
        repo     = ask("GitHub repo name", project)
        cfg = {"project": project, "region": region, "gh_user": gh_user, "repo_name": repo}
        base = Path(project)
        teardown(cfg, base)
        return

    cfg = gather_config()

    # Check dependencies
    required = ["terraform", "kubectl", "helm", "docker", "aws", "git"]
    missing = check_deps(required)
    if missing:
        p(f"\n✗ Please install first: {', '.join(missing)}", RED)
        sys.exit(1)

    # Working directory
    base = Path(cfg["project"])
    base.mkdir(exist_ok=True)
    p(f"\n📁 Generating structure in ./{cfg['project']}/...", BOLD)

    # Generate files
    generate_terraform(cfg, base)
    generate_app(cfg, base)
    generate_helm(cfg, base)
    generate_argocd(cfg, base)
    generate_github_actions(cfg, base)
    generate_gitignore(base)

    if not ask_bool("\nRun the deployment now? (if no, only files are generated)"):
        p(f"\nFiles generated in ./{cfg['project']}/ — deploy whenever you're ready.", GREEN)
        return

    # Deploy
    ecr_url, cluster_name, caller_arn = step_terraform(cfg, base)
    step_kubeconfig(cfg, cluster_name)
    step_iam_access(cfg, cluster_name, caller_arn)

    argocd_pass = step_argocd(base)

    grafana_pass = None
    if cfg["observability"]:
        grafana_pass = step_observability()

    if cfg["sample_app"]:
        step_app(cfg, ecr_url, base)

    if cfg["create_repo"]:
        create_github_repo(cfg, base)

    print_summary(cfg, ecr_url, cluster_name, argocd_pass, grafana_pass)

if __name__ == "__main__":
    main()
