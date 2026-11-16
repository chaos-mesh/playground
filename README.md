# Chaos Mesh Playground

Choose a preset in Grafana for NetworkChaos, PodChaos, IOChaos, HTTPChaos, DNSChaos, or StressChaos. Run it against the demo workloads and watch the measured effect next to the experiment events.

## Run the demo

These steps use ARM64 Minikube with containerd, Linux netem and FUSE, and cgroup v2. Use a dedicated cluster for the demo. If you use a different profile, substitute its name for `minikube` in the commands below.

```sh
minikube start -p minikube --keep-context --driver=docker \
  --container-runtime=containerd --kubernetes-version=v1.37.0 --cpus=2 --memory=3600
```

The demo uses [Chaos Mesh v2.8.8](https://github.com/chaos-mesh/chaos-mesh/releases/tag/v2.8.8). Its ARM64 daemon image includes a native [toda binary](https://github.com/chaos-mesh/toda/commit/75e58ca74e259fff7a14a48d29f17daa563fdd74) for IOChaos and [chaos-tproxy v0.6.0](https://github.com/chaos-mesh/chaos-tproxy/releases/tag/v0.6.0) for HTTPChaos recovery. The DNSChaos static preset stays out of the demo until a release supports it.

Build and load the ARM64 daemon. Set `toda_source` to your local checkout.

```sh
toda_source=/path/to/toda
build_context=$(mktemp -d)
git -C "$toda_source" archive 75e58ca74e259fff7a14a48d29f17daa563fdd74 |
  tar -x -C "$build_context"
docker build --platform linux/arm64 -f deploy/chaos-daemon-arm64.Dockerfile \
  -t playground/chaos-daemon:2.8.8-toda-arm64 "$build_context"
rm -r "$build_context"
minikube -p minikube image load playground/chaos-daemon:2.8.8-toda-arm64
```

Install Chaos Mesh using the matching chart from your local checkout.

```sh
chaos_mesh_source=/path/to/chaos-mesh
chart_context=$(mktemp -d)
git -C "$chaos_mesh_source" archive v2.8.8 helm/chaos-mesh |
  tar -x -C "$chart_context"
helm install playground-chaos "$chart_context/helm/chaos-mesh" \
  --kube-context minikube --namespace playground-chaos --create-namespace \
  -f deploy/chaos-mesh-values.yaml --wait
rm -r "$chart_context"
```

Create the Grafana password once and deploy the demo.

```sh
kubectl --context minikube apply -f deploy/namespace.yaml
kubectl --context minikube -n playground-demo create secret generic grafana-admin \
  --from-literal=password="$(openssl rand -hex 18)"
kubectl --context minikube apply -k .
kubectl --context minikube -n playground-demo wait --for=condition=Available deployment --all --timeout=180s
kubectl --context minikube -n playground-demo port-forward svc/grafana 3300:8080
```

Open [Chaos Mesh Playground](http://127.0.0.1:3300/d/chaos-mesh-playground/chaos-mesh-playground) and sign in as `admin`. Read the password from the `grafana-admin` secret.

```sh
kubectl --context minikube -n playground-demo get secret grafana-admin \
  -o jsonpath='{.data.password}' | base64 -d
```

Choose **Chaos type**, then **Action**. The **Impact** chart switches to the measurement for the selected case.

| Chaos | Action | Measurement |
| --- | --- | --- |
| NetworkChaos | delay | HTTP request latency |
| | loss | Missing UDP sequence numbers |
| | bandwidth | Received UDP payload throughput |
| | duplicate | Duplicate UDP packets |
| | corrupt | UDP payload checksum errors |
| | partition | UDP reachability |
| | netem | UDP one-way delay and packet loss |
| PodChaos | pod-failure | HTTP probe success |
| | pod-kill | Ready state of the old and replacement Pods |
| | container-kill | Restarts of the target container |
| IOChaos | latency | Test file read latency |
| | fault | Read error rate |
| | attrOverride | Test file permissions in octal |
| | mistake | Read content mismatch rate |
| HTTPChaos | abort | HTTP response failures |
| | delay | HTTP response latency |
| | replace | Replaced JSON response body |
| | patch | Patched response header |
| DNSChaos | error | DNS lookup failures |
| | random | DNS answers that differ from the Service IP |
| StressChaos | CPU | CPU cores used by the target container |
| | memory | Anonymous memory used by the target container |

Wait for a few baseline samples and click **Run experiment**. Timed cases last 30 seconds and can be stopped early. For Pod kill and container kill, wait for **Workload Ready** while Kubernetes restores the Deployment. The runner executes one case at a time.

IOChaos is scoped to a test file on an `emptyDir`. UDP cases send traffic between two demo Pods. The sender attaches an application checksum so the probe can measure corruption in received UDP payloads.

Grafana filters event markers to the selected preset and identifies every run by UID. **Chaos Mesh recovered** marks the controller event. **Workload Ready** comes from the replacement Pod or restarted container observed through the Kubernetes API. Prometheus retains the samples for the life of its Pod. The runner keeps up to 100 prior runs in memory.

Open the **Chaos Dashboard** tab to inspect the current run of the selected preset. Its gateway requires a Grafana session and allows read requests. The Dashboard service account can only read resources in `playground-demo`. The dedicated local Grafana instance enables HTML in provisioned text panels to embed the original Dashboard.


## Remove the demo

Stop port-forwarding and delete the demo namespace. Remove the Chaos Mesh release only if you installed it for this demo.

```sh
kubectl --context minikube delete namespace playground-demo
helm uninstall playground-chaos --kube-context minikube --namespace playground-chaos
```
