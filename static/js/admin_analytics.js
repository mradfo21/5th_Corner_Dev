/**
 * admin_analytics.js — Cost & Usage Analytics tab for the admin dashboard.
 *
 * Loaded after admin_dashboard.html's inline <script>, so it reuses these
 * globals defined there instead of redefining them:
 *   ADMIN_TOKEN, API_BASE, adminFetch(), escapeHtml(), showError(), showSuccess()
 *
 * See ADMIN_COST_ANALYTICS_DASHBOARD_PLAN.md for the design record and
 * cost_tracker.py / pricing.py for the backing data model.
 */
(function () {
    'use strict';

    const COLORS = {
        red: '#dc143c',
        darkRed: '#8b0000',
        offWhite: '#e0e0e0',
        dim: 'rgba(224, 224, 224, 0.55)',
        grid: 'rgba(220, 20, 60, 0.15)',
        success: '#4caf50',
        warning: '#ff9800',
    };

    // Stable-ish palette for N providers/services in charts.
    const PALETTE = ['#dc143c', '#ff9800', '#4caf50', '#4ecdc4', '#8b5cf6', '#e0e0e0', '#607d8b', '#f06292'];

    const state = {
        range: '7d',
        charts: {},
        pricing: null,
    };

    function fmtUsd(n) {
        if (n === null || n === undefined) return '—';
        const v = Number(n);
        if (Number.isNaN(v)) return '—';
        if (v === 0) return '$0.00';
        if (Math.abs(v) < 0.01) return `$${v.toFixed(4)}`;
        return `$${v.toFixed(2)}`;
    }

    function fmtPct(n) {
        if (n === null || n === undefined) return '—';
        return `${(Number(n) * 100).toFixed(1)}%`;
    }

    function colorFor(key, index) {
        return PALETTE[index % PALETTE.length];
    }

    async function fetchJson(path) {
        const res = await adminFetch(`${API_BASE}${path}`);
        if (!res.ok) {
            const body = await res.json().catch(() => ({}));
            throw new Error(body.error || `HTTP ${res.status}`);
        }
        const body = await res.json();
        return body.data !== undefined ? body.data : body;
    }

    // ─────────────────────────── KPI cards ───────────────────────────

    function renderKpis(summary) {
        document.getElementById('kpi-total-spend').textContent = fmtUsd(summary.total_cost_usd);
        // Fold "projected monthly" and "unpriced calls" in as a note instead
        // of their own cards — only shown when they add information (a
        // projection needs a bounded range; an unpriced flag only matters
        // when it's non-zero).
        const notes = [];
        if (summary.projected_monthly_usd) {
            notes.push(`≈ ${fmtUsd(summary.projected_monthly_usd)}/mo projected`);
        }
        if (summary.unpriced_event_count) {
            notes.push(`${summary.unpriced_event_count} unpriced call${summary.unpriced_event_count === 1 ? '' : 's'}`);
        }
        document.getElementById('kpi-total-spend-note').textContent = notes.join(' · ');

        document.getElementById('kpi-spend-today').textContent = fmtUsd(summary.spend_today_usd);
        document.getElementById('kpi-avg-session').textContent = fmtUsd(summary.avg_cost_per_session_usd);
        document.getElementById('kpi-error-rate').textContent = fmtPct(summary.error_rate);
        document.getElementById('kpi-error-count-note').textContent =
            summary.error_count ? `${summary.error_count} failed call${summary.error_count === 1 ? '' : 's'}` : '';
    }

    // ────────────────────── Storage health banner ──────────────────────
    // There's no way to ask Render "is my disk actually attached" from the
    // browser, so cost_tracker.get_storage_health() combines a mount-point
    // check with "does the oldest ledger row predate this process's start"
    // — the strongest available proof the ledger survived a restart. This
    // turns "why did my totals reset again" from a mystery into an answer.
    function renderStorageHealth(health) {
        const el = document.getElementById('storage-health-banner');
        if (!health) { el.style.display = 'none'; return; }
        el.style.display = 'flex';
        if (health.survived_restart === true) {
            el.className = 'storage-health-banner is-good';
            el.innerHTML = `<span class="shb-icon">✅</span><span><strong>Persistent storage confirmed</strong> — the cost ledger has already survived at least one restart. Numbers here are real running totals, not "since the last deploy."</span>`;
        } else if (!health.mount_detected) {
            el.className = 'storage-health-banner is-bad';
            el.innerHTML = `<span class="shb-icon">⚠️</span><span><strong>Persistent disk NOT detected</strong> — <code>${escapeHtml(health.db_path)}</code> is on ephemeral storage. Everything below will be WIPED on the next deploy or restart. In the Render dashboard: Settings → Disks → Add Disk, mount path <code>/opt/render/project/src/sessions</code> (matches the <code>disk:</code> block already committed in render.yaml — Render may just need this approved/synced once).</span>`;
        } else {
            el.className = 'storage-health-banner is-pending';
            el.innerHTML = `<span class="shb-icon">⏳</span><span><strong>Persistent disk detected</strong> — looks correctly mounted. Can't fully confirm it survives a restart until one actually happens; check back after the next deploy and this should flip to a green confirmation.</span>`;
        }
    }

    // ─────────────────────────── Charts ───────────────────────────

    function destroyChart(key) {
        if (state.charts[key]) {
            state.charts[key].destroy();
            delete state.charts[key];
        }
    }

    function renderTimeseriesChart(ts) {
        destroyChart('timeseries');
        const ctx = document.getElementById('chart-timeseries');
        if (!ctx || typeof Chart === 'undefined') return;

        const buckets = ts.buckets || [];
        const labels = buckets.map(b => b.bucket);
        const serviceTypes = Array.from(new Set(buckets.flatMap(b => Object.keys(b.by_service || {}))));

        const datasets = serviceTypes.map((svc, i) => ({
            label: svc,
            data: buckets.map(b => (b.by_service || {})[svc] || 0),
            backgroundColor: colorFor(svc, i),
            borderColor: colorFor(svc, i),
            fill: true,
            tension: 0.25,
        }));

        state.charts.timeseries = new Chart(ctx, {
            type: 'line',
            data: { labels, datasets },
            options: {
                responsive: true,
                interaction: { mode: 'index', intersect: false },
                scales: {
                    x: { ticks: { color: COLORS.dim, font: { family: 'Share Tech Mono' } }, grid: { color: COLORS.grid } },
                    y: {
                        stacked: true,
                        ticks: { color: COLORS.dim, callback: (v) => `$${v}` },
                        grid: { color: COLORS.grid },
                    },
                },
                plugins: {
                    legend: { labels: { color: COLORS.offWhite, font: { family: 'Share Tech Mono', size: 10 } } },
                    tooltip: { callbacks: { label: (c) => `${c.dataset.label}: ${fmtUsd(c.raw)}` } },
                },
            },
        });
    }

    function renderProviderDonut(summary) {
        destroyChart('providerDonut');
        const ctx = document.getElementById('chart-provider-donut');
        if (!ctx || typeof Chart === 'undefined') return;

        const rows = (summary.cost_by_provider || []).filter(r => (r.cost_usd || 0) > 0);
        if (!rows.length) {
            state.charts.providerDonut = new Chart(ctx, {
                type: 'doughnut',
                data: { labels: ['No priced spend yet'], datasets: [{ data: [1], backgroundColor: ['#333'] }] },
                options: { plugins: { legend: { display: false }, tooltip: { enabled: false } } },
            });
            return;
        }

        state.charts.providerDonut = new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: rows.map(r => r.provider),
                datasets: [{
                    data: rows.map(r => r.cost_usd),
                    backgroundColor: rows.map((r, i) => colorFor(r.provider, i)),
                    borderColor: '#1a1a1a',
                }],
            },
            options: {
                plugins: {
                    legend: { position: 'bottom', labels: { color: COLORS.offWhite, font: { family: 'Share Tech Mono', size: 10 } } },
                    tooltip: { callbacks: { label: (c) => `${c.label}: ${fmtUsd(c.raw)}` } },
                },
            },
        });
    }

    // ─────────────────────────── Tables ───────────────────────────

    function renderProvidersTable(payload) {
        const rows = payload.providers || [];
        const el = document.getElementById('analytics-providers-table');
        if (!rows.length) {
            el.innerHTML = '<div class="loading">No usage recorded yet.</div>';
            return;
        }
        el.innerHTML = `
            <table class="sessions-table">
                <thead><tr>
                    <th>Provider</th><th>Model</th><th>Type</th><th>Calls</th><th>Cost</th>
                </tr></thead>
                <tbody>
                    ${rows.map(r => `
                        <tr>
                            <td>${escapeHtml(r.provider)}</td>
                            <td>${escapeHtml(r.model)}</td>
                            <td>${escapeHtml(r.service_type)}</td>
                            <td>${r.n}</td>
                            <td class="cost-cell">${fmtUsd(r.cost)}${r.unpriced ? ' <span class="flag-tag">unpriced</span>' : ''}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>`;
    }

    function serviceBreakdownPills(costByService) {
        const entries = Object.entries(costByService || {}).sort((a, b) => b[1] - a[1]);
        const shown = entries.slice(0, 3)
            .map(([k, v]) => `<span class="cost-breakdown-pill">${escapeHtml(k)} ${fmtUsd(v)}</span>`)
            .join('');
        const rest = entries.length - 3;
        return shown + (rest > 0 ? `<span class="cost-breakdown-pill">+${rest} more</span>` : '');
    }

    function renderSessionsTable(payload) {
        const rows = payload.sessions || [];
        const el = document.getElementById('analytics-sessions-table');
        if (!rows.length) {
            el.innerHTML = '<div class="loading">No cost data yet — play a session to see it here.</div>';
            return;
        }
        el.innerHTML = `
            <table class="sessions-table">
                <thead><tr>
                    <th>Session</th><th>Total Cost</th><th>By Service</th><th>Calls</th><th>Last Activity</th>
                </tr></thead>
                <tbody>
                    ${rows.map(r => `
                        <tr onclick="AdminAnalytics.openCostModal('${escapeAttr(r.session_id)}')">
                            <td>${escapeHtml(r.session_id)}</td>
                            <td class="cost-cell">${fmtUsd(r.total_cost_usd)}${r.unpriced_event_count ? ' <span class="flag-tag">+unpriced</span>' : ''}</td>
                            <td>${serviceBreakdownPills(r.cost_by_service)}</td>
                            <td>${r.event_count}${r.error_count ? ` <span class="flag-tag">(${r.error_count} failed)</span>` : ''}</td>
                            <td>${r.last_event_ts ? new Date(r.last_event_ts).toLocaleString() : '—'}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>`;
    }

    function renderErrorsTable(payload) {
        const rows = payload.errors || [];
        const section = document.getElementById('analytics-errors-section');
        const el = document.getElementById('analytics-errors-table');
        // Nothing wrong -> no section at all. An empty "no errors" table is
        // exactly the low-value info this cleanup is trying to cut.
        if (!rows.length) {
            section.style.display = 'none';
            return;
        }
        section.style.display = 'block';
        el.innerHTML = `
            <table class="sessions-table">
                <thead><tr>
                    <th>When</th><th>Session</th><th>Provider</th><th>Model</th><th>Service</th><th>Error</th>
                </tr></thead>
                <tbody>
                    ${rows.map(r => `
                        <tr class="error-row">
                            <td>${new Date(r.ts).toLocaleString()}</td>
                            <td>${escapeHtml(r.session_id)}</td>
                            <td>${escapeHtml(r.provider)}</td>
                            <td>${escapeHtml(r.model)}</td>
                            <td>${escapeHtml(r.service_type)}</td>
                            <td>${escapeHtml((r.error_message || '').slice(0, 140))}</td>
                        </tr>
                    `).join('')}
                </tbody>
            </table>`;
    }

    function renderPricingTable(pricingData) {
        state.pricing = pricingData;
        const rates = (pricingData && pricingData.rates) || {};
        // Only show rates for providers/models actually seen in usage, plus
        // any row already showing "unpriced" — the full table (every preset
        // model across every provider, most never used) is exactly the kind
        // of low-signal clutter this view should avoid. Falls back to
        // showing everything if we haven't loaded provider usage yet.
        const relevantKeys = state.knownProviderModelKeys;
        const keys = Object.keys(rates)
            .filter(k => !relevantKeys || relevantKeys.has(k))
            .sort();
        const el = document.getElementById('analytics-pricing-table');
        if (!keys.length) {
            el.innerHTML = '<div class="loading">No priced providers in this range yet.</div>';
            return;
        }
        el.innerHTML = `
            <table class="sessions-table pricing-table">
                <thead><tr>
                    <th>Provider : Model</th><th>Unit</th><th>Rate</th><th></th>
                </tr></thead>
                <tbody>
                    ${keys.map(key => {
                        const rate = rates[key] || {};
                        let fields = '';
                        if (rate.unit_type === 'tokens') {
                            fields = `
                                in <input type="number" step="0.0001" data-key="${escapeAttr(key)}" data-field="input_per_1k" value="${rate.input_per_1k ?? ''}" placeholder="null">
                                out <input type="number" step="0.0001" data-key="${escapeAttr(key)}" data-field="output_per_1k" value="${rate.output_per_1k ?? ''}" placeholder="null">`;
                        } else if (rate.unit_type === 'characters') {
                            fields = `<input type="number" step="0.0001" data-key="${escapeAttr(key)}" data-field="per_1k" value="${rate.per_1k ?? ''}" placeholder="null">`;
                        } else {
                            fields = `<input type="number" step="0.0001" data-key="${escapeAttr(key)}" data-field="per_unit" value="${rate.per_unit ?? ''}" placeholder="null">`;
                        }
                        return `
                            <tr>
                                <td>${escapeHtml(key)}</td>
                                <td>${escapeHtml(rate.unit_type || '')}</td>
                                <td>${fields}</td>
                                <td><button class="btn btn-small btn-secondary" onclick="AdminAnalytics.savePricingRow('${escapeAttr(key)}')">Save</button></td>
                            </tr>`;
                    }).join('')}
                </tbody>
            </table>`;
    }

    function escapeAttr(text) {
        return String(text == null ? '' : text)
            .replace(/&/g, '&amp;').replace(/"/g, '&quot;')
            .replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    // ─────────────────────────── Cost drill-down modal ───────────────────────────

    async function openCostModal(sessionId) {
        const modal = document.getElementById('cost-modal');
        const title = document.getElementById('cost-modal-title');
        const body = document.getElementById('cost-modal-body');
        title.textContent = `Session Cost Detail — ${sessionId}`;
        body.innerHTML = '<div class="loading">Loading…</div>';
        modal.classList.add('show');
        try {
            const detail = await fetchJson(`/admin/analytics/sessions/${encodeURIComponent(sessionId)}`);
            const rollup = detail.rollup || {};
            const events = detail.events || [];
            body.innerHTML = `
                <div class="stats-grid" style="margin-bottom:20px;">
                    <div class="stat-card">
                        <div class="stat-label">Total Cost</div>
                        <div class="stat-value">${fmtUsd(rollup.total_cost_usd)}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Calls</div>
                        <div class="stat-value">${rollup.event_count || 0}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Errors</div>
                        <div class="stat-value danger">${rollup.error_count || 0}</div>
                    </div>
                    <div class="stat-card">
                        <div class="stat-label">Unpriced</div>
                        <div class="stat-value unpriced-flag">${rollup.unpriced_event_count || 0}</div>
                    </div>
                </div>
                <h4 style="margin-bottom:10px;">Call-by-call ledger (most recent first)</h4>
                <table class="sessions-table">
                    <thead><tr>
                        <th>When</th><th>Turn</th><th>Type</th><th>Provider/Model</th><th>Units</th><th>Cost</th><th>Latency</th><th>Status</th>
                    </tr></thead>
                    <tbody>
                        ${events.map(e => `
                            <tr class="${e.success ? '' : 'error-row'}">
                                <td>${new Date(e.ts).toLocaleTimeString()}</td>
                                <td>${e.turn_count ?? '—'}</td>
                                <td>${escapeHtml(e.service_type)}</td>
                                <td>${escapeHtml(e.provider)}/${escapeHtml(e.model)}</td>
                                <td>${e.output_units ?? e.input_units ?? '—'} ${escapeHtml(e.unit_type || '')}</td>
                                <td class="cost-cell">${fmtUsd(e.cost_usd)}</td>
                                <td>${e.latency_ms != null ? e.latency_ms + 'ms' : '—'}</td>
                                <td>${e.success ? 'ok' : escapeHtml(e.error_message || 'failed')}</td>
                            </tr>
                        `).join('') || '<tr><td colspan="8">No events.</td></tr>'}
                    </tbody>
                </table>`;
        } catch (err) {
            body.innerHTML = `<div class="loading">Failed to load: ${escapeHtml(err.message)}</div>`;
        }
    }

    function closeCostModal() {
        document.getElementById('cost-modal').classList.remove('show');
    }

    // ─────────────────────────── Pricing save ───────────────────────────

    async function savePricingRow(key) {
        const inputs = document.querySelectorAll(`input[data-key="${key.replace(/"/g, '\\"')}"]`);
        const rates = (state.pricing && state.pricing.rates) || {};
        const existing = rates[key] || {};
        const rate = Object.assign({}, existing);
        inputs.forEach(input => {
            const field = input.getAttribute('data-field');
            const raw = input.value.trim();
            rate[field] = raw === '' ? null : Number(raw);
        });
        const sep = key.indexOf(':');
        const provider = key.slice(0, sep);
        const model = key.slice(sep + 1);
        try {
            const res = await adminFetch(`${API_BASE}/admin/pricing`, {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ provider, model, rate }),
            });
            const body = await res.json();
            if (!res.ok || !body.success) throw new Error(body.error || `HTTP ${res.status}`);
            showSuccess(`Updated pricing for ${key}`);
            state.pricing = body.data;
            renderPricingTable(body.data);
        } catch (err) {
            showError(`Failed to update pricing: ${err.message}`);
        }
    }

    // ─────────────────────────── Orchestration ───────────────────────────

    async function refreshSessions() {
        const sort = document.getElementById('analytics-session-sort').value || 'cost_desc';
        try {
            const sessions = await fetchJson(`/admin/analytics/sessions?sort=${sort}&limit=100`);
            renderSessionsTable(sessions);
        } catch (err) {
            document.getElementById('analytics-sessions-table').innerHTML =
                `<div class="loading">Failed to load sessions: ${escapeHtml(err.message)}</div>`;
        }
    }

    async function refresh() {
        const loading = document.getElementById('analytics-loading');
        const content = document.getElementById('analytics-content');
        loading.style.display = 'block';
        clearError();
        try {
            const granularity = state.range === '24h' ? 'hour' : 'day';
            const [summary, timeseries, providers, sessions, pricingData, errors, storageHealth] = await Promise.all([
                fetchJson(`/admin/analytics/summary?range=${state.range}`),
                fetchJson(`/admin/analytics/timeseries?range=${state.range}&granularity=${granularity}`),
                fetchJson(`/admin/analytics/providers?range=${state.range}`),
                fetchJson(`/admin/analytics/sessions?sort=${document.getElementById('analytics-session-sort').value}&limit=100`),
                fetchJson('/admin/pricing'),
                fetchJson(`/admin/analytics/errors?range=${state.range}`),
                fetchJson('/admin/analytics/storage_health').catch(() => null),
            ]);

            state.knownProviderModelKeys = new Set(
                (providers.providers || []).map(r => `${r.provider}:${r.model}`)
            );

            renderStorageHealth(storageHealth);
            renderKpis(summary);
            renderTimeseriesChart(timeseries);
            renderProviderDonut(summary);
            renderProvidersTable(providers);
            renderSessionsTable(sessions);
            renderPricingTable(pricingData);
            renderErrorsTable(errors);

            document.getElementById('analytics-last-updated').textContent =
                `Last updated: ${new Date().toLocaleTimeString()}`;
            loading.style.display = 'none';
            content.style.display = 'block';
        } catch (err) {
            console.error('[ANALYTICS] refresh failed:', err);
            loading.textContent = `Failed to load analytics: ${err.message}. Check ADMIN_TOKEN and try Refresh.`;
            loading.style.display = 'block';
            content.style.display = 'none';
        }
    }

    function togglePricing() {
        const panel = document.getElementById('analytics-pricing-panel');
        const caret = document.getElementById('pricing-toggle-caret');
        const isOpen = panel.style.display !== 'none';
        panel.style.display = isOpen ? 'none' : 'block';
        caret.style.transform = isOpen ? 'rotate(0deg)' : 'rotate(90deg)';
    }

    function exportCsv() {
        const sep = '?';
        const tokenPart = ADMIN_TOKEN ? `&token=${encodeURIComponent(ADMIN_TOKEN)}` : '';
        const url = `${API_BASE}/admin/analytics/export.csv${sep}range=${state.range}${tokenPart}`;
        window.open(url, '_blank');
    }

    function setupRangePills() {
        const container = document.getElementById('analytics-range-pills');
        if (!container) return;
        container.querySelectorAll('.range-pill').forEach(btn => {
            btn.addEventListener('click', () => {
                container.querySelectorAll('.range-pill').forEach(b => b.classList.remove('active'));
                btn.classList.add('active');
                state.range = btn.getAttribute('data-range');
                refresh();
            });
        });
    }

    document.addEventListener('DOMContentLoaded', setupRangePills);

    window.AdminAnalytics = {
        refresh,
        refreshSessions,
        exportCsv,
        openCostModal,
        closeCostModal,
        savePricingRow,
        togglePricing,
    };
})();
