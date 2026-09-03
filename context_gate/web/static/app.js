(() => {
  "use strict";

  const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;
  const OUTCOME_COLORS = {
    ALLOW: "#39e6a0",
    REVIEW: "#ffc857",
    BLOCK: "#ff667d",
    UNKNOWN: "#7890a3",
  };
  const LAYOUT_PRESETS = {
    "dashboard-focus": { railWidth: 176, chatWidth: 286 },
    balanced: { railWidth: 194, chatWidth: 316 },
    "chat-focus": { railWidth: 180, chatWidth: 430 },
  };

  const view = {
    state: null,
    health: null,
    activeFilter: "REVIEW",
    selectedCaseId: null,
    calendarMonth: null,
    selectedCalendarEventId: null,
    calendarSelectionInitialized: false,
    websiteSourceOperations: new Map(),
    autoMonitorTimer: null,
    autoMonitorRunning: false,
    autoMonitorLastResult: "",
    autoMonitorLastFailed: false,
  };

  const $ = (selector, root = document) => root.querySelector(selector);
  const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

  function element(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function safeText(value, fallback = "—") {
    if (value === undefined || value === null || value === "") return fallback;
    if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
      return String(value);
    }
    if (Array.isArray(value)) return value.map((item) => safeText(item, "")).filter(Boolean).join(", ");
    if (typeof value === "object") {
      return safeText(value.label ?? value.name ?? value.value ?? value.status, fallback);
    }
    return fallback;
  }

  function normalizeOutcome(value) {
    const candidate = typeof value === "object" && value !== null
      ? value.value ?? value.outcome ?? value.decision
      : value;
    const normalized = safeText(candidate, "UNKNOWN").toUpperCase();
    return ["ALLOW", "REVIEW", "BLOCK"].includes(normalized) ? normalized : "UNKNOWN";
  }

  function caseId(item) {
    return safeText(item?.case_id ?? item?.id ?? item?.caseId, "UNASSIGNED");
  }

  function caseTitle(item) {
    return safeText(item?.title ?? item?.name ?? item?.event_name ?? item?.subject, `Case ${caseId(item)}`);
  }

  function caseOriginalOutcome(item) {
    return normalizeOutcome(
      item?.deterministic_outcome
      ?? item?.original_outcome
      ?? item?.original_decision
      ?? item?.decision?.decision
      ?? item?.decision,
    );
  }

  function caseEffectiveOutcome(item) {
    return normalizeOutcome(
      item?.effective_outcome
      ?? item?.resolved_outcome
      ?? item?.active_correction?.corrected_outcome
      ?? item?.correction?.corrected_outcome
      ?? item?.outcome
      ?? item?.decision?.decision
      ?? item?.decision,
    );
  }

  function caseSummary(item) {
    return safeText(
      item?.summary
      ?? item?.explanation
      ?? item?.rationale
      ?? item?.reason
      ?? item?.description,
      "Evidence is available in the deterministic case receipt.",
    );
  }

  function caseEvidence(item) {
    const evidence = item?.evidence ?? item?.events ?? item?.evidence_refs ?? item?.contributors;
    const explicitCount = Number(item?.evidence_count ?? item?.event_count);
    if (Number.isFinite(explicitCount)) return `${explicitCount} item${explicitCount === 1 ? "" : "s"}`;
    if (Array.isArray(evidence)) return `${evidence.length} item${evidence.length === 1 ? "" : "s"}`;
    return safeText(item?.evidence_status ?? item?.source, "Receipt ready");
  }

  function caseSource(item) {
    return safeText(item?.source ?? item?.source_type ?? item?.channel, "Local evidence");
  }

  function activeCorrection(item) {
    const correction = item?.active_correction ?? item?.correction ?? null;
    if (correction && correction.active !== false && !correction.retracted_at) return correction;
    if (item?.human_corrected || caseOriginalOutcome(item) !== caseEffectiveOutcome(item)) {
      return correction ?? { corrected_outcome: caseEffectiveOutcome(item) };
    }
    return null;
  }

  function normalizeCompany(company) {
    if (typeof company === "string") return { company_name: company };
    return company && typeof company === "object" ? company : {};
  }

  function normalizeCases(cases) {
    return Array.isArray(cases) ? cases.filter((item) => item && typeof item === "object") : [];
  }

  function normalizeWebsiteSources(sources) {
    if (!Array.isArray(sources)) return [];
    return sources.filter((source) => source && typeof source === "object");
  }

  function normalizeCalendar(calendar) {
    const value = calendar && typeof calendar === "object" ? calendar : {};
    return {
      ...value,
      events: Array.isArray(value.events)
        ? value.events.filter((item) => item && typeof item === "object")
        : [],
      scheduled_count: Math.max(0, Number(value.scheduled_count) || 0),
      unscheduled_count: Math.max(0, Number(value.unscheduled_count) || 0),
      data_mode: safeText(value.data_mode, "empty"),
    };
  }

  function normalizeState(payload) {
    const root = payload?.state && typeof payload.state === "object" ? payload.state : payload;
    const state = root && typeof root === "object" ? root : {};
    const cases = normalizeCases(state.cases);
    let selected = state.selected;
    if (typeof selected === "string") {
      selected = cases.find((item) => caseId(item) === selected) ?? null;
    } else if (selected && typeof selected === "object") {
      const fullCase = cases.find((item) => caseId(item) === caseId(selected));
      selected = fullCase ? { ...fullCase, ...selected } : selected;
    } else {
      selected = null;
    }

    return {
      ...state,
      company: normalizeCompany(state.company),
      totals: state.totals && typeof state.totals === "object" ? state.totals : {},
      cases,
      selected,
      source_status: state.source_status && typeof state.source_status === "object" ? state.source_status : {},
      connectors: state.connectors && typeof state.connectors === "object" ? state.connectors : {},
      website_sources: normalizeWebsiteSources(state.website_sources),
      calendar: normalizeCalendar(state.calendar),
      tracking: state.tracking && typeof state.tracking === "object" ? state.tracking : {},
      grouped_metrics: Array.isArray(state.grouped_metrics)
        ? state.grouped_metrics.filter((item) => item && typeof item === "object")
        : [],
      chat_history: Array.isArray(state.chat_history) ? state.chat_history : [],
      patterns: state.patterns ?? [],
    };
  }

  function countFromTotals(totals, outcome) {
    const keys = Object.keys(totals ?? {});
    const target = keys.find((key) => key.toUpperCase() === outcome);
    const value = target === undefined ? undefined : Number(totals[target]);
    return Number.isFinite(value) ? value : null;
  }

  function deriveTotals(state) {
    const derived = { ALLOW: 0, REVIEW: 0, BLOCK: 0 };
    state.cases.forEach((item) => {
      const outcome = caseEffectiveOutcome(item);
      if (Object.hasOwn(derived, outcome)) derived[outcome] += 1;
    });

    ["ALLOW", "REVIEW", "BLOCK"].forEach((outcome) => {
      const supplied = countFromTotals(state.totals, outcome);
      if (supplied !== null) derived[outcome] = supplied;
    });

    const suppliedTotal = Number(
      state.totals.total
      ?? state.totals.evaluated
      ?? state.totals.total_evaluated,
    );
    derived.TOTAL = Number.isFinite(suppliedTotal)
      ? suppliedTotal
      : derived.ALLOW + derived.REVIEW + derived.BLOCK;
    return derived;
  }

  function errorMessage(error, fallback = "The request could not be completed.") {
    if (error instanceof Error && error.message) return error.message;
    return fallback;
  }

  async function apiRequest(path, { method = "GET", body } = {}) {
    const options = {
      method,
      headers: { Accept: "application/json" },
      cache: "no-store",
    };
    if (body !== undefined) {
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }

    let response;
    try {
      response = await fetch(path, options);
    } catch (error) {
      throw new Error(`Cannot reach the local ContextGate API. ${errorMessage(error, "")}`.trim());
    }

    const raw = await response.text();
    let payload = null;
    if (raw) {
      try {
        payload = JSON.parse(raw);
      } catch {
        payload = { message: raw };
      }
    }

    if (!response.ok) {
      const detail = payload?.detail ?? payload?.error ?? payload?.message ?? response.statusText;
      throw new Error(`${response.status} ${safeText(detail, "API request failed")}`);
    }
    return payload;
  }

  function setAppBusy(busy) {
    $("#app").setAttribute("aria-busy", String(Boolean(busy)));
    $("#refresh-state").disabled = Boolean(busy);
  }

  function setButtonBusy(button, busy, busyText = "Working…") {
    if (!button) return;
    if (busy) {
      button.dataset.originalText = button.textContent;
      button.textContent = busyText;
      button.disabled = true;
    } else {
      button.textContent = button.dataset.originalText || button.textContent;
      button.disabled = false;
      delete button.dataset.originalText;
    }
  }

  function showGlobalError(message) {
    $("#global-alert-message").textContent = message;
    $("#global-alert").hidden = false;
  }

  function clearGlobalError() {
    $("#global-alert").hidden = true;
    $("#global-alert-message").textContent = "";
  }

  function setFormStatus(id, message = "", isError = false) {
    const status = $(`#${id}`);
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("error", isError);
  }

  function showDialog(id) {
    const dialog = $(`#${id}`);
    if (!dialog || typeof dialog.showModal !== "function") {
      showGlobalError("This browser does not support the required dialog interface.");
      return;
    }
    if (id === "company-dialog") populateProfileForm();
    if (id === "case-dialog") renderCaseDialog();
    if (id === "calendar-dialog") renderCalendar();
    if (!dialog.open) dialog.showModal();
    const firstControl = $("input:not([type='hidden']), textarea, select, button", dialog);
    window.setTimeout(() => firstControl?.focus(), 0);
  }

  function outcomeClass(outcome) {
    return ["ALLOW", "REVIEW", "BLOCK"].includes(outcome) ? outcome.toLowerCase() : "neutral";
  }

  function setOutcomeBadge(node, outcome) {
    node.textContent = outcome;
    node.className = `outcome-badge ${outcomeClass(outcome)}`;
  }

  function renderHeader() {
    const company = view.state.company;
    const companyName = safeText(company.company_name ?? company.name, "Company workspace");
    $("#company-name").textContent = companyName;
    const workspaceLogo = $("#workspace-logo");
    const logoData = safeText(company.company_logo_data_url, "");
    workspaceLogo.hidden = !logoData;
    if (logoData) workspaceLogo.src = logoData;
    else workspaceLogo.removeAttribute("src");

    $("#operator-name").textContent = `Operator: ${safeText(company.operator_name, "Local operator")}`;
    const customPolicy = view.state.policy?.validated_custom === true
      || view.state.policy?.source === "validated_custom_file";
    const policyVersion = safeText(view.state.policy?.version, "active");
    const policyLabel = customPolicy ? "VALIDATED CUSTOM POLICY" : `ACTIVE POLICY ${policyVersion}`;
    $("#policy-status").textContent = policyLabel;
    $("#policy-detail").textContent = customPolicy ? "Validated custom policy" : `Server policy ${policyVersion}`;
    $("#workspace-mode").textContent = safeText(company.workspace_mode, "EXAMPLE · LOCAL").toUpperCase();
  }

  function renderTotals() {
    const totals = deriveTotals(view.state);
    $("#total-count").textContent = totals.TOTAL;
    $("#allow-count").textContent = totals.ALLOW;
    $("#review-count").textContent = totals.REVIEW;
    $("#block-count").textContent = totals.BLOCK;
    $("#orbit-total").textContent = totals.TOTAL;

    const denominator = totals.TOTAL > 0 ? totals.TOTAL : 1;
    const allowPercent = Math.round((totals.ALLOW / denominator) * 100);
    const reviewPercent = Math.round((totals.REVIEW / denominator) * 100);
    const blockPercent = Math.max(0, 100 - allowPercent - reviewPercent);
    $("#allow-percent").textContent = `${totals.TOTAL ? allowPercent : 0}%`;
    $("#review-percent").textContent = `${totals.TOTAL ? reviewPercent : 0}%`;
    $("#block-percent").textContent = `${totals.TOTAL ? blockPercent : 0}%`;

    const orbit = $("#decision-orbit");
    if (totals.TOTAL > 0) {
      const allowDegrees = (totals.ALLOW / denominator) * 360;
      const reviewDegrees = allowDegrees + (totals.REVIEW / denominator) * 360;
      orbit.style.background = `conic-gradient(${OUTCOME_COLORS.ALLOW} 0deg ${allowDegrees}deg, ${OUTCOME_COLORS.REVIEW} ${allowDegrees}deg ${reviewDegrees}deg, ${OUTCOME_COLORS.BLOCK} ${reviewDegrees}deg 360deg)`;
    } else {
      orbit.style.background = "conic-gradient(#24455a 0deg 360deg)";
    }
    orbit.setAttribute(
      "aria-label",
      `${totals.TOTAL} total decisions: ${totals.ALLOW} allow, ${totals.REVIEW} review, ${totals.BLOCK} block`,
    );
  }

  function queueCases() {
    if (view.activeFilter === "ALL") return view.state.cases;
    return view.state.cases.filter((item) => caseEffectiveOutcome(item) === view.activeFilter);
  }

  function renderQueue() {
    const container = $("#case-queue");
    container.replaceChildren();
    const cases = queueCases();
    if (!cases.length) {
      const empty = element("div", "empty-state");
      empty.append(element("p", "", `No ${view.activeFilter === "ALL" ? "" : `${view.activeFilter} `}cases in the active view.`));
      container.append(empty);
      return;
    }

    cases.forEach((item) => {
      const id = caseId(item);
      const outcome = caseEffectiveOutcome(item);
      const button = element("button", "case-row");
      button.type = "button";
      button.dataset.caseId = id;
      button.style.setProperty("--status-color", OUTCOME_COLORS[outcome] ?? OUTCOME_COLORS.UNKNOWN);
      button.setAttribute("aria-label", `Select ${id}, ${caseTitle(item)}, ${outcome}`);
      if (id === caseId(selectedCase())) button.classList.add("selected");

      button.append(element("span", "case-accent"));
      const main = element("span", "case-row-main");
      const top = element("span", "case-row-top");
      top.append(element("span", "case-id", id));
      top.append(element("strong", "", caseTitle(item)));
      const meta = element("span", "case-row-meta");
      meta.append(element("span", "", `${caseSource(item)} · ${caseEvidence(item)}`));
      main.append(top, meta);
      if (activeCorrection(item)) main.append(element("span", "correction-flag", "HUMAN-CORRECTED"));
      button.append(main);

      const badge = element("span", `outcome-badge ${outcomeClass(outcome)}`, outcome);
      button.append(badge);
      button.addEventListener("click", () => selectCase(id, button));
      container.append(button);
    });
  }

  function selectedCase() {
    if (!view.state) return null;
    if (view.state.selected) return view.state.selected;
    if (view.selectedCaseId) {
      return view.state.cases.find((item) => caseId(item) === view.selectedCaseId) ?? null;
    }
    return null;
  }

  function appendTag(container, value) {
    if (!value) return;
    container.append(element("span", "", value));
  }

  function renderSelected() {
    const item = selectedCase();
    const detailsButton = $("#open-case-details");
    const tagContainer = $("#selected-tags");
    tagContainer.replaceChildren();

    if (!item) {
      $("#selected-case-title").textContent = "No case selected";
      setOutcomeBadge($("#selected-outcome"), "WAITING");
      $("#selected-summary").textContent = "Choose a queue item to inspect its evidence and deterministic receipt.";
      $("#selected-original").textContent = "—";
      $("#selected-effective").textContent = "—";
      $("#selected-evidence").textContent = "—";
      detailsButton.disabled = true;
      return;
    }

    const id = caseId(item);
    const original = caseOriginalOutcome(item);
    const effective = caseEffectiveOutcome(item);
    view.selectedCaseId = id;
    $("#selected-case-title").textContent = `${id} · ${caseTitle(item)}`;
    setOutcomeBadge($("#selected-outcome"), effective);
    $("#selected-summary").textContent = caseSummary(item);
    $("#selected-original").textContent = original;
    $("#selected-effective").textContent = effective;
    $("#selected-evidence").textContent = caseEvidence(item);
    appendTag(tagContainer, caseSource(item));
    appendTag(tagContainer, safeText(item?.authority ?? item?.authority_tier, ""));
    if (activeCorrection(item)) appendTag(tagContainer, "HUMAN-CORRECTED");
    detailsButton.disabled = false;
  }

  function messageText(message) {
    const content = message?.content ?? message?.message ?? message?.text ?? message?.answer;
    if (typeof content === "string" || typeof content === "number") return String(content);
    if (content && typeof content === "object") {
      const sections = ["answer", "why", "evidence", "rules", "safe_next_step"]
        .filter((key) => content[key])
        .map((key) => `${key.replaceAll("_", " ").toUpperCase()}\n${safeText(content[key], "")}`);
      if (sections.length) return sections.join("\n\n");
      return JSON.stringify(content, null, 2);
    }
    return "No message content was returned.";
  }

  function messageGrounding(message) {
    const parts = [];
    const cases = message?.case_ids ?? message?.cases;
    const evidence = message?.evidence_refs ?? message?.evidence_event_ids;
    const rules = message?.rule_ids ?? message?.rules;
    const citations = message?.citations;
    if (cases) parts.push(`cases ${safeText(cases, "")}`);
    if (evidence) parts.push(`evidence ${safeText(evidence, "")}`);
    if (rules) parts.push(`rules ${safeText(rules, "")}`);
    if (citations) parts.push(`citations ${safeText(citations, "")}`);
    return parts.filter(Boolean).join(" · ");
  }

  function renderChat() {
    const log = $("#chat-log");
    log.replaceChildren();
    const history = view.state.chat_history;
    if (!history.length) {
      const initial = element("article", "chat-message assistant");
      initial.append(element("span", "message-role", "CONTEXTGATE"));
      initial.append(element("p", "", "Ask about a case, its evidence, a pattern, or why the gate reached an outcome."));
      log.append(initial);
    } else {
      history.forEach((message) => {
        const role = safeText(message?.role, "assistant").toLowerCase() === "user" ? "user" : "assistant";
        const article = element("article", `chat-message ${role}`);
        article.append(element("span", "message-role", role === "user" ? "OPERATOR" : "CONTEXTGATE"));
        article.append(element("p", "", messageText(message)));
        const grounding = messageGrounding(message);
        if (grounding) article.append(element("small", "message-grounding", `Grounded in · ${grounding}`));
        if (message?.saved === true) article.append(element("small", "message-grounding", "Remembered as retractable company guidance"));
        log.append(article);
      });
    }
    window.requestAnimationFrame(() => { log.scrollTop = log.scrollHeight; });
  }

  function sourceValue(sourceStatus, keys, fallback) {
    for (const key of keys) {
      if (Object.hasOwn(sourceStatus, key)) {
        const value = sourceStatus[key];
        if (typeof value === "boolean") return value ? "Connected" : "Not connected";
        return safeText(value, fallback);
      }
    }
    return fallback;
  }

  function renderSources() {
    const statuses = view.state.source_status;
    const connectedAccounts = connectorAccounts("google").length + connectorAccounts("microsoft").length;
    const mailbox = connectedAccounts
      ? `${connectedAccounts} account${connectedAccounts === 1 ? "" : "s"} connected`
      : sourceValue(statuses, ["mailbox", "gmail", "microsoft", "email"], "Not connected");
    const upload = sourceValue(statuses, ["upload", "local_upload", "file_upload"], "Ready");
    $("#mailbox-status").textContent = `Mailbox: ${mailbox}`;
    $("#upload-status").textContent = `Local upload: ${upload}`;
  }

  function connectorAccounts(provider) {
    const providerState = view.state?.connectors?.[provider];
    if (Array.isArray(providerState)) return providerState;
    return Array.isArray(providerState?.accounts) ? providerState.accounts : [];
  }

  function accountIdentifier(account) {
    if (typeof account === "string") return account;
    return safeText(account?.account ?? account?.email ?? account?.account_id ?? account?.id ?? account?.username, "");
  }

  function accountDisplayName(account) {
    if (typeof account === "string") return account;
    return safeText(account?.display_name ?? account?.name ?? account?.email ?? accountIdentifier(account), "Connected account");
  }

  function accountStatus(account) {
    if (typeof account === "string") return "Connected";
    return safeText(account?.status ?? account?.scan_status ?? account?.state, "Connected");
  }

  function renderConnectorAccounts(provider) {
    const container = $(`#${provider}-account-list`);
    if (!container) return;
    container.replaceChildren();
    const accounts = connectorAccounts(provider);
    if (!accounts.length) {
      container.append(element("p", "", `No ${provider === "google" ? "Google" : "Microsoft"} accounts connected.`));
      return;
    }

    accounts.forEach((account) => {
      const identifier = accountIdentifier(account);
      const row = element("div", "account-row");
      const identity = element("div", "account-identity");
      identity.append(element("strong", "", accountDisplayName(account)));
      identity.append(element("small", "", accountStatus(account)));
      const actions = element("div", "account-actions");
      const scan = element("button", "scan-account", "Scan");
      scan.type = "button";
      scan.addEventListener("click", () => scanConnectorAccount(provider, identifier, scan));
      const remove = element("button", "remove-account", "Remove");
      remove.type = "button";
      remove.addEventListener("click", () => disconnectConnectorAccount(provider, identifier, remove));
      actions.append(scan, remove);
      row.append(identity, actions);
      container.append(row);
    });
  }

  function renderConnectors() {
    renderConnectorAccounts("google");
    renderConnectorAccounts("microsoft");
  }

  function websiteSourceId(source) {
    return safeText(source?.source_id, "");
  }

  function websiteSourceHost(url) {
    try {
      return new URL(url).hostname.replace(/^www\./i, "");
    } catch {
      return "Public website";
    }
  }

  function websiteSourceTimestamp(value) {
    const raw = safeText(value, "");
    if (!raw) return "Not scanned yet";
    const parsed = new Date(raw);
    if (Number.isNaN(parsed.getTime())) return `Last scan: ${raw}`;
    return `Last scan: ${new Intl.DateTimeFormat(undefined, {
      dateStyle: "medium",
      timeStyle: "short",
    }).format(parsed)}`;
  }

  function websiteStatusClass(value) {
    const normalized = safeText(value, "ready").toLowerCase();
    return ["ready", "complete", "scanned", "error", "failed", "scanning", "busy"].includes(normalized)
      ? normalized
      : "ready";
  }

  function renderWebsiteSources() {
    const container = $("#website-source-list");
    if (!container) return;
    container.replaceChildren();
    if (view.state?.website_sources_error) {
      setFormStatus("website-source-status", safeText(view.state.website_sources_error), true);
    }
    const sources = view.state?.website_sources ?? [];
    if (!sources.length) {
      const empty = element("div", "website-source-empty");
      empty.append(element("span", "", "＋"), element("p", "", "No public websites configured yet. Add one above, then scan it when you choose."));
      container.append(empty);
      return;
    }

    sources.forEach((source) => {
      const sourceId = websiteSourceId(source);
      const sourceUrl = safeText(source.url, "URL unavailable");
      const sourceLabel = safeText(source.label, websiteSourceHost(sourceUrl));
      const operation = view.websiteSourceOperations.get(sourceId);
      const statusLabel = operation === "scan"
        ? "SCANNING"
        : operation === "remove"
          ? "REMOVING"
          : safeText(source.status, "READY").toUpperCase();
      const records = Number(source.records_count);

      const row = element("article", "website-source-row");
      const details = element("div", "website-source-details");
      const host = element("strong", "", sourceLabel);
      host.title = `${websiteSourceHost(sourceUrl)} · ${sourceUrl}`;
      const goal = element("span", "", `Collect · ${safeText(source.extraction_goal, "Specified public evidence")}`);
      goal.title = safeText(source.extraction_goal, "Specified public evidence");
      const urlText = element("small", "", sourceUrl);
      urlText.title = sourceUrl;

      const meta = element("div", "website-source-meta");
      const statusClass = websiteStatusClass(operation ? "busy" : source.last_error ? "error" : source.status);
      const status = element("span", `website-source-status ${statusClass}`, statusLabel);
      meta.append(status);
      meta.append(element("span", "", `${Number.isFinite(records) ? Math.max(0, records) : 0} record${records === 1 ? "" : "s"}`));
      meta.append(element("span", "", websiteSourceTimestamp(source.last_scan_at)));
      details.append(host, goal, urlText, meta);
      if (source.last_error) details.append(element("small", "website-source-error", safeText(source.last_error)));

      const actions = element("div", "website-source-actions");
      const scan = element("button", "scan-website", operation === "scan" ? "Scanning…" : "Scan now");
      scan.type = "button";
      scan.disabled = Boolean(operation || !sourceId);
      scan.setAttribute("aria-label", `Scan ${websiteSourceHost(sourceUrl)} now`);
      scan.addEventListener("click", () => scanWebsiteSource(source, scan));
      const remove = element("button", "remove-website", operation === "remove" ? "Removing…" : "Remove");
      remove.type = "button";
      remove.disabled = Boolean(operation || !sourceId);
      remove.setAttribute("aria-label", `Remove ${websiteSourceHost(sourceUrl)} from ContextGate`);
      remove.addEventListener("click", () => removeWebsiteSource(source, remove));
      actions.append(scan, remove);
      row.append(details, actions);
      container.append(row);
    });
  }

  function normalizePatterns(patterns) {
    if (Array.isArray(patterns)) return patterns;
    if (patterns && typeof patterns === "object") {
      return Object.entries(patterns).flatMap(([key, value]) => {
        if (Array.isArray(value)) return value.map((item) => ({ group: key, ...(typeof item === "object" ? item : { value: item }) }));
        if (value && typeof value === "object") return [{ group: key, ...value }];
        return [{ group: key, value }];
      });
    }
    return [];
  }

  function renderPatterns() {
    const container = $("#pattern-list");
    container.replaceChildren();
    const patterns = normalizePatterns(view.state.patterns);
    if (!patterns.length) {
      const empty = element("div", "empty-state");
      empty.append(element("p", "", "No company patterns are available for this workspace."));
      container.append(empty);
      return;
    }

    patterns.forEach((pattern, index) => {
      const item = typeof pattern === "object" && pattern !== null ? pattern : { value: pattern };
      const article = element("article", "pattern-item");
      const label = safeText(item.label ?? item.name ?? item.field ?? item.group ?? item.type, `Pattern ${index + 1}`);
      const count = item.count ?? item.observation_count ?? item.total ?? item.value;
      const detail = safeText(item.description ?? item.summary ?? item.detail ?? item.pattern, "Observed company-memory pattern");
      article.append(element("h3", "", label));
      article.append(element("strong", "", safeText(count, "OBSERVED")));
      article.append(element("p", "", detail));
      container.append(article);
    });
  }

  function calendarEvents() {
    return view.state?.calendar?.events ?? [];
  }

  function calendarEventId(item) {
    return safeText(item?.record_id ?? item?.event_key, "");
  }

  function parseCalendarDate(value) {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(safeText(value, ""));
    if (!match) return null;
    const date = new Date(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
    if (
      date.getFullYear() !== Number(match[1])
      || date.getMonth() !== Number(match[2]) - 1
      || date.getDate() !== Number(match[3])
    ) return null;
    return date;
  }

  function calendarDateKey(date) {
    const year = date.getFullYear();
    const month = String(date.getMonth() + 1).padStart(2, "0");
    const day = String(date.getDate()).padStart(2, "0");
    return `${year}-${month}-${day}`;
  }

  function sameCalendarMonth(date, month) {
    return Boolean(
      date
      && month
      && date.getFullYear() === month.getFullYear()
      && date.getMonth() === month.getMonth(),
    );
  }

  function initializeCalendarMonth() {
    if (view.calendarMonth instanceof Date && !Number.isNaN(view.calendarMonth.getTime())) return;
    const today = new Date();
    const scheduled = calendarEvents()
      .map((item) => ({ item, date: parseCalendarDate(item.date_iso) }))
      .filter(({ date }) => date !== null)
      .sort((a, b) => a.date - b.date);
    const currentMonthEvent = scheduled.find(({ date }) => sameCalendarMonth(date, today));
    const nextEvent = scheduled.find(({ date }) => date >= new Date(today.getFullYear(), today.getMonth(), today.getDate()));
    const anchor = currentMonthEvent?.date ?? nextEvent?.date ?? scheduled[0]?.date ?? today;
    view.calendarMonth = new Date(anchor.getFullYear(), anchor.getMonth(), 1);
  }

  function calendarModeMessage(mode, total, scheduled, unscheduled) {
    const visibility = "Hidden and deleted source preferences are excluded.";
    if (!total || mode === "empty") {
      return `No visible event records are available yet. Connect or upload a source with event data; ContextGate will not invent dates. ${visibility}`;
    }
    if (mode === "fictional_demo") {
      return `Fictional demo calendar — ${total} distinct visible demo event${total === 1 ? "" : "s"}; ${scheduled} scheduled and ${unscheduled} missing source dates. Connect, scan, or upload a real source to replace this proof set. ${visibility}`;
    }
    if (mode === "mixed_sources") {
      return `Mixed-source calendar — real and fictional records are clearly labeled. Only ${scheduled} source-provided date${scheduled === 1 ? " is" : "s are"} placed on the grid; ${unscheduled} event${unscheduled === 1 ? " needs" : "s need"} a date. ${visibility}`;
    }
    return `Live evidence calendar — ${total} distinct visible event${total === 1 ? "" : "s"} from scanned or uploaded sources. ${scheduled} have source-provided dates; ${unscheduled} remain unscheduled instead of being guessed. ${visibility}`;
  }

  function selectCalendarEvent(item) {
    view.selectedCalendarEventId = calendarEventId(item);
    view.calendarSelectionInitialized = true;
    renderCalendar();
  }

  function detailRow(label, value, fallback) {
    const wrapper = element("div");
    wrapper.append(element("dt", "", label));
    wrapper.append(element("dd", "", safeText(value, fallback)));
    return wrapper;
  }

  function renderCalendarDetail(item) {
    const container = $("#calendar-event-detail");
    container.replaceChildren();
    if (!item) {
      const empty = element("div", "calendar-empty-detail");
      empty.append(element("span", "", "◎"));
      empty.append(element("p", "", "Select an event to inspect its grounded details and evidence reference."));
      container.append(empty);
      return;
    }

    const fictional = item.fictional === true;
    const origin = element(
      "span",
      `calendar-detail-origin${fictional ? " fictional" : ""}`,
      fictional ? "FICTIONAL DEMO EVIDENCE" : "SCANNED / UPLOADED EVIDENCE",
    );
    container.append(origin);
    container.append(element("h3", "", safeText(item.title, "Untitled event")));
    container.append(element(
      "p",
      "",
      `${safeText(item.organization, "Organizer not found in source")} · ${safeText(item.source_name, "Source not labeled")}`,
    ));

    const details = element("dl", "calendar-detail-list");
    details.append(detailRow("Date", item.date, "Date not found in source"));
    details.append(detailRow("Time", item.time, "Time not found in source"));
    details.append(detailRow("Address / location", item.address ?? item.location, "Address not found in source"));
    details.append(detailRow("Source", item.source_name, "Source not labeled"));

    const evidenceRow = element("div");
    evidenceRow.append(element("dt", "", "Evidence reference"));
    const evidenceValue = element("dd");
    const reference = safeText(item.evidence_reference, "Reference not available");
    if (/^https?:\/\//i.test(reference)) {
      const link = element("a", "calendar-evidence-link", reference);
      link.href = reference;
      link.target = "_blank";
      link.rel = "noopener noreferrer";
      evidenceValue.append(link);
    } else {
      evidenceValue.textContent = reference;
    }
    evidenceRow.append(evidenceValue);
    details.append(evidenceRow);
    container.append(details);

    const askButton = element("button", "primary-button calendar-ask-button", "Ask ContextGate about this source");
    askButton.type = "button";
    askButton.addEventListener("click", () => {
      const target = safeText(item.organization ?? item.source_name, "this event source");
      $("#chat-input").value = `Show events from ${target}.`;
      $("#calendar-dialog").close();
      $("#chat-input").focus();
    });
    container.append(askButton);
  }

  function renderUnscheduledCalendarEvents(events) {
    const section = $("#calendar-unscheduled-section");
    const container = $("#calendar-unscheduled-list");
    const count = events.length;
    $("#calendar-unscheduled-summary").textContent = String(count);
    section.hidden = count === 0;
    container.replaceChildren();
    events.forEach((item) => {
      const button = element(
        "button",
        `calendar-unscheduled-item${item.fictional === true ? " fictional" : ""}`,
      );
      button.type = "button";
      if (calendarEventId(item) === view.selectedCalendarEventId) button.classList.add("selected");
      button.append(element("strong", "", safeText(item.title, "Untitled event")));
      button.append(element(
        "small",
        "",
        `${safeText(item.organization, "Organizer not found")} · ${safeText(item.source_name, "Source not labeled")}`,
      ));
      button.setAttribute("aria-label", `Inspect ${safeText(item.title, "untitled event")}; date not found in source`);
      button.addEventListener("click", () => selectCalendarEvent(item));
      container.append(button);
    });
  }

  function renderCalendarGrid(events) {
    const grid = $("#calendar-grid");
    grid.replaceChildren();
    const month = view.calendarMonth;
    const monthStart = new Date(month.getFullYear(), month.getMonth(), 1);
    const gridStart = new Date(monthStart);
    gridStart.setDate(1 - monthStart.getDay());
    const todayKey = calendarDateKey(new Date());
    const eventsByDay = new Map();
    events.forEach((item) => {
      if (!item.date_iso) return;
      const bucket = eventsByDay.get(item.date_iso) ?? [];
      bucket.push(item);
      eventsByDay.set(item.date_iso, bucket);
    });

    for (let offset = 0; offset < 42; offset += 1) {
      const date = new Date(gridStart);
      date.setDate(gridStart.getDate() + offset);
      const key = calendarDateKey(date);
      const dayEvents = eventsByDay.get(key) ?? [];
      const cell = element("div", "calendar-day");
      cell.setAttribute("role", "gridcell");
      cell.setAttribute("aria-label", new Intl.DateTimeFormat(undefined, { dateStyle: "full" }).format(date));
      if (!sameCalendarMonth(date, month)) cell.classList.add("outside-month");
      if (key === todayKey) cell.classList.add("today");
      cell.append(element("span", "calendar-day-number", String(date.getDate())));
      const eventList = element("div", "calendar-day-events");
      dayEvents.forEach((item) => {
        const title = safeText(item.title, "Untitled event");
        const time = safeText(item.time, "Time not found");
        const chip = element(
          "button",
          `calendar-event-chip${item.fictional === true ? " fictional" : ""}`,
          `${item.time ? `${time} · ` : ""}${title}`,
        );
        chip.type = "button";
        chip.title = `${title} — ${time}`;
        chip.setAttribute("aria-label", `Inspect ${title}, ${time}`);
        if (calendarEventId(item) === view.selectedCalendarEventId) chip.classList.add("selected");
        chip.addEventListener("click", () => selectCalendarEvent(item));
        eventList.append(chip);
      });
      cell.append(eventList);
      grid.append(cell);
    }
  }

  function renderCalendar() {
    if (!view.state || !$("#calendar-grid")) return;
    const events = calendarEvents();
    const scheduled = events.filter((item) => parseCalendarDate(item.date_iso));
    const unscheduled = events.filter((item) => !parseCalendarDate(item.date_iso));
    initializeCalendarMonth();

    if (
      view.selectedCalendarEventId
      && !events.some((item) => calendarEventId(item) === view.selectedCalendarEventId)
    ) {
      view.selectedCalendarEventId = null;
      view.calendarSelectionInitialized = false;
    }
    if (!view.calendarSelectionInitialized) {
      const firstInMonth = scheduled.find(({ date_iso: date }) => sameCalendarMonth(parseCalendarDate(date), view.calendarMonth));
      view.selectedCalendarEventId = calendarEventId(firstInMonth ?? scheduled[0] ?? unscheduled[0]);
      view.calendarSelectionInitialized = true;
    }

    const scheduledCount = scheduled.length;
    const unscheduledCount = unscheduled.length;
    $("#calendar-scheduled-count").textContent = String(scheduledCount);
    $("#calendar-unscheduled-count").textContent = String(unscheduledCount);
    $("#calendar-rail-status").textContent = events.length
      ? `${scheduledCount} scheduled · ${unscheduledCount} need date`
      : "No visible events";
    $("#calendar-month-label").textContent = new Intl.DateTimeFormat(undefined, {
      month: "long",
      year: "numeric",
    }).format(view.calendarMonth);

    const mode = safeText(view.state.calendar?.data_mode, "empty");
    const origin = $("#calendar-origin");
    origin.className = "calendar-origin";
    if (mode === "fictional_demo") origin.classList.add("fictional");
    if (mode === "empty") origin.classList.add("empty");
    origin.textContent = calendarModeMessage(
      mode,
      events.length,
      scheduledCount,
      unscheduledCount,
    );

    renderCalendarGrid(events);
    renderUnscheduledCalendarEvents(unscheduled);
    const selected = events.find((item) => calendarEventId(item) === view.selectedCalendarEventId);
    renderCalendarDetail(selected);
  }

  function moveCalendarMonth(offset) {
    initializeCalendarMonth();
    view.calendarMonth = new Date(
      view.calendarMonth.getFullYear(),
      view.calendarMonth.getMonth() + offset,
      1,
    );
    const inMonth = calendarEvents().find((item) => sameCalendarMonth(parseCalendarDate(item.date_iso), view.calendarMonth));
    view.selectedCalendarEventId = inMonth ? calendarEventId(inMonth) : null;
    view.calendarSelectionInitialized = true;
    renderCalendar();
  }

  function renderAll() {
    if (!view.state) return;
    renderHeader();
    renderTotals();
    renderQueue();
    renderSelected();
    renderChat();
    renderSources();
    renderConnectors();
    renderWebsiteSources();
    renderPatterns();
    renderCalendar();
    if (!$("#company-dialog").open) populateProfileForm();
    $("#last-updated").textContent = `Updated ${new Intl.DateTimeFormat(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date())}`;
  }

  async function refreshState({ quiet = false } = {}) {
    if (!quiet) setAppBusy(true);
    try {
      const payload = await apiRequest("/api/state");
      view.state = normalizeState(payload);
      if (view.state.selected) view.selectedCaseId = caseId(view.state.selected);
      renderAll();
      clearGlobalError();
    } catch (error) {
      showGlobalError(errorMessage(error));
      if (!view.state) {
        view.state = normalizeState({});
        renderAll();
      }
    } finally {
      if (!quiet) setAppBusy(false);
    }
  }

  function looksLikeState(payload) {
    const root = payload?.state ?? payload;
    return Boolean(root && typeof root === "object" && ["cases", "totals", "selected", "company", "chat_history"].some((key) => Object.hasOwn(root, key)));
  }

  async function mutate(path, body) {
    const payload = await apiRequest(path, { method: "POST", body });
    if (looksLikeState(payload)) {
      view.state = normalizeState(payload);
      if (view.state.selected) view.selectedCaseId = caseId(view.state.selected);
      renderAll();
    } else {
      await refreshState({ quiet: true });
    }
    return payload;
  }

  async function selectCase(id, button) {
    if (!id || id === "UNASSIGNED") return;
    view.selectedCaseId = id;
    $$(".case-row").forEach((row) => row.classList.toggle("selected", row === button));
    try {
      await mutate("/api/select", { case_id: id });
      clearGlobalError();
    } catch (error) {
      showGlobalError(`Could not select ${id}. ${errorMessage(error)}`);
      renderQueue();
    }
  }

  async function checkHealth({ reportError = true } = {}) {
    try {
      const payload = await apiRequest("/api/health");
      view.health = payload ?? { status: "ok" };
      const status = safeText(payload?.status ?? payload?.health, "ok");
      $("#engine-status").textContent = `ENGINE ${status.toUpperCase()}`;
      $("#health-detail").textContent = status;
    } catch (error) {
      view.health = { status: "unavailable" };
      $("#engine-status").textContent = "ENGINE UNAVAILABLE";
      $("#health-detail").textContent = "Unavailable";
      if (reportError) showGlobalError(`Health check failed. ${errorMessage(error)}`);
    }
  }

  function populateProfileForm() {
    if (!view.state) return;
    const company = view.state.company;
    $("#profile-company-name").value = safeText(company.company_name ?? company.name, "");
    $("#profile-operator-name").value = safeText(company.operator_name, "");
    $("#profile-company-website").value = safeText(company.company_website, "");
    $("#profile-important-detail").value = safeText(company.important_detail, "");
    const fields = company.identity_fields;
    $("#profile-identity-fields").value = Array.isArray(fields) ? fields.join(", ") : safeText(fields, "");
    $("#profile-risk-posture").value = company.risk_posture === "custom_policy"
      ? "custom_policy"
      : "safety_first";
    $("#voice-enabled").checked = company.voice_enabled === true || company.voice_enabled === "true";
    const configuredLimit = Number.parseInt(company.mail_scan_limit, 10);
    $("#scan-limit").value = Number.isFinite(configuredLimit)
      ? String(Math.min(25, Math.max(1, configuredLimit)))
      : "25";
    $("#auto-monitor-enabled").checked = company.auto_monitor_enabled === true || company.auto_monitor_enabled === "true";
    const configuredMonitorMinutes = Number.parseInt(company.auto_monitor_minutes, 10);
    $("#auto-monitor-minutes").value = Number.isFinite(configuredMonitorMinutes)
      ? String(Math.min(1440, Math.max(1, configuredMonitorMinutes)))
      : "15";
    $("#auto-monitor-now").disabled = !$("#auto-monitor-enabled").checked || view.autoMonitorRunning;
    $("#document-company-header").checked = company.document_company_header !== false;
    $("#document-footer").value = safeText(company.document_footer, "");
    const logoPreview = $("#company-logo-preview");
    const logoData = safeText(company.company_logo_data_url, "");
    logoPreview.hidden = !logoData;
    if (logoData) logoPreview.src = logoData;
    else logoPreview.removeAttribute("src");
    $("#remove-company-logo").disabled = !company.has_company_logo;
  }

  function appendReceiptBlock(container, title, content) {
    const block = element("section", "receipt-block");
    block.append(element("h3", "", title));
    block.append(element("p", "", safeText(content, "Not supplied")));
    container.append(block);
  }

  function renderCaseDialog() {
    const item = selectedCase();
    const container = $("#case-detail-content");
    container.replaceChildren();
    if (!item) {
      appendReceiptBlock(container, "No selected case", "Choose a case from the signal queue first.");
      $("#correction-form").hidden = true;
      $("#retraction-section").hidden = true;
      return;
    }

    $("#correction-form").hidden = false;
    $("#case-dialog-title").textContent = `${caseId(item)} · ${caseTitle(item)}`;
    const facts = element("div", "receipt-grid");
    [
      ["Original receipt", caseOriginalOutcome(item)],
      ["Effective status", caseEffectiveOutcome(item)],
      ["Evidence", caseEvidence(item)],
      ["Source", caseSource(item)],
      ["Policy fingerprint", item.policy_fingerprint ?? item.policy?.fingerprint],
      ["Request fingerprint", item.request_fingerprint ?? item.request_digest],
    ].forEach(([label, value]) => {
      const fact = element("div");
      fact.append(element("span", "", label));
      fact.append(element("strong", "", safeText(value)));
      facts.append(fact);
    });
    container.append(facts);
    appendReceiptBlock(container, "Why", caseSummary(item));

    const evidence = item.evidence ?? item.events ?? item.evidence_refs;
    if (evidence) appendReceiptBlock(container, "Evidence trace", safeText(evidence));
    const rules = item.rules ?? item.rule_ids ?? item.failed_rules;
    if (rules) appendReceiptBlock(container, "Rules", safeText(rules));

    const original = caseOriginalOutcome(item);
    const alternative = ["ALLOW", "REVIEW", "BLOCK"].find((value) => value !== original) ?? "REVIEW";
    $("#corrected-outcome").value = alternative;
    const correction = activeCorrection(item);
    $("#retraction-section").hidden = !correction;
    if (correction) {
      appendReceiptBlock(
        container,
        "Active human correction",
        `${safeText(correction.original_outcome, original)} → ${safeText(correction.corrected_outcome, caseEffectiveOutcome(item))}. Reviewer: ${safeText(correction.reviewer, "Recorded reviewer")}. ${safeText(correction.rationale, "")}`,
      );
    }
  }

  async function fileToBase64(file) {
    const bytes = new Uint8Array(await file.arrayBuffer());
    let binary = "";
    const chunkSize = 0x8000;
    for (let offset = 0; offset < bytes.length; offset += chunkSize) {
      binary += String.fromCharCode(...bytes.subarray(offset, offset + chunkSize));
    }
    return btoa(binary);
  }

  function getLocalPreference(key, fallback) {
    try {
      const stored = window.localStorage.getItem(`contextgate.${key}`);
      return stored === null ? fallback : stored;
    } catch {
      return fallback;
    }
  }

  function setLocalPreference(key, value, statusId = "layout-status") {
    try {
      window.localStorage.setItem(`contextgate.${key}`, String(value));
      setFormStatus(statusId, "Saved in this browser only.");
      return true;
    } catch (error) {
      const message = `This browser could not save the local preference. ${errorMessage(error)}`;
      setFormStatus(statusId, message, true);
      showGlobalError(message);
      return false;
    }
  }

  function clampedLayoutWidths(railWidth, chatWidth) {
    let rail = Math.min(260, Math.max(160, Number(railWidth) || 194));
    let chat = Math.min(460, Math.max(280, Number(chatWidth) || 316));
    if (window.innerWidth > 1100) {
      const centerMinimum = window.innerWidth <= 1260 ? 480 : 510;
      const widthBudget = Math.max(440, window.innerWidth - centerMinimum - 66);
      if (rail + chat > widthBudget) chat = Math.max(280, widthBudget - rail);
      if (rail + chat > widthBudget) rail = Math.max(160, widthBudget - chat);
    }
    return { railWidth: Math.round(rail), chatWidth: Math.round(chat) };
  }

  function updateLayoutOutputs() {
    $("#rail-width-output").textContent = `${$("#rail-width").value} px`;
    $("#chat-width-output").textContent = `${$("#chat-width").value} px`;
  }

  function applyLayout({ persist = false } = {}) {
    const position = $("input[name='chat_position']:checked")?.value ?? "right";
    const preset = $("#layout-preset").value;
    const widths = clampedLayoutWidths($("#rail-width").value, $("#chat-width").value);
    $("#rail-width").value = String(widths.railWidth);
    $("#chat-width").value = String(widths.chatWidth);
    updateLayoutOutputs();
    document.documentElement.style.setProperty("--rail-width", `${widths.railWidth}px`);
    document.documentElement.style.setProperty("--chat-width", `${widths.chatWidth}px`);
    $("#main-content").dataset.chatPosition = position;
    if (persist) {
      const saved = [
        setLocalPreference("layout_position", position),
        setLocalPreference("layout_preset", preset),
        setLocalPreference("layout_rail_width", widths.railWidth),
        setLocalPreference("layout_chat_width", widths.chatWidth),
      ].every(Boolean);
      if (saved) setFormStatus("layout-status", "Layout applied and saved in this browser only.");
    }
  }

  function initializeLayout() {
    const storedPreset = getLocalPreference("layout_preset", "balanced");
    const preset = Object.hasOwn(LAYOUT_PRESETS, storedPreset) ? storedPreset : storedPreset === "custom" ? "custom" : "balanced";
    const defaults = LAYOUT_PRESETS[preset] ?? LAYOUT_PRESETS.balanced;
    const railWidth = Number.parseInt(getLocalPreference("layout_rail_width", String(defaults.railWidth)), 10);
    const chatWidth = Number.parseInt(getLocalPreference("layout_chat_width", String(defaults.chatWidth)), 10);
    const position = getLocalPreference("layout_position", "right");
    $("#layout-preset").value = preset;
    $("#rail-width").value = String(railWidth);
    $("#chat-width").value = String(chatWidth);
    const positionControl = $(`input[name='chat_position'][value='${["left", "middle", "right"].includes(position) ? position : "right"}']`);
    if (positionControl) positionControl.checked = true;
    applyLayout();
  }

  function scanLimit() {
    const parsed = Number.parseInt($("#scan-limit")?.value ?? "25", 10);
    return Number.isFinite(parsed) ? Math.min(25, Math.max(1, parsed)) : 25;
  }

  function autoMonitorMinutes() {
    const parsed = Number.parseInt($("#auto-monitor-minutes")?.value ?? "15", 10);
    return Math.min(1440, Math.max(1, Number.isFinite(parsed) ? parsed : 15));
  }

  function autoMonitorIsEnabled() {
    return view.state?.company?.auto_monitor_enabled === true
      || view.state?.company?.auto_monitor_enabled === "true";
  }

  function onlineSourceCount() {
    const websites = view.state?.website_sources?.length ?? 0;
    const mailboxes = connectorAccounts("google").length + connectorAccounts("microsoft").length;
    return websites + mailboxes;
  }

  function setAutoMonitorStatus(message, isError = false) {
    const status = $("#auto-monitor-status");
    if (!status) return;
    status.textContent = message;
    status.classList.toggle("error", isError);
  }

  function stopAutoMonitorTimer() {
    if (view.autoMonitorTimer !== null) {
      window.clearTimeout(view.autoMonitorTimer);
      view.autoMonitorTimer = null;
    }
  }

  function scheduleAutoMonitor() {
    stopAutoMonitorTimer();
    if (!autoMonitorIsEnabled()) {
      setAutoMonitorStatus("Auto-monitor is off. Manual Scan buttons remain available.");
      $("#auto-monitor-now").disabled = true;
      return;
    }
    $("#auto-monitor-now").disabled = view.autoMonitorRunning;
    const minutes = Math.min(1440, Math.max(1, Number(view.state?.company?.auto_monitor_minutes) || 15));
    const nextCheck = new Date(Date.now() + minutes * 60 * 1000);
    const nextLabel = new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
    }).format(nextCheck);
    const waiting = onlineSourceCount() === 0
      ? " No configured website or connected mailbox is available yet."
      : "";
    const previous = view.autoMonitorLastResult ? `${view.autoMonitorLastResult} ` : "";
    setAutoMonitorStatus(
      `${previous}Next automatic check: ${nextLabel}.${waiting}`,
      view.autoMonitorLastFailed,
    );
    view.autoMonitorTimer = window.setTimeout(() => runAutoMonitor(), minutes * 60 * 1000);
  }

  async function runAutoMonitor({ manual = false } = {}) {
    if (view.autoMonitorRunning || (!manual && !autoMonitorIsEnabled())) return;
    view.autoMonitorRunning = true;
    stopAutoMonitorTimer();
    $("#auto-monitor-now").disabled = true;
    setAutoMonitorStatus("Checking configured websites and connected mailboxes…");
    let latestState = view.state;
    let completed = 0;
    const failures = [];
    let localStateAvailable = true;
    const websites = [...(latestState?.website_sources ?? [])];
    for (const source of websites) {
      const sourceId = websiteSourceId(source);
      if (!sourceId) continue;
      view.websiteSourceOperations.set(sourceId, "scan");
      renderWebsiteSources();
      try {
        latestState = normalizeState(await apiRequest("/api/websites/scan", {
          method: "POST",
          body: { source_id: sourceId },
        }));
        completed += 1;
      } catch (error) {
        failures.push(`${websiteSourceHost(source.url)}: ${errorMessage(error).slice(0, 160)}`);
      } finally {
        view.websiteSourceOperations.delete(sourceId);
      }
    }
    for (const provider of ["google", "microsoft"]) {
      const providerState = latestState?.connectors?.[provider];
      const accounts = Array.isArray(providerState?.accounts) ? providerState.accounts : [];
      for (const accountValue of accounts) {
        const account = accountIdentifier(accountValue);
        if (!account) continue;
        try {
          latestState = normalizeState(await apiRequest("/api/connectors/scan", {
            method: "POST",
            body: {
              provider,
              account,
              limit: Number(latestState?.company?.mail_scan_limit) || 25,
            },
          }));
          completed += 1;
        } catch (error) {
          failures.push(`${account}: ${errorMessage(error).slice(0, 160)}`);
        }
      }
    }
    try {
      latestState = normalizeState(await apiRequest("/api/state"));
    } catch {
      failures.push("Local state refresh failed after the source checks.");
      localStateAvailable = false;
    }
    view.state = latestState;
    if (view.state?.selected) view.selectedCaseId = caseId(view.state.selected);
    const checkedAt = new Intl.DateTimeFormat(undefined, {
      hour: "numeric",
      minute: "2-digit",
      second: "2-digit",
    }).format(new Date());
    const failureDetail = failures.length ? ` ${failures.slice(0, 2).join(" | ")}` : "";
    view.autoMonitorLastResult = failures.length
      ? `Last check ${checkedAt}: ${completed} succeeded, ${failures.length} failed.${failureDetail}`
      : `Last check ${checkedAt}: ${completed} source${completed === 1 ? "" : "s"} checked.`;
    view.autoMonitorLastFailed = failures.length > 0;
    view.autoMonitorRunning = false;
    renderAll();
    if (!localStateAvailable) {
      stopAutoMonitorTimer();
      $("#auto-monitor-now").disabled = !autoMonitorIsEnabled();
      setAutoMonitorStatus(
        `${view.autoMonitorLastResult} Automatic checks are paused because the local server is unavailable. Restart it, then choose Check connected sources now.`,
        true,
      );
      return;
    }
    scheduleAutoMonitor();
  }

  function voiceEnabled() {
    return Boolean($("#voice-enabled")?.checked);
  }

  function speakAssistantReply() {
    if (!voiceEnabled() || !("speechSynthesis" in window)) return;
    const latest = [...view.state.chat_history].reverse().find((item) => safeText(item?.role, "assistant").toLowerCase() !== "user");
    if (!latest) return;
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(messageText(latest));
    const voices = window.speechSynthesis.getVoices();
    const preferred = voices.find((voice) =>
      /\b(aria|ava|emma|jenny|samantha|sonia|zira|female)\b/i.test(voice.name)
      && /^en(?:-|$)/i.test(voice.lang),
    ) ?? voices.find((voice) => /^en(?:-|$)/i.test(voice.lang));
    if (preferred) utterance.voice = preferred;
    utterance.rate = 0.98;
    utterance.pitch = 1.04;
    window.speechSynthesis.speak(utterance);
  }

  async function saveCompanyLogo(button) {
    const input = $("#company-logo-file");
    const file = input.files?.[0];
    setFormStatus("company-logo-status");
    if (!file) {
      setFormStatus("company-logo-status", "Choose a PNG or JPEG logo first.", true);
      return;
    }
    if (!["image/png", "image/jpeg"].includes(file.type) || file.size > 1024 * 1024) {
      setFormStatus("company-logo-status", "Choose a PNG or JPEG no larger than 1 MB.", true);
      return;
    }
    setButtonBusy(button, true, "Saving…");
    try {
      await mutate("/api/profile/logo", {
        content_type: file.type,
        data_base64: await fileToBase64(file),
      });
      input.value = "";
      setFormStatus("company-logo-status", "Company logo saved locally and normalized without image metadata.");
      clearGlobalError();
    } catch (error) {
      const message = errorMessage(error);
      setFormStatus("company-logo-status", message, true);
      showGlobalError(`Company logo was not saved. ${message}`);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function removeCompanyLogo(button) {
    if (!window.confirm("Remove the saved company logo from this workspace?")) return;
    setButtonBusy(button, true, "Removing…");
    setFormStatus("company-logo-status");
    try {
      await mutate("/api/profile/logo/remove", {});
      setFormStatus("company-logo-status", "Company logo removed. The company name and footer settings were unchanged.");
      clearGlobalError();
    } catch (error) {
      const message = errorMessage(error);
      setFormStatus("company-logo-status", message, true);
      showGlobalError(`Company logo was not removed. ${message}`);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function configureGoogle(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("button[type='submit']", form);
    const file = $("#google-client-json").files?.[0];
    setFormStatus("google-configure-status");
    if (!file) {
      setFormStatus("google-configure-status", "Choose the OAuth client JSON file first.", true);
      return;
    }
    if (file.size > 32 * 1024) {
      setFormStatus("google-configure-status", "The OAuth client JSON file is larger than 32 KB.", true);
      return;
    }
    setButtonBusy(button, true, "Saving…");
    try {
      await mutate("/api/connectors/google/configure", { client_json_base64: await fileToBase64(file) });
      form.reset();
      setFormStatus("google-configure-status", "Google OAuth configuration accepted by the local API. Use Add Google account to authorize an account.");
      clearGlobalError();
    } catch (error) {
      const message = errorMessage(error);
      setFormStatus("google-configure-status", message, true);
      showGlobalError(`Google OAuth was not configured. ${message}`);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function configureMicrosoft(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("button[type='submit']", form);
    const clientId = $("#microsoft-client-id").value.trim();
    setButtonBusy(button, true, "Saving…");
    setFormStatus("microsoft-configure-status");
    try {
      await mutate("/api/connectors/microsoft/configure", { client_id: clientId });
      form.reset();
      setFormStatus("microsoft-configure-status", "Microsoft OAuth configuration accepted by the local API. Use Add Microsoft account to authorize an account.");
      clearGlobalError();
    } catch (error) {
      const message = errorMessage(error);
      setFormStatus("microsoft-configure-status", message, true);
      showGlobalError(`Microsoft OAuth was not configured. ${message}`);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function startConnector(provider, button) {
    const providerLabel = provider === "google" ? "Google" : "Microsoft";
    const authWindow = window.open("about:blank", `contextgate-${provider}-oauth`);
    if (authWindow) {
      authWindow.opener = null;
      authWindow.document.title = `Opening ${providerLabel} authorization…`;
      authWindow.document.body.textContent = `Waiting for the local ContextGate API to start ${providerLabel} authorization…`;
    }
    setButtonBusy(button, true, "Starting…");
    setFormStatus("connector-action-status", `Starting ${providerLabel} authorization…`);
    try {
      const payload = await apiRequest(`/api/connectors/${provider}/start`, { method: "POST", body: {} });
      const authorizationUrl = safeText(payload?.authorization_url, "");
      const parsedUrl = new URL(authorizationUrl);
      if (!["https:", "http:"].includes(parsedUrl.protocol)) throw new Error("The connector API returned an invalid authorization URL.");
      if (authWindow) {
        authWindow.location.replace(parsedUrl.href);
      } else {
        throw new Error("The browser blocked the authorization window. Allow pop-ups for this local app and try again.");
      }
      setFormStatus("connector-action-status", `${providerLabel} authorization opened in a new window. Return here and refresh after completing it.`);
      clearGlobalError();
    } catch (error) {
      authWindow?.close();
      const message = errorMessage(error);
      setFormStatus("connector-action-status", message, true);
      showGlobalError(`${providerLabel} authorization did not start. ${message}`);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function scanConnectorAccount(provider, account, button) {
    if (!account) {
      setFormStatus("connector-action-status", "The connected account has no usable account identifier.", true);
      return;
    }
    setButtonBusy(button, true, "Scanning…");
    setFormStatus("connector-action-status", `Scanning ${account} with a limit of ${scanLimit()} items…`);
    try {
      await mutate("/api/connectors/scan", { provider, account, limit: scanLimit() });
      setFormStatus("connector-action-status", `Scan completed for ${account}. Ask ContextGate about the imported mailbox records or review source status.`);
      clearGlobalError();
    } catch (error) {
      const message = errorMessage(error);
      setFormStatus("connector-action-status", message, true);
      showGlobalError(`Mailbox scan failed for ${account}. ${message}`);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function disconnectConnectorAccount(provider, account, button) {
    if (!account) return;
    if (!window.confirm(`Remove ${account} from ContextGate? Existing audit receipts are not rewritten.`)) return;
    setButtonBusy(button, true, "Removing…");
    setFormStatus("connector-action-status", `Removing ${account}…`);
    try {
      await mutate("/api/connectors/disconnect", { provider, account });
      setFormStatus("connector-action-status", `${account} was disconnected. Existing receipts remain unchanged.`);
      clearGlobalError();
    } catch (error) {
      const message = errorMessage(error);
      setFormStatus("connector-action-status", message, true);
      showGlobalError(`Account removal failed for ${account}. ${message}`);
    } finally {
      setButtonBusy(button, false);
    }
  }

  function normalizedPublicWebsiteUrl(rawUrl) {
    let parsed;
    try {
      parsed = new URL(rawUrl);
    } catch {
      throw new Error("Enter a complete public URL, including https:// or http://.");
    }
    if (!["https:", "http:"].includes(parsed.protocol)) {
      throw new Error("Website sources must use an http:// or https:// URL.");
    }
    if (parsed.username || parsed.password) {
      throw new Error("Do not put usernames or passwords in a website-source URL.");
    }
    return parsed.href;
  }

  async function addWebsiteSource(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("button[type='submit']", form);
    const extractionGoal = $("#website-source-goal").value.trim();
    setFormStatus("website-source-status");
    if (extractionGoal.length < 3) {
      setFormStatus("website-source-status", "Describe the data ContextGate should collect from this page.", true);
      return;
    }

    let url;
    try {
      url = normalizedPublicWebsiteUrl($("#website-source-url").value.trim());
    } catch (error) {
      setFormStatus("website-source-status", errorMessage(error), true);
      return;
    }

    setButtonBusy(button, true, "Adding…");
    try {
      await mutate("/api/websites/add", { url, extraction_goal: extractionGoal });
      form.reset();
      setFormStatus("website-source-status", "Website added. Select Scan now when you want ContextGate to collect the requested public data.");
      clearGlobalError();
    } catch (error) {
      const message = errorMessage(error);
      setFormStatus("website-source-status", message, true);
      showGlobalError(`Website source was not added. ${message}`);
    } finally {
      setButtonBusy(button, false);
    }
  }

  async function scanWebsiteSource(source) {
    const sourceId = websiteSourceId(source);
    const sourceUrl = safeText(source?.url, "this website");
    if (!sourceId || view.websiteSourceOperations.has(sourceId)) return;
    view.websiteSourceOperations.set(sourceId, "scan");
    renderWebsiteSources();
    setFormStatus("website-source-status", `Scanning ${websiteSourceHost(sourceUrl)} on demand for the requested public data…`);
    try {
      const result = await mutate("/api/websites/scan", { source_id: sourceId });
      const events = Math.max(0, Number(result?.events_found) || 0);
      const records = Math.max(0, Number(result?.records_found) || 0);
      const detail = events
        ? `${events} structured event${events === 1 ? "" : "s"} entered the event catalog and can be queried in chat.`
        : `${records} bounded page evidence record${records === 1 ? " was" : "s were"} inspected; no structured event was added to event totals.`;
      setFormStatus("website-source-status", `Scan completed for ${websiteSourceHost(sourceUrl)}. ${detail}`);
      clearGlobalError();
    } catch (error) {
      const message = errorMessage(error);
      setFormStatus("website-source-status", message, true);
      showGlobalError(`Website scan failed for ${websiteSourceHost(sourceUrl)}. ${message}`);
    } finally {
      view.websiteSourceOperations.delete(sourceId);
      renderWebsiteSources();
    }
  }

  async function removeWebsiteSource(source) {
    const sourceId = websiteSourceId(source);
    const sourceUrl = safeText(source?.url, "this website");
    if (!sourceId || view.websiteSourceOperations.has(sourceId)) return;
    if (!window.confirm(`Remove ${sourceUrl} from website sources? Existing decision receipts are not rewritten.`)) return;
    view.websiteSourceOperations.set(sourceId, "remove");
    renderWebsiteSources();
    setFormStatus("website-source-status", `Removing ${websiteSourceHost(sourceUrl)}…`);
    try {
      await mutate("/api/websites/remove", { source_id: sourceId });
      setFormStatus("website-source-status", `${websiteSourceHost(sourceUrl)} was removed from configured website sources.`);
      clearGlobalError();
    } catch (error) {
      const message = errorMessage(error);
      setFormStatus("website-source-status", message, true);
      showGlobalError(`Website source removal failed for ${websiteSourceHost(sourceUrl)}. ${message}`);
    } finally {
      view.websiteSourceOperations.delete(sourceId);
      renderWebsiteSources();
    }
  }

  function exportSafeValue(value, depth = 0, seen = new WeakSet()) {
    if (value === null || value === undefined) return value ?? null;
    if (["string", "number", "boolean"].includes(typeof value)) return value;
    if (depth > 8) return "[nested value omitted]";
    if (typeof value !== "object") return String(value);
    if (seen.has(value)) return "[circular value omitted]";
    seen.add(value);
    if (Array.isArray(value)) return value.map((item) => exportSafeValue(item, depth + 1, seen));
    const result = {};
    Object.entries(value).forEach(([key, item]) => {
      if (/(^|_)(access_token|refresh_token|token|secret|password|client_json|authorization_code)($|_)/i.test(key)) {
        result[key] = "[credential omitted]";
      } else {
        result[key] = exportSafeValue(item, depth + 1, seen);
      }
    });
    return result;
  }

  function caseDataOrigin(item) {
    const explicit = safeText(item?.data_origin ?? item?.provenance_type ?? item?.dataset, "").toLowerCase();
    const source = `${explicit} ${caseSource(item)}`.toLowerCase();
    if (item?.fictional === true || /(fictional|synthetic|demo|bundled|scenario)/.test(source) || (item?.name && /^[ARB]\d+$/i.test(caseId(item)))) return "Fictional demo data";
    if (/(gmail|google|microsoft|outlook|mailbox|scan|upload|file intake)/.test(source)) return "Scanned/uploaded evidence";
    return "Unspecified local evidence";
  }

  function buildExportModel() {
    const company = view.state?.company ?? {};
    const totals = deriveTotals(view.state ?? normalizeState({}));
    const cases = (view.state?.cases ?? []).map((item) => ({
      case_id: caseId(item),
      title: caseTitle(item),
      data_origin: caseDataOrigin(item),
      deterministic_outcome: caseOriginalOutcome(item),
      effective_outcome: caseEffectiveOutcome(item),
      summary: caseSummary(item),
      source: caseSource(item),
      evidence: exportSafeValue(item.evidence ?? item.events ?? item.evidence_refs ?? item.contributors ?? null),
      rules: exportSafeValue(item.rules ?? item.rule_ids ?? item.failed_rules ?? null),
      correction: exportSafeValue(item.active_correction ?? item.correction ?? item.corrections ?? null),
      receipt: exportSafeValue(item.receipt ?? item.decision_record ?? item.decision ?? null),
      fingerprints: {
        request: safeText(item.request_fingerprint ?? item.request_digest, "Not supplied"),
        evidence: safeText(item.evidence_fingerprint ?? item.evidence_digest ?? item.decision?.evidence_fingerprint, "Not supplied"),
        policy: safeText(item.policy_fingerprint ?? item.policy?.fingerprint, "Not supplied"),
      },
    }));
    const originCounts = cases.reduce((counts, item) => {
      counts[item.data_origin] = (counts[item.data_origin] ?? 0) + 1;
      return counts;
    }, {});
    return {
      export_format: "ContextGate current-state evidence export",
      generated_at: new Date().toISOString(),
      action_boundary: "No external action was executed by this export.",
      company: {
        company_name: safeText(company.company_name ?? company.name, "Company workspace"),
        operator_name: safeText(company.operator_name, "Local operator"),
        important_detail: safeText(company.important_detail, "Not configured"),
        identity_fields: exportSafeValue(company.identity_fields ?? []),
        risk_posture: safeText(company.risk_posture, "safety_first"),
        source_mode: safeText(company.source_mode, "unspecified"),
        company_website: safeText(company.company_website, ""),
        document_company_header: company.document_company_header !== false,
        document_footer: safeText(company.document_footer, ""),
        company_logo_data_url: safeText(company.company_logo_data_url, ""),
        hidden_sources: exportSafeValue(company.hidden_sources ?? []),
        deleted_sources: exportSafeValue(company.deleted_sources ?? []),
      },
      policy: exportSafeValue(view.state?.policy ?? { risk_posture: company.risk_posture ?? "safety_first" }),
      totals,
      data_origin_counts: originCounts,
      patterns: exportSafeValue(normalizePatterns(view.state?.patterns ?? [])),
      source_summary: exportSafeValue(view.state?.source_summary ?? {}),
      tracking: exportSafeValue(view.state?.tracking ?? {}),
      grouped_metrics: exportSafeValue(view.state?.grouped_metrics ?? []),
      cases,
    };
  }

  function escapeHtml(value) {
    return String(value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function escapeXml(value) {
    return escapeHtml(value);
  }

  function downloadBlob(filename, type, content) {
    const url = URL.createObjectURL(new Blob([content], { type }));
    const anchor = document.createElement("a");
    anchor.href = url;
    anchor.download = filename;
    anchor.rel = "noopener";
    document.body.append(anchor);
    anchor.click();
    anchor.remove();
    window.setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  function exportFileStem(model) {
    const company = safeText(model.company.company_name, "company").toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 48) || "company";
    return `contextgate-${company}-${new Date().toISOString().slice(0, 10)}`;
  }

  function printableReport(model) {
    const logo = model.company.company_logo_data_url
      ? `<img class="company-logo" src="${escapeHtml(model.company.company_logo_data_url)}" alt="Company logo">`
      : "";
    const companyBlock = model.company.document_company_header
      ? `<h2>${escapeHtml(model.company.company_name)}</h2>${model.company.company_website ? `<p><a href="${escapeHtml(model.company.company_website)}">${escapeHtml(model.company.company_website)}</a></p>` : ""}`
      : "";
    const customFooter = model.company.document_footer
      ? `<footer>${escapeHtml(model.company.document_footer)}</footer>`
      : "";
    const caseSections = model.cases.map((item) => `
      <section class="case">
        <h3>${escapeHtml(item.case_id)} · ${escapeHtml(item.title)}</h3>
        <p class="origin">${escapeHtml(item.data_origin)}</p>
        <dl>
          <div><dt>Deterministic outcome</dt><dd>${escapeHtml(item.deterministic_outcome)}</dd></div>
          <div><dt>Effective outcome</dt><dd>${escapeHtml(item.effective_outcome)}</dd></div>
          <div><dt>Source</dt><dd>${escapeHtml(item.source)}</dd></div>
        </dl>
        <p>${escapeHtml(item.summary)}</p>
        <h4>Receipt, evidence, correction, and fingerprints</h4>
        <pre>${escapeHtml(JSON.stringify({ evidence: item.evidence, rules: item.rules, correction: item.correction, receipt: item.receipt, fingerprints: item.fingerprints }, null, 2))}</pre>
      </section>`).join("");
    const metricSections = (Array.isArray(model.grouped_metrics) ? model.grouped_metrics : []).map((dataset) => {
      const totals = dataset?.group_totals && typeof dataset.group_totals === "object" ? dataset.group_totals : {};
      const evidence = Array.isArray(dataset?.evidence) ? dataset.evidence : [];
      const rows = Object.entries(totals).map(([group, total]) => {
        const references = evidence
          .filter((item) => safeText(item?.group, "") === group)
          .map((item) => escapeHtml(safeText(item?.reference, "Not supplied")))
          .join("<br>");
        const unit = dataset?.unit ? ` ${escapeHtml(dataset.unit)}` : "";
        return `<tr><td>${escapeHtml(group)}</td><td>${escapeHtml(total)}${unit}</td><td>${references || "Not supplied"}</td></tr>`;
      }).join("");
      return `<section class="case"><h3>${escapeHtml(safeText(dataset?.dataset_name, "Grouped metric"))}</h3><p class="origin">${escapeHtml(safeText(dataset?.metric_field, "Metric"))} by ${escapeHtml(safeText(dataset?.group_field, "group"))} · ${escapeHtml(safeText(dataset?.row_count, evidence.length))} contributing rows</p><p>Source: <code>${escapeHtml(safeText(dataset?.source_reference, "Not supplied"))}</code></p><table><thead><tr><th>Group</th><th>Total</th><th>Evidence rows</th></tr></thead><tbody>${rows}</tbody></table></section>`;
    }).join("");
    const patterns = escapeHtml(JSON.stringify(model.patterns, null, 2));
    const policy = escapeHtml(JSON.stringify(model.policy, null, 2));
    const origins = Object.entries(model.data_origin_counts).map(([label, count]) => `<li>${escapeHtml(label)}: ${escapeHtml(count)}</li>`).join("");
    return `<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>ContextGate report</title>
<style>body{max-width:980px;margin:36px auto;padding:0 24px;color:#14212b;font:15px/1.5 system-ui,sans-serif}header{border-bottom:3px solid #087ca7;padding-bottom:18px}.company-logo{display:block;max-width:120px;max-height:72px;object-fit:contain}h1{margin:0;color:#052c42}h2{margin-top:32px}.meta{color:#4b6473}table{width:100%;border-collapse:collapse}th,td{padding:10px;border:1px solid #a9bbc6;text-align:left}.case{break-inside:avoid;border:1px solid #b6c6d0;border-left:4px solid #087ca7;margin:14px 0;padding:14px}.case h3{margin:0}.origin{color:#087ca7;font-weight:700}dl{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}dl div{background:#edf5f8;padding:8px}dt{font-size:12px;color:#536b79}dd{margin:3px 0 0;font-weight:700}pre{overflow-wrap:anywhere;white-space:pre-wrap;background:#f4f7f9;padding:10px;font-size:11px}footer{margin-top:28px;padding-top:10px;border-top:1px solid #a9bbc6;color:#4b6473}@media print{@page{margin:14mm}body{margin:0;max-width:none}}</style></head>
<body><header>${logo}${companyBlock}<h1>ContextGate evidence report</h1><p>Operator ${escapeHtml(model.company.operator_name)}</p><p class="meta">Generated ${escapeHtml(model.generated_at)} · ${escapeHtml(model.action_boundary)}</p></header>
<h2>Policy and company profile</h2><p>Important detail: ${escapeHtml(model.company.important_detail)} · Risk posture: ${escapeHtml(model.company.risk_posture)}</p><pre>${policy}</pre>
<h2>Resolved decision totals</h2><table><thead><tr><th>Total</th><th>Allow</th><th>Review</th><th>Block</th></tr></thead><tbody><tr><td>${escapeHtml(model.totals.TOTAL)}</td><td>${escapeHtml(model.totals.ALLOW)}</td><td>${escapeHtml(model.totals.REVIEW)}</td><td>${escapeHtml(model.totals.BLOCK)}</td></tr></tbody></table>
<h2>Data provenance</h2><ul>${origins || "<li>No cases loaded</li>"}</ul><h3>Source catalog summary</h3><pre>${escapeHtml(JSON.stringify(model.source_summary, null, 2))}</pre>${metricSections ? `<h2>Grouped metrics and row evidence</h2>${metricSections}` : ""}<h2>Patterns</h2><pre>${patterns}</pre><h2>Cases, receipts, and evidence</h2>${caseSections || "<p>No cases loaded.</p>"}${customFooter}</body></html>`;
  }

  function dashboardSvg(model) {
    const selected = selectedCase();
    const selectedLabel = selected ? `${caseId(selected)} · ${caseTitle(selected)}` : "No selected case";
    const selectedOutcome = selected ? caseEffectiveOutcome(selected) : "WAITING";
    const metricDatasets = Array.isArray(model.grouped_metrics) ? model.grouped_metrics : [];
    const activeTopicId = safeText(model.tracking?.active_topic_id, "");
    const metricDataset = [...metricDatasets].reverse().find((item) => activeTopicId && item?.topic_id === activeTopicId)
      ?? metricDatasets.at(-1)
      ?? null;
    const catalogLabel = (metricDataset ? metricDataset.fictional === true : model.source_summary?.fictional === true)
      ? "FICTIONAL DEMO DATA"
      : "SCANNED / UPLOADED DATA";
    const showBrand = model.company.document_company_header !== false;
    const brandName = showBrand ? safeText(model.company.company_name, "Company workspace") : "ContextGate workspace";
    const website = showBrand ? safeText(model.company.company_website, "") : "";
    const logo = showBrand && model.company.company_logo_data_url
      ? `<image x="1105" y="68" width="88" height="76" preserveAspectRatio="xMidYMid meet" href="${escapeXml(model.company.company_logo_data_url)}"/>`
      : "";
    const footer = safeText(model.company.document_footer, "No external action · local evidence report").slice(0, 110);
    let chartTitle = "Resolved decision signal";
    let detailBlock = `<rect x="90" y="548" width="1090" height="86" rx="10" fill="#0c2232" stroke="#205875"/><text x="112" y="580" fill="#7890a3" font-size="14" font-family="monospace">SELECTED CASE · ${escapeXml(selectedOutcome)}</text><text x="112" y="612" fill="#f4fbff" font-size="22" font-family="system-ui" font-weight="700">${escapeXml(selectedLabel)}</text><text x="980" y="612" fill="#38d8ff" font-size="18" font-family="monospace">TOTAL ${model.totals.TOTAL}</text>`;
    let bars;
    if (metricDataset) {
      const entries = Object.entries(metricDataset.group_totals ?? {}).slice(0, 5);
      const maximum = Math.max(1, ...entries.map(([, value]) => Math.abs(Number(value)) || 0));
      chartTitle = `${safeText(metricDataset.metric_field, "Metric")} by ${safeText(metricDataset.group_field, "group")}`;
      bars = entries.map(([label, count], index) => {
        const numeric = Number(count);
        const width = Math.round((Math.abs(numeric) / maximum) * 650);
        const y = 298 + index * 54;
        const color = numeric < 0 ? OUTCOME_COLORS.BLOCK : OUTCOME_COLORS.ALLOW;
        return `<text x="90" y="${y}" fill="#93acbd" font-size="17" font-family="monospace">${escapeXml(label.slice(0, 24))}</text><rect x="350" y="${y - 18}" width="650" height="24" rx="5" fill="#10283a"/><rect x="350" y="${y - 18}" width="${width}" height="24" rx="5" fill="${color}"/><text x="1020" y="${y}" fill="#f4fbff" font-size="21" font-weight="700" font-family="monospace">${escapeXml(count)}${metricDataset.unit ? ` ${escapeXml(metricDataset.unit)}` : ""}</text>`;
      }).join("");
      detailBlock = `<rect x="90" y="574" width="1090" height="60" rx="10" fill="#0c2232" stroke="#205875"/><text x="112" y="599" fill="#7890a3" font-size="14" font-family="monospace">${escapeXml(safeText(metricDataset.dataset_name, "GROUPED METRIC").toUpperCase())} · ${escapeXml(metricDataset.row_count)} EVIDENCE ROWS</text><text x="112" y="621" fill="#38d8ff" font-size="13" font-family="monospace">${escapeXml(safeText(metricDataset.source_reference, "Not supplied").slice(0, 116))}</text>`;
    } else {
      const total = Math.max(1, model.totals.TOTAL);
      bars = [
        ["ALLOW", model.totals.ALLOW, OUTCOME_COLORS.ALLOW],
        ["REVIEW", model.totals.REVIEW, OUTCOME_COLORS.REVIEW],
        ["BLOCK", model.totals.BLOCK, OUTCOME_COLORS.BLOCK],
      ].map(([label, count, color], index) => {
        const width = Math.round((count / total) * 650);
        const y = 290 + index * 82;
        return `<text x="90" y="${y}" fill="#93acbd" font-size="18" font-family="monospace">${label}</text><rect x="205" y="${y - 19}" width="650" height="24" rx="5" fill="#10283a"/><rect x="205" y="${y - 19}" width="${width}" height="24" rx="5" fill="${color}"/><text x="875" y="${y}" fill="#f4fbff" font-size="22" font-weight="700" font-family="monospace">${count}</text>`;
      }).join("");
    }
    return `<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" viewBox="0 0 1280 720" role="img" aria-label="ContextGate dashboard export"><defs><linearGradient id="bg" x1="0" y1="0" x2="1" y2="1"><stop stop-color="#03070d"/><stop offset="1" stop-color="#0a1b2a"/></linearGradient><pattern id="grid" width="40" height="40" patternUnits="userSpaceOnUse"><path d="M40 0H0V40" fill="none" stroke="#18415a" stroke-opacity=".28"/></pattern></defs><rect width="1280" height="720" fill="url(#bg)"/><rect width="1280" height="720" fill="url(#grid)"/><rect x="48" y="45" width="1184" height="630" rx="18" fill="#07131f" stroke="#277a9d"/><text x="84" y="100" fill="#38d8ff" font-size="19" font-family="monospace" letter-spacing="3">CONTEXTGATE CONTROL ROOM</text><text x="84" y="148" fill="#f4fbff" font-size="34" font-family="system-ui" font-weight="700">${escapeXml(brandName)}</text>${website ? `<text x="84" y="177" fill="#38d8ff" font-size="15" font-family="system-ui">${escapeXml(website)}</text>` : ""}<text x="84" y="204" fill="#8ba2b4" font-size="17" font-family="system-ui">Operator: ${escapeXml(model.company.operator_name)} · ${escapeXml(model.generated_at)}</text>${logo}<text x="915" y="169" fill="#39e6a0" font-size="17" font-family="monospace">NO EXTERNAL ACTION</text><text x="84" y="229" fill="#38d8ff" font-size="14" font-family="monospace">${escapeXml(catalogLabel)}</text><text x="90" y="262" fill="#d5e5ef" font-size="22" font-family="system-ui" font-weight="700">${escapeXml(chartTitle)}</text>${bars}${detailBlock}<text x="84" y="658" fill="#7890a3" font-size="13" font-family="system-ui">${escapeXml(footer)}</text></svg>`;
  }

  function prepareEmailSummary(model) {
    const selected = selectedCase();
    const subject = `ContextGate summary · ${model.company.company_name}`;
    const lines = [
      `ContextGate control-room summary`,
      `Company: ${model.company.company_name}`,
      `Operator: ${model.company.operator_name}`,
      `Policy posture: ${model.company.risk_posture}`,
      `Totals: ${model.totals.ALLOW} ALLOW / ${model.totals.REVIEW} REVIEW / ${model.totals.BLOCK} BLOCK (${model.totals.TOTAL} total)`,
      `Selected case: ${selected ? `${caseId(selected)} · ${caseTitle(selected)} · ${caseEffectiveOutcome(selected)}` : "None"}`,
      ``,
      `Data includes explicit fictional/scanned provenance labels in the exported attachments.`,
      `Nothing has been sent automatically. Add the downloaded report, JSON, or SVG attachments manually before sending.`,
    ];
    window.location.href = `mailto:?subject=${encodeURIComponent(subject)}&body=${encodeURIComponent(lines.join("\n"))}`;
  }

  function runLocalExport(successMessage, action) {
    try {
      action();
      setFormStatus("export-status", successMessage);
      clearGlobalError();
    } catch (error) {
      const message = errorMessage(error, "The local export could not be prepared.");
      setFormStatus("export-status", message, true);
      showGlobalError(`Export failed. ${message}`);
    }
  }

  function bindDialogs() {
    $$('[data-open-dialog]').forEach((button) => {
      button.addEventListener("click", () => showDialog(button.dataset.openDialog));
    });
    $$('[data-close-dialog]').forEach((button) => {
      button.addEventListener("click", () => button.closest("dialog")?.close());
    });
    $$("dialog").forEach((dialog) => {
      dialog.addEventListener("click", (event) => {
        if (event.target === dialog) dialog.close();
      });
    });
    $("#open-case-details").addEventListener("click", () => showDialog("case-dialog"));
  }

  function bindQueueFilters() {
    $$('[data-queue-filter]').forEach((button) => {
      button.addEventListener("click", () => {
        view.activeFilter = button.dataset.queueFilter;
        $$('[data-queue-filter]').forEach((filterButton) => {
          filterButton.setAttribute("aria-pressed", String(filterButton === button));
        });
        renderQueue();
      });
    });
  }

  function profilePayload() {
    return {
      company_name: $("#profile-company-name").value.trim(),
      operator_name: $("#profile-operator-name").value.trim(),
      company_website: $("#profile-company-website").value.trim(),
      important_detail: $("#profile-important-detail").value.trim(),
      identity_fields: $("#profile-identity-fields").value
        .split(",")
        .map((value) => value.trim())
        .filter(Boolean),
      risk_posture: $("#profile-risk-posture").value,
      voice_enabled: $("#voice-enabled").checked,
      mail_scan_limit: scanLimit(),
      auto_monitor_enabled: $("#auto-monitor-enabled").checked,
      auto_monitor_minutes: autoMonitorMinutes(),
      document_company_header: $("#document-company-header").checked,
      document_footer: $("#document-footer").value.trim(),
    };
  }

  function bindForms() {
    $("#chat-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = $("#chat-input").value.trim();
      if (!message) return;
      const button = $("#chat-submit");
      const historyLength = view.state.chat_history.length;
      $("#chat-error").hidden = true;
      setButtonBusy(button, true, "Asking…");
      try {
        const payload = await mutate("/api/chat", {
          message,
          save_guidance: $("#save-guidance").checked,
        });
        $("#chat-input").value = "";
        $("#save-guidance").checked = false;
        if (!looksLikeState(payload) && payload?.reply && !view.state.chat_history.some((item) => messageText(item) === safeText(payload.reply))) {
          view.state.chat_history.push({ role: "assistant", content: payload.reply });
          renderChat();
        }
        if (view.state.chat_history.length > historyLength) speakAssistantReply();
        clearGlobalError();
      } catch (error) {
        const messageTextValue = errorMessage(error);
        $("#chat-error").textContent = messageTextValue;
        $("#chat-error").hidden = false;
        showGlobalError(`Chat request failed. ${messageTextValue}`);
      } finally {
        setButtonBusy(button, false);
      }
    });

    $("#profile-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type='submit']", event.currentTarget);
      setButtonBusy(button, true, "Saving…");
      setFormStatus("profile-status");
      try {
        await mutate("/api/profile", profilePayload());
        setFormStatus("profile-status", "Display profile saved. Enforcement remains governed by the active validated policy.");
        scheduleAutoMonitor();
        clearGlobalError();
      } catch (error) {
        const message = errorMessage(error);
        setFormStatus("profile-status", message, true);
        showGlobalError(`Company profile was not saved. ${message}`);
      } finally {
        setButtonBusy(button, false);
      }
    });

    $("#operator-settings-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type='submit']", event.currentTarget);
      setButtonBusy(button, true, "Saving…");
      setFormStatus("local-settings-status");
      try {
        await mutate("/api/profile", profilePayload());
        setFormStatus("local-settings-status", "Operator settings saved by the company-profile API.");
        scheduleAutoMonitor();
        if (!voiceEnabled() && "speechSynthesis" in window) window.speechSynthesis.cancel();
        clearGlobalError();
      } catch (error) {
        const message = errorMessage(error);
        setFormStatus("local-settings-status", message, true);
        showGlobalError(`Operator settings were not saved. ${message}`);
      } finally {
        setButtonBusy(button, false);
      }
    });

    $("#upload-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const form = event.currentTarget;
      const button = $("button[type='submit']", form);
      const file = $("#upload-file").files?.[0];
      setFormStatus("upload-form-status");
      if (!file) {
        setFormStatus("upload-form-status", "Choose a file first.", true);
        return;
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        setFormStatus("upload-form-status", "The selected file is larger than the 10 MiB interface limit.", true);
        return;
      }
      setButtonBusy(button, true, "Uploading…");
      try {
        const dataBase64 = await fileToBase64(file);
        const result = await mutate("/api/upload", {
          filename: file.name,
          content_type: file.type || "application/octet-stream",
          data_base64: dataBase64,
        });
        form.reset();
        const receipt = (result?.state ?? result)?.source_status?.last_upload;
        const metricRows = Number(receipt?.grouped_metric_rows) || 0;
        const metricError = safeText(receipt?.grouped_metric_error, "");
        if (metricRows) {
          setFormStatus("upload-form-status", `${file.name} was accepted and ${metricRows} structured metric row${metricRows === 1 ? " was" : "s were"} grouped with evidence.`);
        } else if (metricError) {
          setFormStatus("upload-form-status", `${file.name} was accepted as local evidence, but no grouped totals were created: ${metricError}`, true);
        } else {
          setFormStatus("upload-form-status", `${file.name} was accepted by the local intake API.`);
        }
        clearGlobalError();
      } catch (error) {
        const message = errorMessage(error);
        setFormStatus("upload-form-status", message, true);
        showGlobalError(`Upload failed. ${message}`);
      } finally {
        setButtonBusy(button, false);
      }
    });

    $("#correction-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const item = selectedCase();
      if (!item) return;
      const correctedOutcome = $("#corrected-outcome").value;
      if (correctedOutcome === caseOriginalOutcome(item)) {
        setFormStatus("correction-status", "Choose an outcome different from the original deterministic receipt.", true);
        return;
      }
      const button = $("button[type='submit']", event.currentTarget);
      setButtonBusy(button, true, "Recording…");
      setFormStatus("correction-status");
      try {
        await mutate("/api/correct", {
          case_id: caseId(item),
          corrected_outcome: correctedOutcome,
          reviewer: $("#correction-reviewer").value.trim(),
          rationale: $("#correction-rationale").value.trim(),
        });
        setFormStatus("correction-status", "Human correction recorded. The original receipt is preserved.");
        $("#correction-rationale").value = "";
        renderCaseDialog();
        clearGlobalError();
      } catch (error) {
        const message = errorMessage(error);
        setFormStatus("correction-status", message, true);
        showGlobalError(`Correction was not recorded. ${message}`);
      } finally {
        setButtonBusy(button, false);
      }
    });

    $("#retraction-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const item = selectedCase();
      if (!item) return;
      const form = event.currentTarget;
      const button = $("button[type='submit']", form);
      setButtonBusy(button, true, "Retracting…");
      setFormStatus("retraction-status");
      try {
        await mutate("/api/retract", {
          case_id: caseId(item),
          reason: $("#retraction-reason").value.trim(),
        });
        form.reset();
        setFormStatus("retraction-status", "Correction retracted. The append-only history remains available in the receipt.");
        renderCaseDialog();
        clearGlobalError();
      } catch (error) {
        const message = errorMessage(error);
        setFormStatus("retraction-status", message, true);
        showGlobalError(`Correction was not retracted. ${message}`);
      } finally {
        setButtonBusy(button, false);
      }
    });

    $("#google-configure-form").addEventListener("submit", configureGoogle);
    $("#microsoft-configure-form").addEventListener("submit", configureMicrosoft);
    $("#website-source-form").addEventListener("submit", addWebsiteSource);
    $("#layout-form").addEventListener("submit", (event) => {
      event.preventDefault();
      applyLayout({ persist: true });
    });
  }

  function updateClock() {
    $("#system-clock").textContent = new Intl.DateTimeFormat(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).format(new Date());
  }

  function bindStaticControls() {
    $("#dismiss-alert").addEventListener("click", clearGlobalError);
    $("#calendar-previous").addEventListener("click", () => moveCalendarMonth(-1));
    $("#calendar-next").addEventListener("click", () => moveCalendarMonth(1));
    $("#calendar-today").addEventListener("click", () => {
      const today = new Date();
      view.calendarMonth = new Date(today.getFullYear(), today.getMonth(), 1);
      const todayEvent = calendarEvents().find((item) => item.date_iso === calendarDateKey(today));
      view.selectedCalendarEventId = todayEvent ? calendarEventId(todayEvent) : null;
      view.calendarSelectionInitialized = true;
      renderCalendar();
    });
    $("#refresh-state").addEventListener("click", async () => {
      await refreshState();
      await checkHealth({ reportError: false });
    });
    $("#upload-company-logo").addEventListener("click", (event) => saveCompanyLogo(event.currentTarget));
    $("#remove-company-logo").addEventListener("click", (event) => removeCompanyLogo(event.currentTarget));
    $$('[data-quick-prompt]').forEach((button) => {
      button.addEventListener("click", () => {
        $("#chat-input").value = button.dataset.quickPrompt;
        $("#chat-input").focus();
      });
    });
    $$('[data-start-connector]').forEach((button) => {
      button.addEventListener("click", () => startConnector(button.dataset.startConnector, button));
    });
    $("#layout-preset").addEventListener("change", (event) => {
      const preset = LAYOUT_PRESETS[event.currentTarget.value];
      if (!preset) return;
      $("#rail-width").value = String(preset.railWidth);
      $("#chat-width").value = String(preset.chatWidth);
      updateLayoutOutputs();
    });
    [$("#rail-width"), $("#chat-width")].forEach((control) => {
      control.addEventListener("input", () => {
        $("#layout-preset").value = "custom";
        updateLayoutOutputs();
      });
    });
    $("#voice-enabled").addEventListener("change", (event) => {
      if (event.currentTarget.checked && !("speechSynthesis" in window)) {
        event.currentTarget.checked = false;
        setFormStatus("local-settings-status", "Voice synthesis is not available in this browser.", true);
        return;
      }
      setFormStatus("local-settings-status", "Unsaved change. Select Save operator settings.");
      if (!event.currentTarget.checked && "speechSynthesis" in window) window.speechSynthesis.cancel();
    });
    $("#scan-limit").addEventListener("change", (event) => {
      const normalized = Math.min(25, Math.max(1, Number.parseInt(event.currentTarget.value, 10) || 25));
      event.currentTarget.value = String(normalized);
      setFormStatus("local-settings-status", "Unsaved change. Select Save operator settings.");
    });
    $("#auto-monitor-enabled").addEventListener("change", () => {
      $("#auto-monitor-now").disabled = true;
      setFormStatus("local-settings-status", "Unsaved monitoring change. Select Save operator settings.");
      setAutoMonitorStatus("Save operator settings to apply the monitoring change.");
    });
    $("#auto-monitor-minutes").addEventListener("change", (event) => {
      const normalized = Math.min(1440, Math.max(1, Number.parseInt(event.currentTarget.value, 10) || 15));
      event.currentTarget.value = String(normalized);
      setFormStatus("local-settings-status", "Unsaved monitoring change. Select Save operator settings.");
      setAutoMonitorStatus("Save operator settings to apply the monitoring interval.");
    });
    $("#auto-monitor-now").addEventListener("click", () => runAutoMonitor({ manual: true }));
    [$("#document-company-header"), $("#document-footer")].forEach((control) => {
      control.addEventListener("change", () => {
        setFormStatus("local-settings-status", "Unsaved change. Select Save operator settings.");
      });
    });
    $("#demo-reset").addEventListener("click", async (event) => {
      if (!window.confirm("Reset the fictional demo workspace? Company memory, connector credentials, and audit retention are outside this reset.")) return;
      const button = event.currentTarget;
      setButtonBusy(button, true, "Resetting…");
      setFormStatus("demo-reset-status");
      try {
        await mutate("/api/demo/reset", {});
        setFormStatus("demo-reset-status", "Fictional demo data reset. No claim is made about company memory, connector credentials, or audit records.");
        clearGlobalError();
      } catch (error) {
        const message = errorMessage(error);
        setFormStatus("demo-reset-status", message, true);
        showGlobalError(`Fictional demo reset failed. ${message}`);
      } finally {
        setButtonBusy(button, false);
      }
    });
    $("#export-html").addEventListener("click", () => {
      runLocalExport("Printable HTML report prepared locally.", () => {
        const model = buildExportModel();
        downloadBlob(`${exportFileStem(model)}-report.html`, "text/html;charset=utf-8", printableReport(model));
      });
    });
    $("#export-json").addEventListener("click", () => {
      runLocalExport("JSON data and receipts prepared locally.", () => {
        const model = buildExportModel();
        downloadBlob(`${exportFileStem(model)}-receipts.json`, "application/json;charset=utf-8", `${JSON.stringify(model, null, 2)}\n`);
      });
    });
    $("#export-svg").addEventListener("click", () => {
      runLocalExport("SVG dashboard graphic prepared locally.", () => {
        const model = buildExportModel();
        downloadBlob(`${exportFileStem(model)}-dashboard.svg`, "image/svg+xml;charset=utf-8", dashboardSvg(model));
      });
    });
    $("#print-dashboard").addEventListener("click", () => {
      $("#export-dialog").close();
      window.setTimeout(() => window.print(), 0);
    });
    $("#email-summary").addEventListener("click", () => {
      runLocalExport("Email draft requested. Nothing was sent; add attachments manually.", () => {
        prepareEmailSummary(buildExportModel());
      });
    });
    bindDialogs();
    bindQueueFilters();
    bindForms();
  }

  async function start() {
    initializeLayout();
    bindStaticControls();
    updateClock();
    window.setInterval(updateClock, 1000);
    await refreshState();
    await checkHealth();
    scheduleAutoMonitor();
  }

  let resizeFrame = 0;

  window.addEventListener("DOMContentLoaded", start, { once: true });
  window.addEventListener("pagehide", stopAutoMonitorTimer);
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) scheduleAutoMonitor();
  });
  window.addEventListener("resize", () => {
    if (!$("#layout-form")) return;
    window.cancelAnimationFrame(resizeFrame);
    resizeFrame = window.requestAnimationFrame(() => applyLayout());
  });
})();
