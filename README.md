# ☁️ Cloud-Native Platform Bootstrap

Interactive Python script that generates and deploys a full cloud-native stack on AWS with a single command.

## What it deploys

- **VPC** with public and private subnets (Terraform)
- **EKS** — managed Kubernetes cluster
- **ECR** — private Docker image registry
- **ArgoCD** — GitOps, automatic sync from the repo
- **Prometheus + Grafana** — observability (optional)
- **FastAPI** — sample application with metrics (optional)
- **GitHub Actions** — CI pipeline that builds, pushes and updates the Helm chart

## Requirements

| Tool | Minimum version |
|------|----------------|
| Python | 3.10+ |
| Terraform | 1.5+ |
| kubectl | 1.28+ |
| Helm | 3.12+ |
| Docker | 24+ |
| AWS CLI | 2.x |
| Git | 2.x |

AWS credentials configured (`aws configure` or environment variables).  
`GITHUB_TOKEN` environment variable required if you want the repo created automatically.

## Usage

```bash
python platform_bootstrap.py
```

The script presents a menu with two options:

```
1) Deploy stack
2) Destroy stack (teardown)
```

### Deploy

1. Answer the configuration questions (project name, region, node type, etc.)
2. Review the summary and confirm
3. The script generates all files under `./<project>/`
4. Optionally runs the full deployment:
   - Terraform provisions the infrastructure (shows plan before applying)
   - kubectl is configured
   - ArgoCD is installed
   - Prometheus + Grafana installed (if enabled)
   - Sample app built and deployed (if enabled)
   - GitHub repo created and pushed (if enabled)

### Teardown

Deletes all AWS resources for the project: node groups, EKS cluster, ECR, NAT Gateway, VPC, etc. Verifies at the end that nothing billable remains.

## Generated structure

```
<project>/
├── terraform/
│   └── main.tf              # VPC + EKS + ECR
├── app/
│   ├── main.py              # FastAPI with /health, /items, /metrics
│   ├── requirements.txt
│   └── Dockerfile
├── helm/
│   └── <project>/
│       ├── Chart.yaml
│       ├── values.yaml
│       └── templates/
│           ├── deployment.yaml
│           └── service.yaml
├── argocd/
│   └── application.yaml
├── .github/
│   └── workflows/
│       └── ci.yaml          # Build → push ECR → update values.yaml
└── .gitignore
```

## Accessing services after deployment

```bash
# ArgoCD
kubectl port-forward svc/argocd-server -n argocd 8080:443
# → https://localhost:8080  (user: admin)

# Grafana
kubectl port-forward svc/kube-prometheus-stack-grafana -n monitoring 3000:80
# → http://localhost:3000  (user: admin)

# FastAPI app
kubectl port-forward svc/<project> -n <project> 8000:80
# → http://localhost:8000/docs
```

Passwords are displayed at the end of the deployment.

## Design Decisions & FAQ

**Why `single_nat_gateway = true`?**  
Cost vs availability trade-off. A NAT Gateway costs ~$32/month fixed plus data transfer. Using one per AZ doubles that cost with no benefit for a dev/demo environment. In a production setup this would be set to `false` so that if one AZ goes down, the other AZ's private subnets don't depend on the same NAT Gateway.

**Why not use a remote Terraform backend (S3 + DynamoDB)?**  
This project is designed to be run by a single operator from a local machine. Adding a remote backend would require pre-provisioning an S3 bucket and DynamoDB table before running the script, which adds friction for a bootstrap tool. In a team or production context the `terraform` block would be extended with an S3 backend and DynamoDB state locking to prevent concurrent applies.

**Why ArgoCD over Flux?**  
Both are valid GitOps tools. ArgoCD was chosen for its visual UI, which makes it easier to inspect sync status, diff live state vs desired state, and onboard team members. Flux is more lightweight and Kubernetes-native but lacks a built-in dashboard.

**How would you handle multiple environments (dev/staging/prod)?**  
The current script provisions a single environment. To scale to multiple environments, each would get its own `terraform/environments/<env>/terraform.tfvars` with environment-specific values (instance types, node counts, regions). On the GitOps side, ArgoCD ApplicationSets would manage deployments across environments by targeting different branches or directory paths in the repo.
