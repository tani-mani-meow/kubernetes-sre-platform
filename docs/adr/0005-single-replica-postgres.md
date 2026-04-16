# 5. Postgres is a single-replica StatefulSet

Status: Accepted (2026-09-01)

## Context

`catalog` owns products and orders in Postgres, and it is the only service whose readiness
probe depends on something external. The production-shaped answer is an operator —
CloudNativePG, or Zalando's — giving streaming replication, automated failover, backups
and point-in-time recovery.

The project's deliverable is a measured recovery time per failure mode. A database that
fails over in seconds produces one number; a database that has nowhere to fail over to
produces a different and more interesting one, and the difference is the whole content of
the `pod-kill-postgres` and `az-loss` scenarios.

## Decision

`gitops/services/postgres/base/statefulset.yaml`: `replicas: 1`, `postgres:16-alpine`, one
`volumeClaimTemplate` (2Gi, storage class set by overlay), seeded by a Job. No operator,
no replication, no backups.

A StatefulSet rather than a Deployment, for the at-most-one guarantee: the replacement pod
is not started until `postgres-0` has fully terminated. A Deployment's surge would briefly
run two Postgres processes against one data directory, which is a corrupted database
rather than a rollout.

## Consequences

`catalog` has nowhere to go when Postgres is gone. `/readyz` pings the connection pool, so
every catalog replica leaves the Service within about ten seconds, and storefront's calls
to catalog start failing. `pod-kill-postgres` measures that rather than asserting it.

Recovery is bounded by pod scheduling plus volume attach, not by anything the application
does — the kind of number that stays invisible until someone measures it. On EKS that is
an EBS detach and attach; on kind it is a local-path directory, which is why the two
clusters' MTTRs for this scenario are not comparable.

`az-loss` deliberately targets a zone that does not hold Postgres.
`chaos/experiments/az-loss.sh` reads the Postgres pod's node, looks up its zone label, and
picks a different one. Not to protect the database — the scenario is supposed to hurt —
but because losing it makes every other signal in that run unreadable, and
`pod-kill-postgres` already measures the database in isolation.

No backups, no PITR, no failover. The honest statement is that this database is disposable,
and it is only disposable because it holds twelve seeded products and whatever orders the
load generator wrote since the cluster came up.

Running Postgres without an operator means doing by hand the things an operator would have
handled, and the StatefulSet carries the evidence of that:

- `fsGroup: 70` with `PGDATA` in a *subdirectory* of the mount. `fsGroup` makes the volume
  root group-writable, and `initdb` refuses a data directory with group permissions; a
  subdirectory it creates itself gets 0700.
- `preStop: pg_ctl stop -m fast`. A bare SIGTERM is a Postgres *smart* shutdown, which
  waits for every client session to disconnect — and catalog holds a pooled connection open
  indefinitely, so the container would be SIGKILLed at the end of the grace period, leaving
  an unclean shutdown and WAL recovery on the next start.
- `pg_isready` over TCP rather than the unix socket, because the entrypoint's first-boot
  initialisation server listens on the socket with `listen_addresses=''` and a socket
  check would report it ready.
- `startupProbe.failureThreshold: 30` (150s), because `initdb` plus the seed on a kind
  local-path volume is slow and a probe that gives up restarts the pod mid-`initdb`.

catalog compensates with an init container that blocks on `pg_isready`, because
`asyncpg.create_pool(min_size=2)` connects eagerly at startup and would otherwise put
catalog into a CrashLoopBackOff whose kubelet backoff reaches five minutes.

## Rejected

**CloudNativePG or an equivalent operator.** It would produce a database that survives a
pod kill — and would delete the most informative scenario in the suite along with it. It
also adds an operator, its CRDs and its own failure modes to a cluster whose measurements
are the product. The availability cost of not using it is recorded here and measured in
`evidence/` rather than hidden.

**Two replicas with manual failover.** Replication without automated promotion is the worst
of both: the extra moving parts, none of the recovery, and a scenario whose result depends
on how fast the operator running the demo can type.
