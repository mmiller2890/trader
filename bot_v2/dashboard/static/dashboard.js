(() => {
  "use strict";

  const operatorToken = document.body.dataset.operatorToken;
  const byId = (id) => document.getElementById(id);
  let currentState = null;
  let recentEvents = [];
  let polling = false;
  let toastTimer = null;
  let pendingConfirmation = null;
  let lastFocusedControl = null;

  const setText = (id, value) => {
    const element = byId(id);
    if (element) element.textContent = value ?? "—";
  };

  const formatNumber = (value, digits = 4) => {
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) return "—";
    return parsed.toLocaleString(undefined, { maximumFractionDigits: digits });
  };

  const formatTime = (value) => {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.valueOf()) ? "—" : date.toLocaleTimeString();
  };

  const showToast = (message) => {
    const toast = byId("toast");
    toast.textContent = message;
    toast.hidden = false;
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => { toast.hidden = true; }, 5000);
  };

  const request = async (path, options = {}) => {
    const method = options.method || "GET";
    const headers = new Headers(options.headers || {});
    if (method !== "GET") {
      headers.set("X-Operator-Token", operatorToken);
      if (options.body) headers.set("Content-Type", "application/json");
    }
    const response = await fetch(path, { ...options, method, headers });
    let payload = null;
    try { payload = await response.json(); } catch (_error) { payload = null; }
    if (!response.ok) {
      const detail = payload && payload.detail ? payload.detail : `Request failed (${response.status})`;
      throw new Error(detail);
    }
    return payload;
  };

  const cell = (value, className = "") => {
    const element = document.createElement("td");
    element.textContent = value ?? "—";
    if (className) element.className = className;
    return element;
  };

  const renderHeartbeats = (heartbeats) => {
    const grid = byId("heartbeat-grid");
    const cards = heartbeats.map((heartbeat) => {
      const card = document.createElement("article");
      card.className = "metric-card";
      const label = document.createElement("span");
      label.textContent = heartbeat.component.replaceAll("_", " ");
      const value = document.createElement("strong");
      value.textContent = heartbeat.state.toUpperCase();
      value.className = heartbeat.state === "fresh" ? "positive" : "negative";
      const detail = document.createElement("small");
      detail.textContent = `${formatNumber(heartbeat.age_seconds, 1)}s ago · ${formatTime(heartbeat.recorded_at)}`;
      card.append(label, value, detail);
      return card;
    });
    if (!cards.length) {
      const card = document.createElement("article");
      card.className = "metric-card";
      const label = document.createElement("span");
      label.textContent = "Heartbeats";
      const value = document.createElement("strong");
      value.textContent = "MISSING";
      value.className = "negative";
      card.append(label, value);
      cards.push(card);
    }
    grid.replaceChildren(...cards);
  };

  const renderOperationalHealth = (state) => {
    const badge = byId("ops-state-badge");
    const phase = state.runtime.phase;
    const labels = {
      running: "Running",
      degraded: "Degraded",
      halting: "Halting",
      halted: "Halted",
      failed: "Failed",
      starting: "Starting",
      stopping: "Stopping",
      stopped: "Stopped",
    };
    badge.textContent = `${labels[phase] || phase} · ${state.market_data_source || "unavailable"}`;
    badge.className = `ops-badge ops-badge-${phase || "unknown"}`;

    const fallback = byId("ops-rest-fallback");
    const onFallback = state.market_data_source === "rest_fallback";
    fallback.textContent = onFallback ? "REST fallback active" : "REST fallback off";
    fallback.classList.toggle("count-pill-warn", onFallback);

    const backlog = byId("ops-telegram-backlog");
    const pending = Number(state.outbox_pending || 0);
    backlog.textContent = `Telegram backlog ${pending}`;
    const oldestAge = state.oldest_outbox_age_seconds;
    backlog.classList.toggle(
      "count-pill-warn",
      pending > 0 && oldestAge !== null && oldestAge >= 120,
    );

    const leaseWarning = byId("ops-lease-warning");
    const remaining = state.lease_remaining_seconds;
    if (remaining !== null && remaining !== undefined) {
      leaseWarning.hidden = false;
      const hours = remaining / 3600;
      leaseWarning.textContent = hours <= 1
        ? "Lease expiring within 1 hour"
        : hours <= 24
          ? `Lease expiring in ${Math.floor(hours)}h`
          : `Lease ${Math.floor(hours)}h remaining`;
      leaseWarning.classList.toggle("count-pill-warn", hours <= 24);
      leaseWarning.classList.toggle("count-pill-bad", hours <= 1);
    } else {
      leaseWarning.hidden = true;
      leaseWarning.textContent = "";
    }

    const body = byId("ops-task-health-body");
    const tasks = Array.isArray(state.task_health) ? state.task_health : [];
    const rows = tasks.map((task) => {
      const row = document.createElement("tr");
      const name = document.createElement("td");
      name.textContent = task.name;
      const statusCell = document.createElement("td");
      statusCell.textContent = task.running ? "healthy" : "down";
      statusCell.className = task.running ? "positive" : "negative";
      const restarts = document.createElement("td");
      restarts.textContent = String(task.restart_count ?? 0);
      const failures = document.createElement("td");
      failures.textContent = String(task.consecutive_failures ?? 0);
      row.append(name, statusCell, restarts, failures);
      return row;
    });
    if (!rows.length) {
      const row = document.createElement("tr");
      const cell = document.createElement("td");
      cell.colSpan = 4;
      cell.textContent = "No supervised tasks reported.";
      row.append(cell);
      rows.push(row);
    }
    body.replaceChildren(...rows);
  };

  const renderReadiness = (items) => {
    const list = byId("readiness-list");
    const rows = items.map((item) => {
      const row = document.createElement("li");
      row.className = `readiness-item${item.passed ? "" : " failed"}`;
      const icon = document.createElement("span");
      icon.className = "readiness-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = item.passed ? "✓" : "×";
      const copy = document.createElement("div");
      const name = document.createElement("span");
      name.className = "readiness-name";
      name.textContent = item.name.replaceAll("_", " ");
      const reason = document.createElement("span");
      reason.className = "readiness-reason";
      reason.textContent = item.reason;
      copy.append(name, reason);
      row.append(icon, copy);
      return row;
    });
    list.replaceChildren(...rows);
    const passed = items.filter((item) => item.passed).length;
    setText("readiness-count", `${passed}/${items.length} passed`);
  };

  const renderPreflightChecks = (checks) => {
    const rows = (checks || []).map((check) => {
      const row = document.createElement("li");
      row.className = `preflight-check${check.passed ? "" : " failed"}`;
      const status = document.createElement("span");
      status.className = "preflight-check-status";
      status.textContent = check.passed ? "PASS" : "BLOCKED";
      const copy = document.createElement("div");
      const name = document.createElement("strong");
      name.textContent = check.name.replaceAll("_", " ");
      const reason = document.createElement("span");
      reason.textContent = check.reason;
      copy.append(name, reason);
      row.append(status, copy);
      return row;
    });
    byId("preflight-checks").replaceChildren(...rows);
  };

  const renderOrders = (orders) => {
    const rows = orders.map((order) => {
      const row = document.createElement("tr");
      row.append(
        cell(order.status),
        cell(order.side || "—"),
        cell(`${order.market_id || "—"}\n${order.token_id || "—"}`, "mono"),
        cell(formatNumber(order.requested_size), "mono"),
        cell(formatNumber(order.filled_size), "mono"),
        cell(order.exchange_order_id || order.client_order_id, "mono"),
      );
      return row;
    });
    byId("orders-body").replaceChildren(...rows);
    byId("orders-empty").hidden = rows.length > 0;
    setText("orders-count", String(rows.length));
  };

  const renderPositions = (positions) => {
    const rows = positions.map((position) => {
      const row = document.createElement("tr");
      row.append(
        cell(`${position.market_id}\n${position.token_id}`, "mono"),
        cell(formatNumber(position.quantity), "mono"),
        cell(formatNumber(position.average_entry_price), "mono"),
        cell(formatNumber(position.mark_price), "mono"),
        cell(formatNumber(position.unrealized_pnl), Number(position.unrealized_pnl) < 0 ? "negative mono" : "positive mono"),
        cell(formatNumber(position.realized_pnl), Number(position.realized_pnl) < 0 ? "negative mono" : "positive mono"),
      );
      return row;
    });
    byId("positions-body").replaceChildren(...rows);
    byId("positions-empty").hidden = rows.length > 0;
    setText("positions-count", String(rows.length));
  };

  const exitStatusLabel = (managed) => {
    if (managed.dust) return "Dust";
    if (managed.exit_pending) return "Exit pending";
    if (managed.confirmation_deferred) return "Awaiting account confirmation";
    if (managed.exit_reason) return `Exit: ${managed.exit_reason}`;
    return "Monitoring";
  };

  const renderManagedPositions = (managedPositions) => {
    const rows = managedPositions.map((managed) => {
      const position = managed.position;
      const row = document.createElement("tr");
      const returnCell = cell(
        managed.return_bps === null || managed.return_bps === undefined
          ? "—"
          : `${formatNumber(managed.return_bps, 1)} bps`,
        Number(managed.return_bps) < 0 ? "negative mono" : "positive mono",
      );
      const heldCell = cell(
        managed.held_seconds === null || managed.held_seconds === undefined
          ? "—"
          : `${formatNumber(managed.held_seconds, 0)}s`,
        "mono",
      );
      const deadlineCell = cell(
        managed.market_end_at ? formatTime(managed.market_end_at) : "—",
        "mono",
      );
      const exitCell = cell(exitStatusLabel(managed));
      row.append(
        cell(`${position.market_id}\n${position.token_id}`, "mono"),
        cell(formatNumber(position.quantity), "mono"),
        cell(formatNumber(position.average_entry_price), "mono"),
        cell(formatNumber(position.mark_price), "mono"),
        returnCell,
        heldCell,
        deadlineCell,
        exitCell,
      );
      return row;
    });
    byId("managed-positions-body").replaceChildren(...rows);
    byId("positions-empty").hidden = rows.length > 0;
    setText("positions-count", String(rows.length));
  };

  const renderClosedPositions = (closedPositions) => {
    const rows = closedPositions.map((closed) => {
      const row = document.createElement("tr");
      row.append(
        cell(`${closed.market_id}\n${closed.token_id}`, "mono"),
        cell(formatTime(closed.opened_at), "mono"),
        cell(formatTime(closed.closed_at), "mono"),
        cell(formatNumber(closed.closed_exit_price), "mono"),
        cell(formatNumber(closed.closed_realized_pnl), Number(closed.closed_realized_pnl) < 0 ? "negative mono" : "positive mono"),
        cell(closed.last_exit_reason || "Closed"),
      );
      return row;
    });
    byId("closed-positions-body").replaceChildren(...rows);
    byId("closed-positions-empty").hidden = rows.length > 0;
  };

  const renderMarketRotation = (rotation) => {
    const state = rotation || {
      enabled: false,
      state: "disabled",
      reason: "automatic_market_disabled",
    };
    setText("market-rotation-state", state.state.toUpperCase());
    setText("market-rotation-title-value", state.title || (state.enabled ? "Waiting for market discovery." : "Automatic discovery is disabled."));
    setText("market-rotation-slug", state.slug);
    const windowLabel = state.start_at && state.end_at
      ? `${new Date(state.start_at).toISOString()} → ${new Date(state.end_at).toISOString()}`
      : "—";
    setText("market-rotation-window", windowLabel);
    setText("market-rotation-up", state.up_token_id);
    setText("market-rotation-down", state.down_token_id);
    setText("market-rotation-reason", `${state.reason} · Token IDs rotate automatically every 15 minutes. Dry run does not use trading credentials.`);
  };

  const renderEvents = () => {
    const filter = byId("event-filter").value;
    const selected = recentEvents.filter((event) => filter === "all" || event.event_type === filter);
    const rows = selected.map((event) => {
      const row = document.createElement("li");
      row.className = "event-item";
      const time = document.createElement("time");
      time.className = "event-time";
      time.dateTime = event.created_at;
      time.textContent = formatTime(event.created_at);
      const copy = document.createElement("div");
      const type = document.createElement("span");
      type.className = "event-type";
      type.textContent = event.event_type;
      const message = document.createElement("span");
      message.className = "event-message";
      message.textContent = `${event.component} · ${event.message}`;
      copy.append(type, message);
      if (event.reason) {
        const reason = document.createElement("span");
        reason.className = "event-reason";
        reason.textContent = event.reason;
        copy.append(reason);
      }
      row.append(time, copy);
      return row;
    });
    byId("events-list").replaceChildren(...rows);
    byId("events-empty").hidden = rows.length > 0;
  };

  const renderState = (state) => {
    currentState = state;
    setText("mode-value", state.mode.toUpperCase());
    setText("phase-value", state.runtime.phase.toUpperCase());
    setText("kill-value", state.kill_switch ? "ACTIVE" : "INACTIVE");
    setText("source-value", state.source.toUpperCase());
    setText("updated-value", formatTime(state.generated_at));
    setText("websocket-state", state.websocket_connected ? "WebSocket active" : "WebSocket inactive");
    setText("metric-orders", String(state.open_orders.length));
    setText("metric-positions", String(state.positions.length));
    setText("metric-exposure", formatNumber(state.total_exposure));
    setText("metric-pnl", formatNumber(state.total_pnl));
    if (state.preflight) {
      const preflightLabel = state.preflight.status === "not_run"
        ? "Preflight has not run in this dashboard session."
        : `${state.preflight.status.toUpperCase()} · ${state.preflight.reason}`;
      setText("preflight-result", preflightLabel);
      renderPreflightChecks(state.preflight.checks);
    }

    const running = state.runtime.phase === "running" || state.runtime.phase === "halted";
    const stopped = state.runtime.phase === "stopped" || state.runtime.phase === "failed";
    const preflightExpired = Boolean(
      state.preflight
      && state.preflight.status === "passed"
      && !state.preflight_fresh
      && state.preflight_expires_at,
    );
    const automaticScope = Boolean(state.market_rotation && state.market_rotation.enabled);
    const liveMode = state.mode === "live";
    const startButton = byId("start-button");
    startButton.disabled = !stopped || (liveMode && !state.live_start_ready);
    startButton.classList.toggle("button-primary", !liveMode);
    startButton.classList.toggle("button-danger", liveMode);
    setText("start-button-label", liveMode ? "Start live bot" : "Start dry run");
    byId("stop-button").disabled = !running;
    byId("preflight-button").disabled = !stopped;
    byId("enable-live-button").disabled = !stopped || liveMode || !state.preflight_fresh;
    byId("dry-run-button").disabled = !stopped || !liveMode;
    byId("halt-button").disabled = !running || state.runtime.phase === "halted";
    byId("cancel-button").disabled = !running || !liveMode;
    byId("clear-halt-button").disabled =
      !(state.runtime.phase === "halted" || state.runtime.phase === "failed")
      || !state.active_halt_incident_suffix;
    // The config overlay is only writable while stopped, so the toggle has to
    // follow the same rule or it would silently fail. The test button does
    // not: it needs to work right before a live start, which is when the bot
    // is stopped anyway, and after one too.
    byId("telegram-enabled").disabled = !stopped;
    byId("save-config-button").disabled = !stopped || automaticScope;
    byId("subscribed-tokens").disabled = !stopped || automaticScope;
    byId("target-tokens").disabled = !stopped || automaticScope;
    setText(
      "config-scope-help",
      automaticScope
        ? "automatic_market_owns_token_scope · IDs are discovered and rotated by the bot."
        : "Only these lists are editable while stopped. Live flags, sizing, risk, and credentials stay locked.",
    );
    setText(
      "control-message",
      state.kill_switch
        ? `Trading halted: ${state.kill_switch_reason || "kill switch active"}.`
        : state.runtime.reason || (running ? "Bot runtime is active." : "Bot is stopped."),
    );
    setText(
      "live-warning-title",
      liveMode
        ? (state.live_start_ready
          ? "Live mode armed."
          : preflightExpired
            ? "Preflight expired."
            : "Live start is blocked.")
        : (state.preflight_fresh ? "Preflight passed." : "Live mode requires preflight."),
    );
    setText(
      "live-warning-copy",
      liveMode
        ? (state.live_start_ready
          ? "Starting requires the exact START LIVE confirmation and repeats every safety check."
          : preflightExpired
            ? `The last preflight passed but expired at ${formatTime(state.preflight_expires_at)}. Run it again before restarting live trading.`
            : "Run preflight again before live start.")
        : (state.preflight_fresh
          ? "You may enable live mode with the exact ENABLE LIVE confirmation."
          : "Run the read-only checks before enabling live mode."),
    );

    const connection = byId("connection-state");
    connection.replaceChildren();
    const dot = document.createElement("span");
    dot.className = `status-dot ${state.source === "live" ? "status-good" : "status-warn"}`;
    dot.setAttribute("aria-hidden", "true");
    const label = document.createElement("span");
    label.textContent = state.source === "live" ? "Live runtime state" : "Historical state";
    connection.append(dot, label);

    renderHeartbeats(state.heartbeats);
    renderOperationalHealth(state);
    renderMarketRotation(state.market_rotation);
    renderReadiness(state.readiness);
    renderOrders(state.open_orders);
    renderManagedPositions(state.managed_positions || []);
    renderClosedPositions(state.closed_positions || []);
  };

  const loadConfig = async () => {
    const config = await request("/api/config");
    byId("subscribed-tokens").value = config.subscribed_token_ids.join("\n");
    byId("target-tokens").value = config.target_token_ids.join("\n");
    byId("telegram-enabled").checked = Boolean(config.telegram_enabled);
  };

  const loadEvents = async () => {
    const result = await request("/api/events?limit=100");
    recentEvents = result.events || [];
    renderEvents();
  };

  const poll = async () => {
    if (polling) return;
    polling = true;
    try {
      const state = await request("/api/state");
      renderState(state);
      await loadEvents();
    } catch (error) {
      const connection = byId("connection-state");
      connection.replaceChildren();
      const dot = document.createElement("span");
      dot.className = "status-dot status-bad";
      const label = document.createElement("span");
      label.textContent = "Dashboard API unavailable";
      connection.append(dot, label);
    } finally {
      polling = false;
    }
  };

  const runAction = async (path, options = {}) => {
    try {
      await request(path, { method: "POST", ...options });
      await poll();
      showToast("Action completed.");
    } catch (error) {
      showToast(error.message);
    }
  };

  const openConfirmation = (kind, phrase, path, method = "POST", payload = {}) => {
    const dialog = byId("confirm-dialog");
    lastFocusedControl = document.activeElement;
    pendingConfirmation = { kind, phrase, path, method, payload };
    setText("confirm-title", kind);
    setText("confirm-description", `Enter ${phrase} exactly to continue.`);
    byId("confirmation-input").value = "";
    dialog.showModal();
    byId("confirmation-input").focus();
  };

  byId("start-button").addEventListener("click", () => {
    if (currentState && currentState.mode === "live") {
      openConfirmation("Start live bot", "START LIVE", "/api/control/start");
      return;
    }
    runAction("/api/control/start");
  });
  byId("stop-button").addEventListener("click", () => runAction("/api/control/stop"));
  byId("enable-live-button").addEventListener("click", () => {
    openConfirmation(
      "Enable live mode",
      "ENABLE LIVE",
      "/api/mode",
      "PUT",
      { mode: "live" },
    );
  });
  byId("dry-run-button").addEventListener("click", () => runAction(
    "/api/mode",
    { method: "PUT", body: JSON.stringify({ mode: "dry_run" }) },
  ));
  byId("preflight-button").addEventListener("click", async () => {
    setText("preflight-result", "Preflight running…");
    try {
      const result = await request("/api/preflight", { method: "POST" });
      setText("preflight-result", `${result.status.toUpperCase()} · ${result.reason}`);
      await poll();
      showToast(result.ok ? "Preflight passed." : "Preflight found blockers.");
    } catch (error) {
      setText("preflight-result", error.message);
      showToast(error.message);
    }
  });
  byId("telegram-test-button").addEventListener("click", async () => {
    setText("telegram-message", "Sending Telegram test…");
    try {
      // The confirmation phrase is fixed by the controller, not typed here:
      // sending a test alert to your own channel is not a destructive action.
      const result = await request("/api/notifications/test", {
        method: "POST",
        body: JSON.stringify({ confirmation: "SEND TEST" }),
      });
      const delivered = result.ok;
      setText(
        "telegram-message",
        delivered
          ? "Telegram test delivered. Live start is unlocked for 5 minutes."
          : `Telegram test not delivered: ${result.reason}.`,
      );
      await poll();
      showToast(delivered ? "Telegram test delivered." : `Not delivered: ${result.reason}`);
    } catch (error) {
      setText("telegram-message", error.message);
      showToast(error.message);
    }
  });
  byId("telegram-enabled").addEventListener("change", async (event) => {
    const enabled = event.target.checked;
    try {
      const current = await request("/api/config");
      await request("/api/config", {
        method: "PUT",
        body: JSON.stringify({ ...current, telegram_enabled: enabled }),
      });
      showToast(`Telegram alerts ${enabled ? "enabled" : "disabled"}. Restart to apply.`);
    } catch (error) {
      // Put the checkbox back so it never claims a state that was not saved.
      event.target.checked = !enabled;
      showToast(error.message);
    }
  });
  byId("halt-button").addEventListener("click", () => openConfirmation("Emergency halt", "HALT", "/api/control/halt"));
  byId("cancel-button").addEventListener("click", () => openConfirmation("Cancel all orders", "CANCEL ALL", "/api/control/cancel-all"));
  byId("clear-halt-button").addEventListener("click", () => {
    const suffix = currentState && currentState.active_halt_incident_suffix;
    if (!suffix) {
      showToast("No active halt incident is available to clear.");
      return;
    }
    openConfirmation(
      "Clear halt",
      `CLEAR HALT ${suffix}`,
      "/api/control/clear-halt",
      "POST",
      { incident_id_suffix: suffix },
    );
  });
  byId("event-filter").addEventListener("change", renderEvents);

  byId("confirm-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const dialog = byId("confirm-dialog");
    const submitted = event.submitter && event.submitter.value;
    if (submitted === "confirm" && pendingConfirmation) {
      const pending = pendingConfirmation;
      const confirmation = byId("confirmation-input").value;
      if (confirmation !== pending.phrase) {
        showToast(`Enter ${pending.phrase} exactly.`);
        return;
      }
      dialog.close();
      await runAction(pending.path, {
        method: pending.method,
        body: JSON.stringify({ ...pending.payload, confirmation }),
      });
    } else {
      dialog.close();
    }
  });
  byId("confirm-dialog").addEventListener("close", () => {
    pendingConfirmation = null;
    if (lastFocusedControl) lastFocusedControl.focus();
  });

  byId("config-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const parseTokens = (id) => byId(id).value.split(/\s+/).map((item) => item.trim()).filter(Boolean);
    const payload = {
      subscribed_token_ids: parseTokens("subscribed-tokens"),
      target_token_ids: parseTokens("target-tokens"),
      // Carry the current switch through: the overlay is written whole, so
      // omitting this would silently turn Telegram off on every scope save.
      telegram_enabled: byId("telegram-enabled").checked,
    };
    if ([...payload.subscribed_token_ids, ...payload.target_token_ids].some((item) => !/^\d+$/.test(item))) {
      showToast("Token IDs must contain decimal digits only.");
      return;
    }
    try {
      await request("/api/config", { method: "PUT", body: JSON.stringify(payload) });
      await loadConfig();
      await poll();
      showToast("Market scope saved.");
    } catch (error) {
      showToast(error.message);
    }
  });

  loadConfig().catch((error) => showToast(error.message));
  poll();
  window.setInterval(poll, 1000);
})();
