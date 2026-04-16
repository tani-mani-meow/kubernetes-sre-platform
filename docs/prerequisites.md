# Prerequisites

What a machine needs before `make demo` works. Run `bash scripts/doctor.sh` to check
all of this automatically.

## Resource requirements

The full stack is Kubernetes plus Prometheus, Grafana, Alertmanager, Loki, Tempo, an
OTel Collector, Argo CD, Argo Rollouts, Chaos Mesh, Postgres, a load generator, and
three application services.

| Resource | Minimum | Comfortable |
|---|---|---|
| RAM available to Docker | 8 GiB | 12 GiB |
| CPU cores | 4 | 8 |
| Free disk | 20 GB | 40 GB |

On Windows and macOS the limit that matters is the one Docker Desktop is allowed, not
the one the machine has. Docker Desktop → Settings → Resources.

## Tools

| Tool | Needed for | Install |
|---|---|---|
| Docker | everything | [Docker Desktop](https://docs.docker.com/desktop/) |
| kind | the local cluster | `go install sigs.k8s.io/kind@latest`, `brew install kind`, or the [release binary](https://github.com/kubernetes-sigs/kind/releases) |
| kubectl | everything | [docs](https://kubernetes.io/docs/tasks/tools/) |
| helm | platform charts | [docs](https://helm.sh/docs/intro/install/) — 3.x or 4.x |
| kustomize | rendering overlays | [docs](https://kubectl.docs.kubernetes.io/installation/kustomize/) |
| Python 3.11+ | services, chaos harness | [python.org](https://www.python.org/downloads/) |
| sloth | `make slo-gen` only | [sloth.dev](https://sloth.dev) |
| terraform | the EKS capture phase only | [developer.hashicorp.com](https://developer.hashicorp.com/terraform/downloads) |
| make | convenience only | every target is also `bash scripts/<name>.sh` |

`kubectl argo rollouts` (the [Rollouts plugin](https://argo-rollouts.readthedocs.io/en/stable/installation/#kubectl-plugin-installation))
is optional but makes the canary demo much easier to watch.

## Windows

Everything is written for bash. Use **Git Bash** (ships with Git for Windows), not
PowerShell or `cmd`.

Docker Desktop needs WSL2, and both need an **elevated** install:

```powershell
wsl --install
```

That reboots the machine. Then install Docker Desktop and confirm it is using the WSL2
backend (Settings → General → "Use the WSL 2 based engine").

Give WSL2 enough memory — its default is a fraction of host RAM and the stack will get
OOMKilled without this. Create `%UserProfile%\.wslconfig`:

```ini
[wsl2]
memory=12GB
processors=8
```

Then `wsl --shutdown` and restart Docker Desktop.

GNU make is not part of Git for Windows and its usual installers want admin. Rather
than fight that, run the scripts directly — `bash scripts/demo.sh` is exactly what
`make demo` does.

### inotify limits

Grafana, Loki and the Prometheus config-reloaders all watch files. On a multi-node kind
cluster every node container shares the *host's* inotify limits, and the stack exhausts
the default. The symptom is a crash-loop with `too many open files`, which reads like a
broken chart rather than a host limit.

**This cannot be fixed in `kind-config.yaml`.** inotify limits are not namespaced, so a
node container always reads the host's value, and kind's node schema has no sysctl
field. The fix has to be applied to whatever kernel is hosting Docker.

On **Windows**, that kernel belongs to the `docker-desktop` WSL distro — not the default
Ubuntu distro, and not Windows itself:

```bash
wsl -d docker-desktop -u root sh -c 'echo 524288 > /proc/sys/fs/inotify/max_user_watches'
wsl -d docker-desktop -u root sh -c 'echo 512 > /proc/sys/fs/inotify/max_user_instances'
```

That resets when the distro restarts. To persist it, add to `%UserProfile%\.wslconfig`:

```ini
[wsl2]
kernelCommandLine = sysctl.fs.inotify.max_user_watches=524288 sysctl.fs.inotify.max_user_instances=512
```

On **Linux**, the usual place — `/etc/sysctl.d/99-inotify.conf`, then `sysctl --system`.

`scripts/kind-up.sh` reads the current values and warns if they are low, so a failed
demo points at this section rather than at Loki.

## Repository location

The repo works anywhere, with one caveat: if it lives inside a cloud-synced folder
(OneDrive, Dropbox, iCloud), the sync client will churn on `.git/` during every commit
and can hold file locks that make git operations fail intermittently. If you hit
`unable to unlink` or `permission denied` errors from git, that is why. Either mark the
folder "always keep on this device", or move the repo outside the synced tree — GitHub
is already the backup.

## GitHub access

`make demo` installs Argo CD and points it at this repository on GitHub, which is
public, so no credentials are needed. Note the consequence: **Argo CD syncs from
`main` on GitHub, not from your working copy.** Local edits to `gitops/` will not appear
in the cluster until they are pushed.

For iterating on application code, `make deploy-local` builds, loads into kind and
restarts directly, bypassing Git — see the comment at the top of
`scripts/deploy-local.sh` for why that does not fight Argo CD.
