const form = document.querySelector("#scan-form");
const scanButton = document.querySelector("#scan-button");
const scanButtonLabel = document.querySelector("#scan-button-label");
const formError = document.querySelector("#form-error");
const readiness = document.querySelector("#readiness");
const resultsStatus = document.querySelector("#results-status");
const statusMessage = document.querySelector("#status-message");
const statusTime = document.querySelector("#status-time");
const signalsBody = document.querySelector("#signals-body");
const exportButton = document.querySelector("#export-button");
const directionFilter = document.querySelector("#direction-filter");
const allSignals = [];
let pollHandle = null;

function setReadiness(status) {
  readiness.classList.remove("is-ready", "is-error");
  const label = readiness.querySelector(".readiness-label");
  if (status.ready) {
    readiness.classList.add("is-ready");
    label.textContent = "READY TO SCAN";
  } else if (!status.credentials_configured) {
    readiness.classList.add("is-error");
    label.textContent = "ADD QUOTEX CREDENTIALS TO .ENV";
  } else {
    readiness.classList.add("is-error");
    label.textContent = "INSTALL PYQUOTEX TO SCAN";
  }
}

function updateStatus(state) {
  const text = state.error ? `Error: ${state.error}` : state.message;
  statusMessage.textContent = text || "Dashboard ready · configure a scan to begin";
  resultsStatus.classList.toggle("is-running", Boolean(state.running));
  resultsStatus.classList.toggle("is-error", state.status === "error");
  statusTime.textContent = state.updated_at ? new Date(state.updated_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  scanButton.disabled = Boolean(state.running);
  scanButtonLabel.textContent = state.running ? "Scanning market history…" : "Run market scan";
  if (state.running) startPolling();
  else stopPolling();
}

function updateMetrics(summary) {
  document.querySelector("#metric-signals").textContent = summary ? summary.signals : "—";
  document.querySelector("#metric-signals-caption").textContent = summary
    ? `${summary.pairs_with_data} of ${summary.pairs_scanned} pairs returned data`
    : "Awaiting first scan";
  document.querySelector("#metric-accuracy").textContent = summary?.average_test_accuracy == null
    ? "—"
    : `${summary.average_test_accuracy}%`;
  document.querySelector("#metric-breakeven").textContent = summary ? `${summary.breakeven}%` : "—";
}

function renderChart(signals, breakeven) {
  const chartData = document.querySelector("#chart-data");
  const emptyMessage = document.querySelector("#chart-empty");
  chartData.replaceChildren();
  const tested = signals.filter((signal) => Number.isFinite(Number(signal.test_accuracy)));
  emptyMessage.hidden = tested.length > 0;
  if (!tested.length) return;

  const namespace = "http://www.w3.org/2000/svg";
  const yFor = (value) => 128 - ((Math.min(100, Math.max(25, value)) - 25) / 75) * 108;
  const baseY = 128;
  const plotWidth = 700;
  const thresholdY = yFor(breakeven ?? 50);
  const line = document.createElementNS(namespace, "line");
  line.setAttribute("x1", "48");
  line.setAttribute("x2", "748");
  line.setAttribute("y1", thresholdY);
  line.setAttribute("y2", thresholdY);
  line.setAttribute("class", "breakeven-line");
  chartData.append(line);

  const thresholdLabel = document.createElementNS(namespace, "text");
  thresholdLabel.setAttribute("x", "746");
  thresholdLabel.setAttribute("y", Math.max(12, thresholdY - 4));
  thresholdLabel.setAttribute("text-anchor", "end");
  thresholdLabel.setAttribute("class", "breakeven-label");
  thresholdLabel.textContent = `BREAKEVEN ${breakeven}%`;
  chartData.append(thresholdLabel);

  tested.forEach((signal, index) => {
    const accuracy = Number(signal.test_accuracy);
    const x = tested.length === 1 ? 48 + plotWidth / 2 : 48 + (index / (tested.length - 1)) * plotWidth;
    const y = yFor(accuracy);
    const point = document.createElementNS(namespace, "circle");
    point.setAttribute("cx", x);
    point.setAttribute("cy", y);
    point.setAttribute("r", "5");
    point.setAttribute("class", `accuracy-point ${accuracy >= breakeven ? "above" : "below"}`);
    const title = document.createElementNS(namespace, "title");
    title.textContent = `${signal.pair} ${signal.direction}: ${accuracy.toFixed(1)}% test accuracy (${signal.test_samples} samples)`;
    point.append(title);
    chartData.append(point);

    const label = document.createElementNS(namespace, "text");
    label.setAttribute("x", x);
    label.setAttribute("y", baseY + 18);
    label.setAttribute("text-anchor", "middle");
    label.setAttribute("class", "chart-tick");
    label.textContent = signal.pair.length > 9 ? signal.pair.slice(0, 8) : signal.pair;
    chartData.append(label);
  });
}

function makeCell(text, className) {
  const cell = document.createElement("td");
  if (className) cell.className = className;
  cell.textContent = text ?? "—";
  return cell;
}

function renderSignals(signals) {
  allSignals.splice(0, allSignals.length, ...signals);
  const direction = directionFilter.value;
  const visible = signals.filter((signal) => direction === "all" || signal.direction === direction);
  signalsBody.replaceChildren();
  document.querySelector("#result-count").textContent = String(signals.length);
  exportButton.disabled = signals.length === 0;

  if (!visible.length) {
    const row = document.createElement("tr");
    row.className = "empty-row";
    const cell = document.createElement("td");
    cell.colSpan = 6;
    cell.textContent = signals.length ? "No signals match this direction" : "No scan results yet";
    row.append(cell);
    signalsBody.append(row);
    return;
  }

  for (const signal of visible) {
    const row = document.createElement("tr");
    const time = String(signal.time_pkt || "").replace(" PKT", "");
    row.append(makeCell(time, "time-cell"));
    const pair = makeCell(signal.pair, "pair-name");
    row.append(pair);
    const directionCell = document.createElement("td");
    const chip = document.createElement("span");
    chip.className = `direction-chip ${signal.direction === "BUY" ? "buy" : "sell"}`;
    chip.textContent = signal.direction;
    directionCell.append(chip);
    row.append(directionCell);
    row.append(makeCell(signal.test_accuracy_str, "accuracy-cell"));
    row.append(makeCell(signal.test_samples, "sample-cell"));
    const patternCell = document.createElement("td");
    const tag = document.createElement("span");
    tag.className = "pattern-tag";
    tag.textContent = signal.pattern;
    patternCell.append(tag);
    row.append(patternCell);
    signalsBody.append(row);
  }
}

function csvCell(value) {
  return `"${String(value ?? "").replaceAll('"', '""')}"`;
}

function exportCsv() {
  if (!allSignals.length) return;
  const columns = ["Date", "Time (PKT)", "Pair", "Direction", "Test Accuracy", "Test Samples", "Train Confidence", "Full Accuracy", "Full Samples", "Pattern", "Reason"];
  const rows = allSignals.map((signal) => [
    signal.date,
    signal.time_pkt,
    signal.pair,
    signal.direction,
    signal.test_accuracy_str,
    signal.test_samples,
    signal.confidence_str,
    signal.full_accuracy_str,
    signal.full_samples,
    signal.pattern,
    signal.reason,
  ]);
  const content = [columns, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n");
  const url = URL.createObjectURL(new Blob([content], { type: "text/csv;charset=utf-8" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = `quotex-signals-${new Date().toISOString().slice(0, 10)}.csv`;
  link.click();
  URL.revokeObjectURL(url);
}

async function refreshState() {
  try {
    const response = await fetch("/api/status", { cache: "no-store" });
    if (!response.ok) throw new Error("Dashboard server did not respond.");
    const state = await response.json();
    setReadiness(state);
    updateStatus(state);
    if (state.summary) updateMetrics(state.summary);
    if (state.signals) renderSignals(state.signals);
    renderChart(state.signals || [], state.summary?.breakeven);
  } catch (error) {
    readiness.classList.add("is-error");
    readiness.querySelector(".readiness-label").textContent = "SERVER CONNECTION LOST";
    statusMessage.textContent = error.message;
    resultsStatus.classList.add("is-error");
  }
}

function startPolling() {
  if (pollHandle) return;
  pollHandle = window.setInterval(refreshState, 1200);
}

function stopPolling() {
  if (!pollHandle) return;
  window.clearInterval(pollHandle);
  pollHandle = null;
}

document.querySelector("#train-split").addEventListener("input", (event) => {
  const train = Number(event.currentTarget.value);
  document.querySelector("#split-value").textContent = `${train} / ${100 - train}`;
});

directionFilter.addEventListener("change", () => renderSignals(allSignals));
exportButton.addEventListener("click", exportCsv);

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  formError.hidden = true;
  const values = Object.fromEntries(new FormData(form));
  values.history_days = Number(values.history_days);
  values.max_signals = Number(values.max_signals);
  values.min_accuracy = Number(values.min_accuracy);
  values.payout_pct = Number(values.payout_pct);
  values.train_ratio = Number(values.train_ratio) / 100;

  try {
    const response = await fetch("/api/scan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(values),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Unable to start scan.");
    if (result.state) updateStatus(result.state);
    startPolling();
    await refreshState();
  } catch (error) {
    formError.textContent = error.message;
    formError.hidden = false;
  }
});

refreshState();