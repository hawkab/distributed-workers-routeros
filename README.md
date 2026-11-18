# RouterOS worker

A generic ARM64 container adapter for RouterOS, including hAP ax³. It reuses the
[portable worker](https://gitflic.ru/project/distributed-workers/worker-portable)
protocol and runtime. Configuration and executable programs are supplied by the
operator; application-specific code and data do not belong in this repository.

The worker connects outbound, receives its server-assigned identity and reports
actual CPU, memory and storage availability. It executes local allowlisted
programs or HTTP operations and streams binary results. A persistent state volume
retains checkpoints, effect receipts and completions until the server acknowledges
them. Program hashes, leases and result hashes are checked by the shared SDK.

## Build

`runtime.lock.json` pins a signed portable SDK. `tools/prepare.py` verifies its RSA
signature and hash before placing it in an ignored build context. The public
image contains neither credentials nor deployment endpoints.

The release builder uses the official Alpine APK trust keys to assemble an ARM64
filesystem without executing foreign binaries. It runs the process supervisor
tests on the builder's native PHP. `ci/Builderfile` provides these tools:

```sh
python3 -B tools/cache_runtime.py --archive /path/to/fleet-worker-portable.zip \
  --manifest /path/to/portable-manifest.json
docker build -t fleet-router-image-builder:1 -f ci/Builderfile .
docker run --rm -v "$PWD:/workspace:ro" -v "$PWD/dist:/out" -w /workspace \
  fleet-router-image-builder:1 python3 -B tools/build.py \
  --output /out --tag v0.1.0 --commit "$(git rev-parse HEAD)"
```

`container/Dockerfile` is also available for a native ARM64 Docker builder after
preparing `.build/sdk`. It uses the same PHP adapter and supervisor.

## RouterOS installation

Tested on RouterOS 7.24.5, ARM64. Install the container package and enable container
support through RouterOS device-mode before deployment. Prepare an isolated VETH
interface; the worker needs outbound HTTPS and the task network selected by its
operator. No inbound application port is required.

Keep `worker.local.json`, proxy configuration and update endpoints outside Git.
Start from `worker.example.json`; provision the SDK's private update-source chain
in the order Gitea, GitFlic, GitHub. Runtime updates verify the pinned RSA key and
are applied between tasks, preserving the worker identity and durable state.

Mount configuration at `/config`, persistent state at `/state` and optional
operator-managed executables at `/programs`. Supply `/config/worker.local.json`
as the container command. Select CPU affinity and memory limits for the device's
available capacity, leaving resources for routing.

The base runtime is approximately 23 MB unpacked. Additional programs, their
libraries and working files require separate space. USB storage or an encrypted
SMB3 mount can supply persistent volumes. RouterOS SSHFS mounts may be readable
by RouterOS but inaccessible inside a user-namespaced container; verify access
from the actual container before provisioning its identity. Validate write,
rename, sync and lock operations on the state filesystem. With RouterOS CIFS,
UID 0 inside the unprivileged container may be required for permission operations;
keep `privileged=no` and protect the remote share with its server ACL.

For maintenance, create `/state/maintenance` and wait for `/state/adapter-drained`.
The supervisor finishes its current tick and then blocks new work, including
after an automatic container restart. Persistent completions stay on the state
volume. Only after this barrier may RouterOS settings or the image be changed.
Remove `/state/maintenance` to resume. The marker disappears before the next tick.

Do not change settings or use RouterOS's stop command during active work: its
separate stop-time can force termination. A power loss is recovered through
persistent state and the queue lease. SIGTERM also drains a current tick without
taking another, but the maintenance marker is the persistent operator barrier.

## Releases and checks

Version-tag pipelines build and sign the canonical image on GitFlic. Gitea and
GitHub verify its RSA signature, source revision, ARM64 configuration and every
filesystem layer, then publish the same archive bytes. The signing key is available
only to the protected signing job. The ignored `mirror-git.py` publisher attempts
all mirrors independently.

```sh
python3 -B -m unittest discover -s tests -v
python3 -B -m unittest discover -s ci/tests -v
```

The process tests check draining on shutdown, immediate continuation after useful
work, restart after a signed runtime update, and the persistent maintenance barrier. Hardware acceptance is performed
before publishing a new adapter implementation.
