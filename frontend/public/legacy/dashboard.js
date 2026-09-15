(() => {
    const API = () => (window.API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '');
    const views = {
        overview: ['Service Dashboard', 'Validation, profiling, cleaning and report-to-presentation services in one workspace.'],
        validation: ['Technical Validation', 'Validate and ingest CSV, Excel and JSON before any AI analysis.'],
        profiling: ['AI Data Profiling', 'Understand dataset structure and quality with the existing Google ADK profiling agent.'],
        cleaning: ['AI Cleaning & Review', 'Generate safe cleaning proposals, validate them and keep the user in control.'],
        reports: ['Word Report → Presentation', 'Extract DOCX evidence, refine the presentation story interactively and download the final handoff package.'],
        'data-formulator': ['Data Formulator', 'Independent Docker data exploration service, embedded in the InsightFlow workspace.'],
        presenton: ['Presenton AI', 'Independent Docker presentation generator, embedded in the InsightFlow workspace.']
    };


    const SIDEBAR_STORAGE_KEY = 'insightflow.sidebar.collapsed';
    let replacingGeminiKeyIndex = null;

    function setSidebarCollapsed(collapsed, {persist = true} = {}) {
        const shell = document.querySelector('.app-shell');
        const toggle = document.getElementById('sidebarToggle');
        if (!shell || !toggle) return;
        shell.classList.toggle('sidebar-collapsed', collapsed);
        toggle.setAttribute('aria-expanded', String(!collapsed));
        toggle.setAttribute('aria-label', collapsed ? 'Agrandir la barre latérale' : 'Réduire la barre latérale');
        toggle.title = collapsed ? 'Agrandir la barre latérale' : 'Réduire la barre latérale';
        if (persist) localStorage.setItem(SIDEBAR_STORAGE_KEY, collapsed ? '1' : '0');
    }

    function initializeSidebar() {
        const toggle = document.getElementById('sidebarToggle');
        if (!toggle) return;
        const collapsed = localStorage.getItem(SIDEBAR_STORAGE_KEY) === '1';
        setSidebarCollapsed(collapsed, {persist: false});
        toggle.addEventListener('click', () => {
            const shell = document.querySelector('.app-shell');
            setSidebarCollapsed(!shell?.classList.contains('sidebar-collapsed'));
        });
    }

    function setHidden(node, hidden) {
        if (!node) return;
        node.hidden = hidden;
        node.classList.toggle('active', !hidden);
    }

    const externalServices = {
        'data-formulator': {
            frameId: 'dataFormulatorFrame',
            linkId: 'dataFormulatorExternalLink',
            labelId: 'dataFormulatorUrlLabel',
            url: () => window.DATA_FORMULATOR_URL || 'http://localhost:5567'
        },
        presenton: {
            frameId: 'presentonFrame',
            linkId: 'presentonExternalLink',
            labelId: 'presentonUrlLabel',
            url: () => window.PRESENTON_URL || 'http://localhost:5001'
        }
    };

    function loadExternalService(name) {
        const service = externalServices[name];
        if (!service) return;
        const url = service.url().replace(/\/$/, '');
        const frame = document.getElementById(service.frameId);
        const link = document.getElementById(service.linkId);
        const label = document.getElementById(service.labelId);
        if (link) link.href = url;
        if (label) label.textContent = url;
        if (frame && frame.dataset.loadedUrl !== url) {
            frame.src = url;
            frame.dataset.loadedUrl = url;
        }
    }

    function show(name, {updateHash = true} = {}) {
        if (!views[name]) name = 'overview';
        document.querySelectorAll('.service-view').forEach(view => setHidden(view, view.dataset.view !== name));
        document.querySelectorAll('.service-nav-item').forEach(btn => btn.classList.toggle('active', btn.dataset.service === name));
        const [title, subtitle] = views[name];
        const titleNode = document.getElementById('workspaceTitle');
        const subtitleNode = document.getElementById('workspaceSubtitle');
        if (titleNode) titleNode.textContent = title;
        if (subtitleNode) subtitleNode.textContent = subtitle;
        if (updateHash) history.replaceState(null, '', `#${name}`);
        if (name === 'overview') refreshServiceStatus();
        if (name === 'profiling' || name === 'cleaning') refreshDatasetActions();
        const workspace = document.querySelector('.workspace');
        const externalActive = Boolean(externalServices[name]);
        workspace?.classList.toggle('external-workspace', externalActive);
        document.body.classList.toggle('external-tool-active', externalActive);
        if (externalActive) loadExternalService(name);
        window.scrollTo({top: 0, behavior: externalActive ? 'auto' : 'smooth'});
    }

    async function apiRequest(path, options = {}) {
        const response = await fetch(`${API()}${path}`, options);
        const data = await response.json();
        if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
        return data;
    }

    async function apiJSON(path) {
        return apiRequest(path);
    }

    function text(nodeId, value) {
        const node = document.getElementById(nodeId);
        if (node) node.textContent = value;
    }

    async function refreshServiceStatus() {
        const apiBadge = document.getElementById('apiStatus');
        const geminiBadge = document.getElementById('geminiStatus');
        try {
            const status = await apiJSON('/services/status');
            apiBadge.textContent = 'API · online';
            apiBadge.classList.add('ok');
            const configured = !!status.profiling?.gemini_configured;
            geminiBadge.textContent = configured ? 'Gemini · configured' : 'Gemini · key required';
            geminiBadge.classList.toggle('ok', configured);
            text('validationCount', `${status.validation?.datasets ?? 0} datasets`);
            text('profilingCount', `${status.profiling?.profiles ?? 0} profiles`);
            text('cleaningCount', `${status.cleaning?.reports ?? 0} reports`);
            text('reportCount', `${status.report_presentation?.jobs ?? 0} jobs`);
        } catch (error) {
            apiBadge.textContent = 'API · offline';
            apiBadge.classList.remove('ok');
            geminiBadge.textContent = 'Gemini · unavailable';
            geminiBadge.classList.remove('ok');
        }
    }

    function geminiKeysFromForm() {
        return Array.from(document.querySelectorAll('.gemini-key-input'))
            .map(input => input.value.trim())
            .filter(Boolean);
    }

    function workspaceSettingsFromForm() {
        return {
            gemini_model: document.getElementById('geminiModelInput')?.value.trim(),
            gemini_key_cooldown_seconds: document.getElementById('geminiCooldownInput')?.value,
            max_file_size_mb: document.getElementById('maxFileSizeInput')?.value,
            max_docx_size_mb: document.getElementById('maxDocxSizeInput')?.value,
            max_uncompressed_size_mb: document.getElementById('maxUncompressedSizeInput')?.value,
            max_zip_entries: document.getElementById('maxZipEntriesInput')?.value
        };
    }

    function applyWorkspaceSettings(settings = {}) {
        const inputs = {
            gemini_model: 'geminiModelInput',
            gemini_key_cooldown_seconds: 'geminiCooldownInput',
            max_file_size_mb: 'maxFileSizeInput',
            max_docx_size_mb: 'maxDocxSizeInput',
            max_uncompressed_size_mb: 'maxUncompressedSizeInput',
            max_zip_entries: 'maxZipEntriesInput'
        };
        Object.entries(inputs).forEach(([name, id]) => {
            const input = document.getElementById(id);
            if (input && settings[name] !== undefined) input.value = settings[name];
        });
    }

    function setGeminiFeedback(message = '', tone = '') {
        const feedback = document.getElementById('geminiSettingsFeedback');
        if (!feedback) return;
        feedback.className = `gemini-settings-feedback${tone ? ` ${tone}` : ''}`;
        feedback.textContent = message;
    }

    function hideGeminiKeyEditor() {
        replacingGeminiKeyIndex = null;
        const editor = document.getElementById('geminiKeyEditor');
        const input = document.getElementById('replaceGeminiKeyInput');
        if (editor) editor.hidden = true;
        if (input) input.value = '';
    }

    function beginGeminiKeyReplacement(key) {
        replacingGeminiKeyIndex = key.index;
        const editor = document.getElementById('geminiKeyEditor');
        const detail = document.getElementById('geminiKeyEditorDetail');
        const input = document.getElementById('replaceGeminiKeyInput');
        if (!editor || !detail || !input) return;
        detail.textContent = `Key ${key.index} · ${key.masked}`;
        editor.hidden = false;
        input.focus();
    }

    async function refreshGeminiSettings() {
        renderGeminiConfiguredKeys(await apiJSON('/settings/gemini'));
    }

    async function replaceSavedGeminiKey() {
        const input = document.getElementById('replaceGeminiKeyInput');
        const save = document.getElementById('saveGeminiKeyReplacement');
        const key = input?.value.trim();
        if (!replacingGeminiKeyIndex || !key) {
            setGeminiFeedback('Paste a replacement API key first.', 'error');
            return;
        }
        if (save) save.disabled = true;
        try {
            await apiRequest(`/settings/gemini/keys/${replacingGeminiKeyIndex}`, {
                method: 'PUT', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key})
            });
            hideGeminiKeyEditor();
            await refreshGeminiSettings();
            setGeminiFeedback('Saved key replaced. The previous secret is no longer retained.', 'success');
            refreshServiceStatus();
        } catch (error) {
            setGeminiFeedback(error.message || 'Could not replace the saved key.', 'error');
        } finally {
            if (save) save.disabled = false;
        }
    }

    async function deleteSavedGeminiKey(key) {
        if (!window.confirm(`Remove Gemini key ${key.index} (${key.masked})? This cannot be undone.`)) return;
        try {
            await apiRequest(`/settings/gemini/keys/${key.index}`, {method: 'DELETE'});
            hideGeminiKeyEditor();
            await refreshGeminiSettings();
            setGeminiFeedback('Saved key removed.', 'success');
            refreshServiceStatus();
        } catch (error) {
            setGeminiFeedback(error.message || 'Could not remove the saved key.', 'error');
        }
    }

    async function testSavedGeminiKey(key) {
        setGeminiFeedback(`Testing key ${key.index}…`);
        try {
            const response = await apiRequest(`/settings/gemini/keys/${key.index}/test`, {method: 'POST'});
            const result = response.result;
            setGeminiFeedback(`Key ${result.index} · ${result.masked}: ${result.message}`, result.valid ? 'success' : 'error');
        } catch (error) {
            setGeminiFeedback(error.message || 'Could not test the saved key.', 'error');
        }
    }

    function updateGeminiKeyRowControls() {
        const rows = Array.from(document.querySelectorAll('.gemini-key-row'));
        rows.forEach(row => {
            const remove = row.querySelector('.gemini-remove-key');
            if (remove) remove.disabled = rows.length === 1;
        });
    }

    function addGeminiKeyRow() {
        const rows = document.getElementById('geminiKeyRows');
        if (!rows) return;
        const row = document.createElement('div');
        row.className = 'gemini-key-row';
        const input = document.createElement('input');
        input.className = 'gemini-key-input';
        input.type = 'password';
        input.name = 'gemini-api-key';
        input.autocomplete = 'new-password';
        input.spellcheck = false;
        input.placeholder = 'Paste a Gemini API key';
        input.setAttribute('aria-label', 'Gemini API key');
        const remove = document.createElement('button');
        remove.className = 'gemini-remove-key';
        remove.type = 'button';
        remove.title = 'Remove key';
        remove.setAttribute('aria-label', 'Remove key');
        remove.innerHTML = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" aria-hidden="true"><path d="M6 6 18 18M18 6 6 18"/></svg>';
        remove.addEventListener('click', () => {
            row.remove();
            updateGeminiKeyRowControls();
        });
        row.append(input, remove);
        rows.append(row);
        updateGeminiKeyRowControls();
        input.focus();
    }

    function renderGeminiConfiguredKeys(settings) {
        const container = document.getElementById('geminiConfiguredKeys');
        if (!container) return;
        container.replaceChildren();
        const heading = document.createElement('strong');
        const count = settings?.total_keys || 0;
        heading.textContent = count ? `${count} saved key${count > 1 ? 's' : ''}` : 'No saved keys';
        const detail = document.createElement('span');
        detail.textContent = settings?.model ? `Model: ${settings.model}` : 'Gemini is not configured yet';
        container.append(heading, detail);
        if (count) {
            const list = document.createElement('div');
            list.className = 'gemini-key-chips';
            (settings.keys || []).forEach(key => {
                const chip = document.createElement('div');
                const label = document.createElement('span');
                label.textContent = `Key ${key.index} · ${key.masked}`;
                const replace = document.createElement('button');
                replace.type = 'button';
                replace.textContent = 'Replace';
                replace.addEventListener('click', () => beginGeminiKeyReplacement(key));
                const test = document.createElement('button');
                test.type = 'button';
                test.textContent = 'Test';
                test.addEventListener('click', () => testSavedGeminiKey(key));
                const remove = document.createElement('button');
                remove.type = 'button';
                remove.textContent = 'Remove';
                remove.className = 'danger';
                remove.addEventListener('click', () => deleteSavedGeminiKey(key));
                chip.append(label, test, replace, remove);
                list.append(chip);
            });
            container.append(list);
        }
        applyWorkspaceSettings(settings?.settings);
    }

    async function openGeminiSettings() {
        const dialog = document.getElementById('geminiSettingsDialog');
        const rows = document.getElementById('geminiKeyRows');
        if (!dialog || !rows) return;
        hideGeminiKeyEditor();
        rows.replaceChildren();
        addGeminiKeyRow();
        setGeminiFeedback('Loading local configuration…');
        dialog.showModal();
        try {
            renderGeminiConfiguredKeys(await apiJSON('/settings/gemini'));
            setGeminiFeedback('Leave the key fields empty to keep the saved pool while updating the other settings.');
        } catch (error) {
            renderGeminiConfiguredKeys(null);
            setGeminiFeedback('Could not load the current Gemini configuration.', 'error');
        }
    }

    function setGeminiActionsBusy(busy) {
        ['testGeminiKeys', 'saveGeminiSettings', 'addGeminiKey'].forEach(id => {
            const button = document.getElementById(id);
            if (button) button.disabled = busy;
        });
    }

    async function testGeminiKeys() {
        const keys = geminiKeysFromForm();
        const payload = workspaceSettingsFromForm();
        if (keys.length) payload.keys = keys;
        setGeminiActionsBusy(true);
        setGeminiFeedback('Testing Gemini connection…');
        try {
            const response = await apiRequest('/settings/gemini/test', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            const valid = response.results.filter(result => result.valid).length;
            const detail = response.results.map(result => `Key ${result.index}: ${result.message}`).join(' · ');
            setGeminiFeedback(`${valid}/${response.results.length} key${response.results.length > 1 ? 's' : ''} validated. ${detail}`, valid === response.results.length ? 'success' : 'error');
        } catch (error) {
            setGeminiFeedback(error.message || 'Could not test the Gemini keys.', 'error');
        } finally {
            setGeminiActionsBusy(false);
        }
    }

    async function saveGeminiSettings(event) {
        event.preventDefault();
        const keys = geminiKeysFromForm();
        const payload = workspaceSettingsFromForm();
        if (keys.length) payload.keys = keys;
        setGeminiActionsBusy(true);
        setGeminiFeedback('Saving your local Gemini configuration…');
        try {
            const response = await apiRequest('/settings/gemini', {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(payload)
            });
            renderGeminiConfiguredKeys({total_keys: response.total_keys, keys: response.keys, settings: response.settings});
            document.querySelectorAll('.gemini-key-input').forEach(input => { input.value = ''; });
            setGeminiFeedback(`${response.total_keys} key${response.total_keys > 1 ? 's' : ''} saved locally. Gemini is ready to use.`, 'success');
            refreshServiceStatus();
        } catch (error) {
            setGeminiFeedback(error.message || 'Could not save the Gemini configuration.', 'error');
        } finally {
            setGeminiActionsBusy(false);
        }
    }

    function initializeGeminiSettings() {
        const openButton = document.getElementById('geminiSettingsButton');
        const dialog = document.getElementById('geminiSettingsDialog');
        const closeButton = document.getElementById('closeGeminiSettings');
        const addButton = document.getElementById('addGeminiKey');
        const testButton = document.getElementById('testGeminiKeys');
        const cancelReplacement = document.getElementById('cancelGeminiKeyReplacement');
        const saveReplacement = document.getElementById('saveGeminiKeyReplacement');
        const form = document.getElementById('geminiSettingsForm');
        if (!openButton || !dialog || !closeButton || !addButton || !testButton || !cancelReplacement || !saveReplacement || !form) return;
        openButton.addEventListener('click', openGeminiSettings);
        closeButton.addEventListener('click', () => dialog.close());
        addButton.addEventListener('click', addGeminiKeyRow);
        testButton.addEventListener('click', testGeminiKeys);
        cancelReplacement.addEventListener('click', hideGeminiKeyEditor);
        saveReplacement.addEventListener('click', replaceSavedGeminiKey);
        form.addEventListener('submit', saveGeminiSettings);
    }

    function datasetActionCard(file, mode) {
        const card = document.createElement('article');
        card.className = 'dataset-action-card';
        const info = document.createElement('div');
        const title = document.createElement('strong');
        title.textContent = file.filename;
        const meta = document.createElement('span');
        meta.textContent = `${file.file_type.toUpperCase()} · ${file.size_kb} KB · ${file.updated_at}`;
        info.append(title, meta);
        const actions = document.createElement('div');
        actions.className = 'dataset-action-buttons';
        if (mode === 'profiling') {
            const primary = document.createElement('button');
            primary.className = 'btn-primary compact';
            primary.textContent = file.has_profile ? 'Voir profil' : 'Profiler';
            primary.addEventListener('click', () => {
                const api = window.InsightFlowData;
                if (!api) return;
                file.has_profile ? api.viewProfilingReport(file.file_type, file.stem) : api.runProfilingAgent(file.file_type, file.stem);
            });
            actions.append(primary);
        } else {
            const run = document.createElement('button');
            run.className = 'btn-primary compact';
            run.textContent = 'Nettoyer';
            run.addEventListener('click', () => window.InsightFlowData?.runCleaningAgent(file.file_type, file.stem));
            actions.append(run);
            if (file.has_cleaning) {
                const view = document.createElement('button');
                view.className = 'btn-secondary compact';
                view.textContent = 'Voir rapport';
                view.addEventListener('click', () => window.InsightFlowData?.viewCleaningReport(file.file_type, file.stem));
                actions.append(view);
            }
            if (file.has_cleaned_data) {
                const download = document.createElement('button');
                download.className = 'btn-secondary compact';
                download.textContent = 'Télécharger nettoyé';
                download.addEventListener('click', () => window.InsightFlowData?.downloadCleanedDataset(file.file_type, file.stem, download));
                actions.append(download);
            }
        }
        card.append(info, actions);
        return card;
    }

    async function refreshDatasetActions() {
        const profileWrap = document.getElementById('profilingDatasetList');
        const cleaningWrap = document.getElementById('cleaningDatasetList');
        if (!profileWrap || !cleaningWrap) return;
        try {
            const files = await apiJSON('/files');
            profileWrap.replaceChildren();
            cleaningWrap.replaceChildren();
            if (!files.length) {
                const empty1 = document.createElement('span'); empty1.className = 'muted'; empty1.textContent = 'Aucun dataset. Importez-en un depuis Validation.';
                const empty2 = empty1.cloneNode(true);
                profileWrap.append(empty1); cleaningWrap.append(empty2); return;
            }
            files.forEach(file => {
                profileWrap.append(datasetActionCard(file, 'profiling'));
                cleaningWrap.append(datasetActionCard(file, 'cleaning'));
            });
        } catch (error) {
            profileWrap.textContent = 'Impossible de charger les datasets.';
            cleaningWrap.textContent = 'Impossible de charger les datasets.';
        }
    }

    let initialized = false;

    function initializeDashboard() {
        if (initialized) return;
        initialized = true;
        initializeSidebar();
        initializeGeminiSettings();

        // Event delegation keeps navigation reliable even if cards/buttons are re-rendered.
        document.addEventListener('click', (event) => {
            const navButton = event.target.closest('.service-nav-item[data-service]');
            if (navButton) {
                event.preventDefault();
                show(navButton.dataset.service);
                return;
            }

            const serviceCard = event.target.closest('[data-open-service]');
            if (serviceCard) {
                event.preventDefault();
                show(serviceCard.dataset.openService);
            }
        });

        const requested = location.hash.slice(1);
        show(views[requested] ? requested : 'overview', {updateHash: false});
        refreshServiceStatus();
        refreshDatasetActions();
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', initializeDashboard, {once: true});
    } else {
        initializeDashboard();
    }

    window.addEventListener('insightflow:data-ui-ready', refreshDatasetActions);
    window.addEventListener('insightflow:report-job-created', refreshServiceStatus);
    window.addEventListener('focus', () => {
        if ((location.hash || '#overview') === '#overview') refreshServiceStatus();
    });

    window.InsightFlowDashboard = {show, refreshServiceStatus, refreshDatasetActions};
})();
