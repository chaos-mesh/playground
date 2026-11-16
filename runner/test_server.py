import copy
import datetime
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from server import APIError, Handler, PAUSE_ANNOTATION, READY_ANNOTATION, Runner, annotations, run_status

PRESET = json.loads((Path(__file__).parent.parent / "scenarios/network-delay.json").read_text())


PRESETS = {"network-delay": PRESET}


def experiment(uid, phase="Run", recovered=False, preset=PRESET):
    obj = copy.deepcopy(preset)
    created = datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")
    obj["metadata"].update(uid=uid, resourceVersion="1", creationTimestamp=created)
    events = [{"type": "Succeeded", "operation": "Apply", "timestamp": "2026-11-14T01:00:00Z"}]
    if recovered:
        events.append({"type": "Succeeded", "operation": "Recover", "timestamp": "2026-11-14T01:00:30Z"})
    obj["status"] = {
        "conditions": [{"type": "AllRecovered", "status": "True" if recovered else "False"}],
        "experiment": {"desiredPhase": phase, "containerRecords": [
            {"id": "playground-demo/podinfo", "selectorKey": ".", "phase": "Not Injected" if recovered else "Injected", "events": events},
            {"id": "playground-demo/blackbox", "selectorKey": ".Target", "phase": "Not Injected" if recovered else "Injected", "events": events},
        ]},
    }
    return obj


class FakeKubernetes:
    def __init__(self, current=None):
        self.current = current
        self.count = 0
        self.deleted = []
        self.pods = []

    def request(self, method, path, body=None):
        if method == "GET":
            collection, _, query = path.partition("?")
            if collection.endswith("/pods") and query:
                return {"items": copy.deepcopy(self.pods)}
            if query:
                kind = collection.rsplit("/", 1)[-1]
                return {"items": [copy.deepcopy(self.current)] if self.current and self.current["kind"].lower() == kind else []}
            if self.current is None or not path.endswith("/" + self.current["metadata"]["name"]):
                raise APIError(404, "Not found")
            return copy.deepcopy(self.current)
        if method == "DELETE":
            assert body["preconditions"]["uid"] == self.current["metadata"]["uid"]
            if "resourceVersion" in body["preconditions"]:
                assert body["preconditions"]["resourceVersion"] == self.current["metadata"]["resourceVersion"]
            self.deleted.append(self.current["metadata"]["uid"])
            self.current = None
            return {}
        if method == "PATCH":
            self.current["metadata"].setdefault("annotations", {}).update(body["metadata"]["annotations"])
            return self.current
        if method == "POST":
            if self.current:
                raise APIError(409, "Already exists")
            self.count += 1
            self.current = experiment(f"run-{self.count}", preset=body)
            return self.current
        raise AssertionError(method)


class RunnerTests(unittest.TestCase):
    def test_two_runs_keep_distinct_apply_and_recover_marks(self):
        kube = FakeKubernetes()
        runner = Runner(kube, PRESETS)
        first = runner.start()
        with self.assertRaises(APIError) as error:
            runner.start()
        self.assertEqual(error.exception.status, 409)
        kube.current = experiment(first["runUID"], "Stop", True)
        second = runner.start()
        kube.current = experiment(second["runUID"], "Stop", True)
        marks = runner.marks(0, 32503680000000)
        self.assertEqual(len(marks), 4)
        self.assertEqual({mark["runUID"] for mark in marks}, {"run-1", "run-2"})
        self.assertEqual(kube.deleted, ["run-1"])
        self.assertEqual(len(runner.marks(1794618030000, 1794618030000)), 2)

    def test_selected_action_sets_preset_and_blocks_overlapping_run(self):
        bandwidth = json.loads((Path(__file__).parent.parent / "scenarios/network-bandwidth.json").read_text())
        kube = FakeKubernetes()
        runner = Runner(kube, {**PRESETS, "network-bandwidth": bandwidth})
        runner.start("network-bandwidth")
        self.assertEqual(kube.current["spec"]["bandwidth"]["rate"], "1mbit")
        self.assertEqual(runner.marks(0, 32503680000000)[0]["case"], "network-bandwidth")
        self.assertEqual(runner.stop("network-delay")["state"], "idle")
        with self.assertRaises(APIError) as conflict:
            runner.start("network-delay")
        self.assertEqual(conflict.exception.status, 409)
        with self.assertRaises(APIError) as unknown:
            runner.start("duplicate")
        self.assertEqual(unknown.exception.status, 400)

    def test_pod_kill_waits_for_replacement_and_marks_ready(self):
        case = "pod-pod-kill"
        preset = json.loads((Path(__file__).parent.parent / f"scenarios/{case}.json").read_text())
        current = experiment("pod-run", preset=preset)
        current["status"]["conditions"].append({"type": "AllInjected", "status": "True"})
        kube = FakeKubernetes(current)
        runner = Runner(kube, {**PRESETS, case: preset})
        self.assertEqual(runner.status(case)["state"], "waiting-for-workload")
        with self.assertRaises(APIError):
            runner.start()
        kube.pods = [{
            "metadata": {"name": "podinfo-replacement", "creationTimestamp": "2026-11-14T01:00:01Z"},
            "status": {"conditions": [
                {"type": "Ready", "status": "True", "lastTransitionTime": "2026-11-14T01:00:03Z"},
            ]},
        }]
        self.assertEqual(runner.status(case)["state"], "recovered")
        self.assertEqual({mark["event"] for mark in runner.marks(0, 32503680000000)}, {"injected", "workload-ready"})
        self.assertIn(READY_ANNOTATION, kube.current["metadata"]["annotations"])
        kube.pods = []
        restarted = Runner(kube, {**PRESETS, case: preset})
        self.assertEqual(restarted.status(case)["state"], "recovered")

    def test_initial_all_recovered_does_not_allow_next_run(self):
        obj = experiment("pending")
        obj["status"]["conditions"] = [{"type": "AllRecovered", "status": "True"}]
        for record in obj["status"]["experiment"]["containerRecords"]:
            record.update(phase="Not Injected", events=[])
        runner = Runner(FakeKubernetes(obj), PRESETS)
        with self.assertRaises(APIError):
            runner.start()

    def test_target_success_does_not_count_as_source_injection(self):
        obj = experiment("pending")
        obj["status"]["experiment"]["containerRecords"][0]["events"] = []
        self.assertEqual(annotations(run_status(obj)), [])

    def test_recovery_still_pending_blocks_run(self):
        obj = experiment("recovering", "Stop")
        obj["status"]["conditions"] = [{"type": "AllRecovered", "status": "True"}]
        with self.assertRaises(APIError):
            Runner(FakeKubernetes(obj), PRESETS).start()

    def test_controls_block_all_presets_while_a_run_is_active(self):
        case = "network-bandwidth"
        preset = json.loads((Path(__file__).parent.parent / f"scenarios/{case}.json").read_text())
        obj = experiment("run-1")
        obj["status"]["conditions"].append({"type": "AllInjected", "status": "True"})
        runner = Runner(FakeKubernetes(obj), {**PRESETS, case: preset})
        selected = runner.controls("network-delay")
        self.assertFalse(selected["canRun"])
        self.assertTrue(selected["canPause"])
        self.assertTrue(selected["canStop"])
        self.assertFalse(selected["canCancel"])
        other = runner.controls(case)
        self.assertEqual(other["activeCase"], "network-delay")
        self.assertFalse(other["canRun"])
        self.assertFalse(other["canStop"])
        obj["status"]["experiment"]["desiredPhase"] = "Stop"
        self.assertFalse(runner.controls("network-delay")["canStop"])
        self.assertFalse(runner.controls(case)["canRun"])

    def test_pending_partial_injection_can_be_cancelled(self):
        obj = experiment("partial")
        obj["status"]["experiment"]["containerRecords"][1].update(phase="Not Injected", events=[])
        kube = FakeKubernetes(obj)
        runner = Runner(kube, PRESETS)
        controls = runner.controls("network-delay")
        self.assertEqual(controls["state"], "pending")
        self.assertTrue(controls["canCancel"])
        self.assertFalse(controls["canStop"])
        self.assertEqual(runner.stop("network-delay", "partial", cancel=True)["state"], "recovering")
        controls = runner.controls("network-delay")
        self.assertFalse(controls["canCancel"])
        self.assertFalse(controls["canRun"])
        self.assertEqual(kube.current["metadata"]["annotations"][PAUSE_ANNOTATION], "true")

    def test_one_shot_cancel_only_before_successful_apply(self):
        case = "pod-pod-kill"
        preset = json.loads((Path(__file__).parent.parent / "scenarios" / f"{case}.json").read_text())
        kube = FakeKubernetes(experiment("applied", preset=preset))
        runner = Runner(kube, {case: preset})
        self.assertFalse(runner.controls(case)["canCancel"])
        with self.assertRaises(APIError):
            runner.stop(case, "applied", cancel=True)

        kube.current = experiment("pending", preset=preset)
        kube.current["status"]["experiment"]["containerRecords"] = []
        self.assertTrue(runner.controls(case)["canCancel"])
        with self.assertRaises(APIError):
            runner.stop(case, "applied", cancel=True)
        runner.stop(case, "pending", cancel=True)
        self.assertEqual(kube.deleted, ["pending"])
        self.assertTrue(runner.controls(case)["canRun"])

    def test_stop_ends_a_pause_in_progress_or_already_recovered(self):
        for recovery_complete in (False, True):
            with self.subTest(recovery_complete=recovery_complete):
                obj = experiment("race")
                obj["status"]["conditions"].append({"type": "AllInjected", "status": "True"})
                kube = FakeKubernetes(obj)
                runner = Runner(kube, PRESETS)
                runner.stop("network-delay", "race", resumable=True)

                def complete_recovery():
                    old_annotations = kube.current["metadata"]["annotations"]
                    kube.current = experiment("race", "Stop", True)
                    kube.current["metadata"]["annotations"] = old_annotations

                if recovery_complete:
                    complete_recovery()
                    self.assertTrue(runner.controls("network-delay")["canStart"])

                self.assertTrue(runner.stop("network-delay", "race")["success"])
                if not recovery_complete:
                    complete_recovery()
                self.assertEqual(runner.controls("network-delay")["state"], "recovered")
                self.assertFalse(runner.controls("network-delay")["canStart"])

    def test_start_continues_the_same_paused_run_and_reserves_the_slot(self):
        obj = experiment("run-1")
        obj["status"]["conditions"].append({"type": "AllInjected", "status": "True"})
        kube = FakeKubernetes(obj)
        runner = Runner(kube, PRESETS)
        runner.stop("network-delay", "run-1", resumable=True)
        self.assertFalse(runner.controls("network-delay")["canRun"])

        metadata_annotations = kube.current["metadata"]["annotations"]
        kube.current = experiment("run-1", "Stop", True)
        kube.current["metadata"]["annotations"] = metadata_annotations
        self.assertTrue(runner.controls("network-delay")["canStart"])
        with self.assertRaises(APIError) as stale:
            runner.resume("network-delay", "older-run")
        self.assertEqual(stale.exception.status, 409)
        result = runner.resume("network-delay", "run-1")
        self.assertEqual(result["runUID"], "run-1")
        self.assertEqual(kube.current["metadata"]["annotations"][PAUSE_ANNOTATION], "false")
        self.assertEqual(runner.controls("network-delay")["state"], "pending")
        self.assertFalse(runner.controls("network-delay")["canRun"])
        with self.assertRaises(APIError):
            runner.start("network-delay")
        kube.current["status"]["experiment"]["desiredPhase"] = "Run"
        for record in kube.current["status"]["experiment"]["containerRecords"]:
            record.update(phase="Injected", injectedCount=1)
        kube.current["status"]["conditions"].append({"type": "AllInjected", "status": "True"})
        self.assertTrue(runner.controls("network-delay")["canStop"])
        self.assertEqual(kube.deleted, [])

    def test_expired_or_stopped_run_cannot_be_started(self):
        obj = experiment("run-1", "Stop", True)
        obj["metadata"]["annotations"][PAUSE_ANNOTATION] = "true"
        obj["metadata"]["creationTimestamp"] = "2020-01-01T00:00:00Z"
        kube = FakeKubernetes(obj)
        runner = Runner(kube, PRESETS)
        self.assertFalse(runner.controls("network-delay")["canStart"])
        self.assertTrue(runner.controls("network-delay")["canRun"])
        with self.assertRaises(APIError):
            runner.resume("network-delay", "run-1")

        kube.current = experiment("run-2")
        runner.stop("network-delay", "run-2")
        records = kube.current["status"]["experiment"]
        records["desiredPhase"] = "Stop"
        for record in records["containerRecords"]:
            record["phase"] = "Not Injected"
        self.assertFalse(runner.controls("network-delay")["canStart"])

    def test_resumed_run_keeps_each_event_in_grafana(self):
        obj = experiment("run-1", "Stop", True)
        events = obj["status"]["experiment"]["containerRecords"][0]["events"]
        events.extend([
            {"type": "Succeeded", "operation": "Apply", "timestamp": "2026-11-14T01:01:00Z"},
            {"type": "Succeeded", "operation": "Recover", "timestamp": "2026-11-14T01:01:30Z"},
        ])
        lines = Runner(FakeKubernetes(obj), PRESETS).metrics().splitlines()
        self.assertEqual(len([line for line in lines if 'run_uid="run-1"' in line]), 4)
        self.assertTrue(any("1794618060000" in line and 'event="injected"' in line for line in lines))
        self.assertTrue(any("1794618090000" in line and 'event="recovered"' in line for line in lines))

    def test_stop_requests_pause_without_deleting_record(self):
        kube = FakeKubernetes(experiment("run-1"))
        result = Runner(kube, PRESETS).stop()
        self.assertEqual(result["state"], "recovering")
        self.assertEqual(kube.current["metadata"]["annotations"]["experiment.chaos-mesh.org/pause"], "true")
        self.assertEqual(kube.current["metadata"]["uid"], "run-1")
        self.assertEqual(kube.deleted, [])

    def test_http_contract_and_metrics_milliseconds(self):
        class TestHandler(Handler):
            runner = Runner(FakeKubernetes(experiment("run-1", "Stop", True)), PRESETS)
            def log_message(self, *args):
                pass
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), TestHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{httpd.server_port}"
        try:
            with urllib.request.urlopen(base + "/metrics") as response:
                body = response.read().decode()
                self.assertIn('playground_chaos_event_timestamp_milliseconds{run_uid="run-1",event="injected",scenario="network-delay"} 1794618000000', body)
                self.assertIn('event="recovered",scenario="network-delay"} 1794618030000', body)
            request = urllib.request.Request(base + "/run", b'{"duration":"1h"}', {"Content-Type": "application/json"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 400)
            request = urllib.request.Request(base + "/run", b'{"action":"delay"}', {"Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.status, 202)
                self.assertTrue(json.load(response)["success"])
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
