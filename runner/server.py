import copy
import datetime
import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


NAMESPACE_API = "/apis/chaos-mesh.org/v1alpha1/namespaces/playground-demo/"
POD_PATH = "/api/v1/namespaces/playground-demo/pods?labelSelector=app%3Dplayground-podinfo"
TERMINAL = {"recovered", "stopped"}
ONE_SHOT = {"pod-kill", "container-kill"}
READY_ANNOTATION = "playground.chaos-mesh.org/workload-ready-at"


class APIError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Kubernetes:
    def __init__(self):
        self.base = "https://kubernetes.default.svc"
        self.service_account = Path("/var/run/secrets/kubernetes.io/serviceaccount")
        self.context = ssl.create_default_context(cafile=str(self.service_account / "ca.crt"))

    def request(self, method, path, body=None):
        headers = {
            "Authorization": "Bearer " + (self.service_account / "token").read_text().strip(),
            "Content-Type": "application/merge-patch+json" if method == "PATCH" else "application/json",
        }
        data = None if body is None else json.dumps(body).encode()
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, context=self.context, timeout=10) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            detail = json.loads(error.read()).get("message", str(error))
            raise APIError(error.code, detail) from error
        except (urllib.error.URLError, TimeoutError) as error:
            raise APIError(502, "Kubernetes API unavailable") from error


def event_time(timestamp):
    return int(datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00")).timestamp() * 1000)


def run_status(obj):
    status = obj.get("status", {})
    experiment = status.get("experiment", {})
    records = experiment.get("containerRecords", [])
    events = [dict(event, target=record["id"]) for record in records if record.get("selectorKey") == "." for event in record.get("events", [])]
    events.sort(key=lambda event: event.get("timestamp", ""))
    conditions = {condition["type"]: condition["status"] for condition in status.get("conditions", [])}
    applied = any(event["type"] == "Succeeded" and event["operation"] == "Apply" for event in events)
    recovered = any(event["type"] == "Succeeded" and event["operation"] == "Recover" for event in events)
    complete = experiment.get("desiredPhase") == "Stop" and all(record["phase"] == "Not Injected" for record in records)
    one_shot = obj["kind"] == "PodChaos" and obj["spec"]["action"] in ONE_SHOT
    if complete and not one_shot:
        state = "recovered" if applied and recovered else "stopped"
    elif obj["metadata"].get("deletionTimestamp"):
        state = "recovering"
    elif experiment.get("desiredPhase") == "Stop":
        state = "recovering"
    elif conditions.get("AllInjected") == "True" and applied:
        state = "waiting-for-workload" if one_shot else "running"
    else:
        state = "pending"
    return {
        "state": state,
        "action": obj["spec"].get("action", obj["metadata"]["name"].partition("-")[2]),
        "case": obj["metadata"]["name"],
        "kind": obj["kind"],
        "runUID": obj["metadata"]["uid"],
        "events": events,
        "conditions": conditions,
    }


def ready_condition(pod):
    if pod["metadata"].get("deletionTimestamp"):
        return None
    return next((c for c in pod.get("status", {}).get("conditions", [])
                 if c["type"] == "Ready" and c["status"] == "True"), None)


def workload_recovered_at(run, pods):
    applied = next(event for event in run["events"] if event["type"] == "Succeeded" and event["operation"] == "Apply")
    old_pod = applied["target"].split("/")[1]
    applied_at = event_time(applied["timestamp"])
    for pod in pods:
        ready = ready_condition(pod)
        if not ready:
            continue
        if run["action"] == "pod-kill":
            if pod["metadata"]["name"] != old_pod and event_time(pod["metadata"]["creationTimestamp"]) >= applied_at:
                return event_time(ready["lastTransitionTime"])
        elif pod["metadata"]["name"] == old_pod:
            for container in pod["status"].get("containerStatuses", []):
                current = container.get("state", {}).get("running", {})
                previous = container.get("lastState", {}).get("terminated", {})
                if (container["name"] == "podinfo" and current and previous
                        and event_time(previous["finishedAt"]) >= applied_at):
                    return max(event_time(current["startedAt"]), event_time(ready["lastTransitionTime"]))
    return None


def annotations(run):
    operation_name = {"Apply": "injected", "Recover": "recovered"}
    marks = [
        {"time": event_time(event["timestamp"]), "event": operation_name[event["operation"]],
         "target": event["target"], "case": run["case"], "runUID": run["runUID"]}
        for event in run["events"]
        if event["type"] == "Succeeded" and event["operation"] in operation_name
    ]
    if "workloadRecoveredAt" in run:
        marks.append({"time": run["workloadRecoveredAt"], "event": "workload-ready",
                      "case": run["case"], "runUID": run["runUID"]})
    return marks


class Runner:
    def __init__(self, kube, presets):
        self.kube = kube
        self.presets = presets
        self.lock = threading.Lock()
        self.history = {}

    def preset(self, case):
        try:
            return self.presets[case]
        except KeyError as error:
            raise APIError(400, "Unknown Playground preset") from error

    def collection(self, case):
        return NAMESPACE_API + self.preset(case)["kind"].lower()

    def object_path(self, case):
        return self.collection(case) + "/" + case

    def pods(self):
        return self.kube.request("GET", POD_PATH)["items"]

    def observe(self, obj, pods):
        run = run_status(obj)
        if run["state"] == "waiting-for-workload":
            metadata = obj["metadata"]
            recorded = metadata.get("annotations", {}).get(READY_ANNOTATION)
            timestamp = int(recorded) if recorded else workload_recovered_at(run, pods)
            if timestamp is not None:
                if not recorded:
                    path = self.object_path(run["case"])
                    try:
                        self.kube.request("PATCH", path, {"metadata": {
                            "resourceVersion": metadata["resourceVersion"],
                            "annotations": {READY_ANNOTATION: str(timestamp)},
                        }})
                    except APIError as error:
                        if error.status not in (404, 409):
                            raise
                run.update(state="recovered", workloadRecoveredAt=timestamp)
        return run

    def current(self, path):
        try:
            return self.kube.request("GET", path)
        except APIError as error:
            if error.status == 404:
                return None
            raise

    def current_experiments(self):
        experiments = []
        for kind in sorted({preset["kind"] for preset in self.presets.values()}):
            collection = NAMESPACE_API + kind.lower()
            objects = self.kube.request("GET", collection + "?labelSelector=app.kubernetes.io%2Fmanaged-by%3Dplayground-runner")
            experiments.extend(obj for obj in objects["items"] if obj["metadata"]["name"] in self.presets)
        return experiments

    @staticmethod
    def require_owned(obj):
        if obj["metadata"].get("labels", {}).get("app.kubernetes.io/managed-by") != "playground-runner":
            raise APIError(409, f'{obj["metadata"]["name"]} is not owned by this runner')

    def start(self, case="network-delay"):
        path = self.object_path(case)
        with self.lock:
            pods = self.pods()
            for obj in self.current_experiments():
                if self.observe(obj, pods)["state"] not in TERMINAL:
                    raise APIError(409, f'Wait for {obj["metadata"]["name"]} to finish')

            current = self.current(path)
            if current is not None:
                self.require_owned(current)
                run = self.observe(current, pods)
                if run["state"] not in TERMINAL:
                    raise APIError(409, "Previous run has not finished")
                uid = run["runUID"]
                self.history[uid] = run
                if len(self.history) > 100:
                    del self.history[next(iter(self.history))]
                self.kube.request("DELETE", path, {"preconditions": {"uid": uid}})
                deadline = time.monotonic() + 10
                while self.current(path) is not None:
                    if time.monotonic() >= deadline:
                        raise APIError(409, "Previous run cleanup is in progress; retry shortly")
                    time.sleep(0.2)
            created = self.kube.request("POST", self.collection(case), copy.deepcopy(self.preset(case)))
            return {"success": True, "runUID": created["metadata"]["uid"], "state": "pending"}

    def stop(self, case="network-delay"):
        path = self.object_path(case)
        preset = self.preset(case)
        if preset["kind"] == "PodChaos" and preset["spec"]["action"] in ONE_SHOT:
            raise APIError(409, "One-shot actions finish when Kubernetes restores the workload")
        with self.lock:
            current = self.current(path)
            if current is None:
                return {"success": True, "state": "idle", "runUID": None}
            self.require_owned(current)
            run = run_status(current)
            if run["state"] in TERMINAL:
                return {"success": True, "state": run["state"], "runUID": run["runUID"]}
            self.kube.request("PATCH", path, {"metadata": {
                "resourceVersion": current["metadata"]["resourceVersion"],
                "annotations": {"experiment.chaos-mesh.org/pause": "true"},
            }})
            return {"success": True, "state": "recovering", "runUID": run["runUID"]}

    def status(self, case="network-delay"):
        with self.lock:
            current = self.current(self.object_path(case))
            return self.observe(current, self.pods()) if current else {"state": "idle", "runUID": None, "events": []}

    def observations(self):
        with self.lock:
            pods = self.pods()
            runs = dict(self.history)
            for obj in self.current_experiments():
                run = self.observe(obj, pods)
                runs[run["runUID"]] = run
            return pods, [mark for run in runs.values() for mark in annotations(run)]

    def marks(self, start, end):
        _, marks = self.observations()
        return sorted((mark for mark in marks if start <= mark["time"] <= end), key=lambda mark: mark["time"])

    def metrics(self):
        pods, marks = self.observations()
        samples = {(mark["runUID"], mark["event"], mark["case"]): mark["time"] for mark in marks}
        lines = ["# TYPE playground_chaos_event_timestamp_milliseconds gauge"]
        for (uid, event, case), timestamp in samples.items():
            lines.append(f'playground_chaos_event_timestamp_milliseconds{{run_uid="{uid}",event="{event}",scenario="{case}"}} {timestamp}')
        ready_pods = [pod for pod in pods if ready_condition(pod)]
        for pod in pods:
            name = pod["metadata"]["name"]
            lines.append(f'playground_pod_ready{{pod="{name}"}} {int(pod in ready_pods)}')
            for container in pod.get("status", {}).get("containerStatuses", []):
                if container["name"] == "podinfo":
                    lines.append(f'playground_pod_container_restarts{{pod="{name}"}} {container["restartCount"]}')
        for case, preset in self.presets.items():
            title = preset["metadata"]["annotations"]["playground.chaos-mesh.org/title"]
            lines.append(f'playground_scenario{{kind="{preset["kind"]}",scenario="{case}",title={json.dumps(title, ensure_ascii=False)}}} 1')
        return "\n".join(lines) + "\n"


class Handler(BaseHTTPRequestHandler):
    runner = None
    allowed_origin = os.environ.get("ALLOWED_ORIGIN", "http://localhost:3000")

    def respond(self, status, body, content_type="application/json"):
        data = json.dumps(body).encode() if content_type == "application/json" else body.encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if self.headers.get("Origin") == self.allowed_origin:
            self.send_header("Access-Control-Allow-Origin", self.allowed_origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.respond(200, {})

    def do_GET(self):
        request = urllib.parse.urlsplit(self.path)
        try:
            if request.path == "/healthz":
                self.respond(200, {"success": True})
            elif request.path == "/status":
                query = urllib.parse.parse_qs(request.query)
                case = query.get("case", [None])[0]
                if case is None:
                    case = "network-" + query.get("action", ["delay"])[0]
                self.respond(200, self.runner.status(case))
            elif request.path == "/dashboard-current":
                case = urllib.parse.parse_qs(request.query).get("case", [""])[0]
                obj = self.runner.current(self.runner.object_path(case))
                if obj is None:
                    self.respond(200, "Run this preset to view its experiment details.", "text/plain; charset=utf-8")
                    return
                self.runner.require_owned(obj)
                self.send_response(302)
                self.send_header("Location", "/chaos-dashboard/#/experiments/" + obj["metadata"]["uid"])
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", "0")
                self.end_headers()
            elif request.path == "/metrics":
                self.respond(200, self.runner.metrics(), "text/plain; version=0.0.4")
            elif request.path == "/annotations":
                query = urllib.parse.parse_qs(request.query)
                start = int(query.get("from", ["0"])[0])
                end = int(query.get("to", ["32503680000000"])[0])
                self.respond(200, self.runner.marks(start, end))
            else:
                self.respond(404, {"success": False, "error": "Unknown route"})
        except ValueError:
            self.respond(400, {"success": False, "error": "from and to must be Unix milliseconds"})
        except APIError as error:
            self.respond(error.status, {"success": False, "error": str(error)})

    def do_POST(self):
        try:
            if self.path not in ("/run", "/stop"):
                raise APIError(404, "Unknown route")
            if self.headers.get("Origin") not in (None, self.allowed_origin):
                raise APIError(403, "Origin not allowed")
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                raise APIError(415, "Use application/json")
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 128:
                raise APIError(400, "Expected a Playground preset ID")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict) or data.keys() not in ({"case"}, {"action"}, set()):
                raise APIError(400, "Expected a Playground preset ID")
            key, value = next(iter(data.items()), ("action", "delay"))
            if not isinstance(value, str):
                raise APIError(400, "Unknown Playground preset")
            case = value if key == "case" else "network-" + value
            result = self.runner.start(case) if self.path == "/run" else self.runner.stop(case)
            self.respond(202, result)
        except ValueError:
            self.respond(400, {"success": False, "error": "Expected a Playground preset ID"})
        except APIError as error:
            self.respond(error.status, {"success": False, "error": str(error)})


if __name__ == "__main__":
    presets = {}
    for file in sorted(Path("/scenarios").glob("*.json")):
        preset = json.loads(file.read_text())
        presets[preset["metadata"]["name"]] = preset
    Handler.runner = Runner(Kubernetes(), presets)
    ThreadingHTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
