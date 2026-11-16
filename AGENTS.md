# Repository Guidelines

## Project Structure

Chaos Mesh Playground is a Kubernetes demo that runs preset experiments from Grafana and displays workload measurements alongside Chaos Mesh events.

- `scenarios/` holds JSON presets for NetworkChaos, PodChaos, IOChaos, HTTPChaos, DNSChaos, and StressChaos. Its Kustomization packages the enabled presets into the runner ConfigMap.
- `runner/` contains the Python API, its unit tests, and its Kubernetes permissions. `workloads/` contains Podinfo, Blackbox Exporter, and the UDP, file I/O, HTTP, DNS, and container resource probes.
- `grafana/` contains the dashboard, provisioning, and same-origin gateway. `prometheus/` configures metric collection. The root `kustomization.yaml` deploys the demo. `deploy/` contains the namespace and the daemon image recipe.

## Development

- Write documentation, comments, UI copy, and user-facing messages in English. Follow existing formatting and keep the runner and probes on the Python standard library.
- Keep scenario IDs stable. When adding a preset, update the scenario ConfigMap, runner RBAC, and Grafana queries together. Prefer an existing workload or exporter when it can measure the fault directly.
- Accept only bundled presets. Scope selectors to demo workloads in `playground-demo` and keep Grafana, Prometheus, and the runner outside the injection targets. The runner executes one preset at a time. IOChaos uses a test file on an `emptyDir`.

## Testing and Local Deployment

- Run `python3 -B -m unittest discover -s runner` for runner tests and `kubectl kustomize .` to render the manifests.
- Follow [README.md](README.md) to deploy into the `minikube` context and open Grafana on port 3300. The README also covers building the demo daemon image with `toda` for IOChaos and `tproxy` for HTTPChaos recovery.
- In Kubernetes, verify the actual fault and the workload measurement. Check that timed faults recover and that one-shot faults show Kubernetes workload readiness. For changes to Grafana controls or annotations, run the same preset twice and verify that events are attributed to the correct run UID.

## Build Caches and Cleanup

Reuse existing tool and BuildKit caches across tasks and worktrees. Isolate source checkouts, test outputs, and runtime state as needed. Clean up temporary resources created for the task and preserve shared caches. Avoid global Docker pruning.
