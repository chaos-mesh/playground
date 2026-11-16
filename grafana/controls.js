const selectedCase = new URLSearchParams(location.search).get("case");
const buttons = Array.from(document.querySelectorAll("[data-action]"));
const status = document.getElementById("status");

const actions = {
    run: "canRun",
    cancel: "canCancel",
    start: "canStart",
    pause: "canPause",
    stop: "canStop",
};
const stateLabels = {
    idle: "Ready",
    pending: "Starting experiment…",
    running: "Experiment running",
    recovering: "Recovering…",
    "waiting-for-workload": "Waiting for workload readiness…",
    paused: "Paused. Start is available until the experiment expires.",
    recovered: "Experiment recovered",
    stopped: "Experiment ended",
};

let snapshot;
let actionError = "";
let refreshError = "";
let working = false;
let generation = 0;

function render() {
    for (const button of buttons) {
        const action = button.dataset.action;
        const available = !refreshError && Boolean(snapshot?.[actions[action]]);
        button.disabled = working || !available;
        button.hidden = action !== "run" && !available;
    }

    const activeElsewhere = snapshot?.activeCase && snapshot.activeCase !== selectedCase;
    const label = activeElsewhere
        ? `Active experiment: ${snapshot.activeTitle}.`
        : stateLabels[snapshot?.state];
    const message = refreshError || actionError || (working ? "Updating experiment…" : label) || "Checking experiment status…";
    if (status.textContent !== message) status.textContent = message;
}

async function request(path, body) {
    const headers = {"X-Grafana-Action": "1"};
    const options = {headers};
    if (body) {
        headers["Content-Type"] = "application/json";
        options.method = "POST";
        options.body = JSON.stringify(body);
    }
    const response = await fetch("/playground-api/" + path, options);
    const data = response.headers.get("Content-Type")?.includes("application/json")
        ? await response.json()
        : null;
    if (!response.ok) {
        throw new Error(data?.error || "Experiment controls are unavailable.");
    }
    return data;
}

async function refresh() {
    const turn = generation;
    try {
        const data = await request("controls?" + new URLSearchParams({case: selectedCase}));
        if (turn !== generation) return;
        if (["state", "runUID", "activeCase"].some(key => snapshot?.[key] !== data[key])) {
            actionError = "";
        }
        snapshot = data;
        refreshError = "";
    } catch (problem) {
        if (turn !== generation) return;
        refreshError = problem.message;
    }
    render();
}

function showLiveRange() {
    if (window.parent === window) return false;

    const url = new URL(window.parent.location.href);
    const live = url.searchParams.get("to") === "now";
    const refresh = url.searchParams.get("refresh");
    if (live && refresh && refresh !== "off") return false;

    if (!live) {
        url.searchParams.set("from", "now-5m");
        url.searchParams.set("to", "now");
    }
    url.searchParams.set("refresh", "5s");
    window.parent.location.replace(url);
    return true;
}

async function perform(button) {
    if (working || button.disabled || button.hidden) return;
    const action = button.dataset.action;
    const body = {case: selectedCase};
    if (action !== "run") body.runUID = snapshot.runUID;

    working = true;
    generation++;
    actionError = "";
    render();

    let failure = "";
    try {
        await request(action, body);
        if ((action === "run" || action === "start") && showLiveRange()) return;
    } catch (problem) {
        failure = problem.message;
    }
    await refresh();
    working = false;
    actionError = failure;
    render();
}

for (const button of buttons) {
    button.addEventListener("click", () => perform(button));
}

async function poll() {
    if (!working) await refresh();
    setTimeout(poll, 1500);
}

poll();
