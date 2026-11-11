#!/usr/bin/env python3
"""Print the manifests of the test cluster, as one JSON list for kubectl.

Usage:
    manifests.py fakes <image ref>
    manifests.py setup <image ref>
    manifests.py receiver <image ref> <agents JSON file> <token file>

- fakes: the fake model gateway, the fake trace collector and the fake GitHub,
  each in the image under test (tests/kind/fakes.py). The model gateway and the
  collector have the service names of the platform, so that the examples work
  as they are: litellm-app in `ai`, alloy in `monitoring`.
- setup: the Job that imports the provider profiles and makes the providers.
- receiver: the run receiver as tnwks-ops runs it, with its Role, the service
  account of the run jobs, the token, the list of agents and the sandbox policy.

The test values come from tests/support. It uses the standard library only.
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO / "tests"))

from support import fake_github, mock_model  # noqa: E402

CLUSTER = "tnwks-image-test"
WORKFLOWS_NS = "agents-workflows"
SUPPORT_NS = "test-support"
GATEWAY_URL = f"https://openshell.{WORKFLOWS_NS}.svc.cluster.local:8080"
MTLS_SECRET = "openshell-client-tls"
TOKEN_SECRET = "run-receiver-token"
RECEIVER_URL = f"http://run-receiver.{WORKFLOWS_NS}.svc.cluster.local:8080"

# (namespace, name, kind of fake, port)
FAKES = [
    ("ai", "litellm-app", "model", 4000),
    ("monitoring", "alloy", "collector", 4318),
    (SUPPORT_NS, "github", "github", 8000),
]
GITHUB_HOST = f"github.{SUPPORT_NS}.svc.cluster.local"
GITHUB_PORT = 8000
GITHUB_URL = f"http://{GITHUB_HOST}:{GITHUB_PORT}"

RESTRICTED = {
    "allowPrivilegeEscalation": False,
    "readOnlyRootFilesystem": True,
    "capabilities": {"drop": ["ALL"]},
}
POD_SECURITY = {
    "runAsNonRoot": True,
    "runAsUser": 10001,
    "runAsGroup": 10001,
    "seccompProfile": {"type": "RuntimeDefault"},
}
TMP = {"name": "tmp", "emptyDir": {"sizeLimit": "64Mi"}}
TMP_MOUNT = {"name": "tmp", "mountPath": "/tmp"}
MTLS = {"name": "mtls", "secret": {"secretName": MTLS_SECRET, "defaultMode": 0o440}}
MTLS_MOUNT = {"name": "mtls", "mountPath": "/mtls", "readOnly": True}


def namespace(name: str) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": name, "labels": {"pod-security.kubernetes.io/enforce": "restricted"}},
    }


def config_map(name: str, ns: str, data: dict) -> dict:
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": ns}, "data": data}


def container(name: str, image: str, **extra) -> dict:
    c = {"name": name, "image": image, "imagePullPolicy": "Never", "securityContext": RESTRICTED}
    c.update(extra)
    return c


def deployment(name: str, ns: str, c: dict, volumes: list, service_account: str = "") -> dict:
    spec = {
        "automountServiceAccountToken": bool(service_account),
        "enableServiceLinks": False,
        "securityContext": POD_SECURITY,
        "containers": [c],
        "volumes": volumes,
    }
    if service_account:
        spec["serviceAccountName"] = service_account
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {"name": name, "namespace": ns},
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {"metadata": {"labels": {"app": name}}, "spec": spec},
        },
    }


def service(name: str, ns: str, port: int) -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Service",
        "metadata": {"name": name, "namespace": ns},
        "spec": {"selector": {"app": name}, "ports": [{"name": "http", "port": port, "targetPort": port}]},
    }


def fakes(image: str) -> list:
    support = REPO / "tests" / "support"
    files = {"fakes.py": (HERE / "fakes.py").read_text()}
    for name in ("__init__.py", "mock_model.py", "fake_github.py", "cases.py"):
        files[f"support__{name}"] = (support / name).read_text()
    items = [{"key": k, "path": k.replace("support__", "support/")} for k in files]
    out = [namespace(ns) for ns in sorted({f[0] for f in FAKES})]
    for ns, name, kind, port in FAKES:
        c = container(
            "fake",
            image,
            command=["python3", "/fakes/fakes.py", kind, str(port)],
            env=[{"name": "PYTHONDONTWRITEBYTECODE", "value": "1"}],
            ports=[{"containerPort": port}],
            readinessProbe={"tcpSocket": {"port": port}, "periodSeconds": 2},
            volumeMounts=[{"name": "fakes", "mountPath": "/fakes", "readOnly": True}, TMP_MOUNT],
            resources={"requests": {"cpu": "10m", "memory": "48Mi"}, "limits": {"memory": "256Mi"}},
        )
        volumes = [{"name": "fakes", "configMap": {"name": "fakes", "items": items}}, TMP]
        out += [config_map("fakes", ns, files), deployment(name, ns, c, volumes), service(name, ns, port)]
    return out


def setup(image: str) -> list:
    providers = REPO / "examples" / "providers"
    files = {
        "setup.sh": (HERE / "setup.sh").read_text(),
        "profiles.py": (HERE / "profiles.py").read_text(),
    }
    for p in sorted(providers.glob("*.yaml")):
        files[p.name] = p.read_text()
    env = {
        "GATEWAY_URL": GATEWAY_URL,
        "MODEL_KEY": mock_model.KEY,
        "GITHUB_KEY": fake_github.TOKEN,
        "FAKE_GITHUB_HOST": GITHUB_HOST,
        "FAKE_GITHUB_PORT": str(GITHUB_PORT),
        "HOME": "/tmp",
    }
    c = container(
        "setup",
        image,
        command=["sh", "/setup/setup.sh"],
        env=[{"name": k, "value": v} for k, v in env.items()],
        volumeMounts=[{"name": "setup", "mountPath": "/setup", "readOnly": True}, MTLS_MOUNT, TMP_MOUNT],
    )
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": "kind-setup", "namespace": WORKFLOWS_NS},
        "spec": {
            "backoffLimit": 0,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "securityContext": POD_SECURITY,
                    "containers": [c],
                    "volumes": [{"name": "setup", "configMap": {"name": "kind-setup"}}, MTLS, TMP],
                }
            },
        },
    }
    return [config_map("kind-setup", WORKFLOWS_NS, files), job]


def receiver(image: str, agents_file: str, token: str) -> list:
    ns = WORKFLOWS_NS
    role = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "Role",
        "metadata": {"name": "run-receiver", "namespace": ns},
        "rules": [
            {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "get"]},
            {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
            {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
        ],
    }
    binding = {
        "apiVersion": "rbac.authorization.k8s.io/v1",
        "kind": "RoleBinding",
        "metadata": {"name": "run-receiver", "namespace": ns},
        "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "run-receiver"},
        "subjects": [{"kind": "ServiceAccount", "name": "run-receiver", "namespace": ns}],
    }
    accounts = [
        {
            "apiVersion": "v1",
            "kind": "ServiceAccount",
            "metadata": {"name": name, "namespace": ns},
            "automountServiceAccountToken": False,
        }
        for name in ("run-receiver", "workflow-run")
    ]
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": TOKEN_SECRET, "namespace": ns},
        "type": "Opaque",
        "stringData": {"token": token},
    }
    c = container(
        "run-receiver",
        image,
        args=["run-receiver"],
        env=[
            {"name": "RUN_RECEIVER_TOKEN", "valueFrom": {"secretKeyRef": {"name": TOKEN_SECRET, "key": "token"}}},
            {"name": "POD_NAMESPACE", "valueFrom": {"fieldRef": {"fieldPath": "metadata.namespace"}}},
        ],
        ports=[{"containerPort": 8080}],
        readinessProbe={"httpGet": {"path": "/healthz", "port": 8080}, "periodSeconds": 2},
        volumeMounts=[{"name": "agents", "mountPath": "/etc/run-receiver", "readOnly": True}, TMP_MOUNT],
        resources={"requests": {"cpu": "10m", "memory": "64Mi"}, "limits": {"memory": "256Mi"}},
    )
    volumes = [{"name": "agents", "configMap": {"name": "run-receiver-agents"}}, TMP]
    return [
        *accounts,
        role,
        binding,
        secret,
        config_map("run-receiver-agents", ns, {"agents.yaml": Path(agents_file).read_text()}),
        config_map(
            "renovate-reviewer-policy", ns, {"policy.yaml": (REPO / "examples" / "sandbox-policy.yaml").read_text()}
        ),
        deployment("run-receiver", ns, c, volumes, service_account="run-receiver"),
        service("run-receiver", ns, 8080),
    ]


def sweep(image: str, name: str, max_age: int, dry_run: bool) -> dict:
    """A Job of the sweep, as the CronJob of tnwks-ops runs it."""
    env = {"OPENSHELL_GATEWAY_URL": GATEWAY_URL, "OPENSHELL_MTLS_DIR": "/mtls", "SWEEP_MAX_AGE_SECONDS": str(max_age)}
    c = container(
        "sweep",
        image,
        args=["sweep", "--dry-run"] if dry_run else ["sweep"],
        env=[{"name": k, "value": v} for k, v in env.items()],
        volumeMounts=[MTLS_MOUNT, TMP_MOUNT],
    )
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": WORKFLOWS_NS},
        "spec": {
            "backoffLimit": 0,
            "template": {
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": "workflow-run",
                    "automountServiceAccountToken": False,
                    "securityContext": POD_SECURITY,
                    "containers": [c],
                    "volumes": [MTLS, TMP],
                }
            },
        },
    }


def as_list(items: list) -> str:
    return json.dumps({"apiVersion": "v1", "kind": "List", "items": items}, indent=1)


def main() -> int:
    if len(sys.argv) < 3 or sys.argv[1] not in ("fakes", "setup", "receiver"):
        sys.exit(__doc__)
    what, image = sys.argv[1], sys.argv[2]
    if what == "fakes":
        items = fakes(image)
    elif what == "setup":
        items = setup(image)
    else:
        token = Path(sys.argv[4]).read_text().strip()
        items = receiver(image, sys.argv[3], token)
    print(as_list(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
