document.addEventListener("DOMContentLoaded", () => {
    const fileInput = document.getElementById("fileInput");
    const uploadButton = document.getElementById("uploadButton");
    const messageContainer = document.getElementById("message");
    const fileInfoContainer = document.getElementById("fileInfo");
    const profileResultContainer = document.getElementById("profileResult");
    const cleaningResultContainer = document.getElementById("cleaningResult");
    const resultContainer = document.getElementById("result");
    const selectedFileName = document.getElementById("selectedFileName");
    const filesListContainer = document.getElementById("filesListContainer");
    const refreshFilesButton = document.getElementById("refreshFilesButton");

    const BASE_API_URL = window.API_BASE_URL || "http://localhost:8000";

    // Presentation-only icons for dynamically rendered dataset actions.
    // Event classes and data attributes below remain the original contract.
    const actionIcon = (name) => {
        const paths = {
            check: '<path d="m5 12 4 4L19 6"/>',
            eye: '<path d="M2 12s3.5-6 10-6 10 6 10 6-3.5 6-10 6S2 12 2 12Z"/><circle cx="12" cy="12" r="2.5"/>',
            chart: '<path d="M3 3v18h18"/><path d="m7 16 4-5 4 3 5-7"/><path d="M18 7h2v2"/>',
            sparkles: '<path d="m12 3-1.8 5.2L5 10l5.2 1.8L12 17l1.8-5.2L19 10l-5.2-1.8L12 3Z"/><path d="m5 3-.6 1.6-1.6.6 1.6.6L5 8l.6-1.6 1.6-.6-1.6-.6L5 3Z"/>',
            download: '<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>',
            trash: '<path d="M3 6h18"/><path d="M8 6V4h8v2"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v5M14 11v5"/>'
        };
        return `<svg class="action-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || ''}</svg>`;
    };

    let issueMessagePending = false;

    // Chargement initial de la liste des fichiers
    loadFilesList();

    // Actualiser la liste des fichiers
    if (refreshFilesButton) {
        refreshFilesButton.addEventListener("click", () => {
            loadFilesList();
        });
    }

    // Affichage du nom du fichier sélectionné
    fileInput.addEventListener("change", () => {
        if (fileInput.files.length > 0) {
            selectedFileName.textContent = fileInput.files[0].name;
        } else {
            selectedFileName.textContent = "Aucun fichier choisi";
        }
    });

    // Gestion du clic sur le bouton Envoyer
    uploadButton.addEventListener("click", async () => {
        const file = fileInput.files[0];

        // Validation préliminaire
        if (!file) {
            showMessage("Veuillez sélectionner un fichier avant d'envoyer.", "error");
            return;
        }

        // Préparation du formulaire
        const formData = new FormData();
        formData.append("file", file);

        // État : Chargement
        setLoadingState(true);
        clearDisplays();
        window.ValidationUI.stage('validation');
        showMessage("Validation du format et de la structure en cours…", "info");

        try {
            const response = await fetch(`${BASE_API_URL}/upload`, {
                method: "POST",
                body: formData
            });

            const data = await response.json();

            if (!response.ok) {
                const errorMessage = data.detail || "Une erreur est survenue lors du traitement du fichier.";
                showMessage(errorMessage, "error");
                if (data.validation) window.ValidationUI.render(data.validation);
            } else {
                showMessage("Validation réussie. Votre dataset est prêt pour le profilage.", "success");
                window.ValidationUI.render(data.validation, () => runProfilingAgent(data.file_type, data.filename.replace(/\.[^.]+$/, '')));
                displayMetadata(data);
                displayTablePreview(data);
                loadFilesList(); // Actualiser la liste des fichiers
            }
        } catch (error) {
            console.error("Erreur de communication avec l'API :", error);
            showMessage("Impossible de joindre le serveur FastAPI. Vérifiez que l'API est bien lancée sur http://localhost:8000.", "error");
        } finally {
            setLoadingState(false);
        }
    });

    function cleanedFormatLabel(fileType) {
        return ({ csv: "CSV", json: "JSON", excel: "Excel (.xlsx)" })[fileType] || "fichier";
    }

    async function downloadCleanedDataset(fileType, stem, button = null) {
        const previous = button?.textContent;
        if (button) {
            button.disabled = true;
            button.textContent = "Préparation…";
        }
        try {
            const response = await fetch(`${BASE_API_URL}/cleanings/${encodeURIComponent(fileType)}/${encodeURIComponent(stem)}/download`);
            if (!response.ok) {
                let detail = "Impossible de télécharger le fichier nettoyé.";
                try { detail = (await response.json()).detail || detail; } catch (_) {}
                throw new Error(detail);
            }
            const blob = await response.blob();
            const disposition = response.headers.get("content-disposition") || "";
            const match = disposition.match(/filename=\"?([^\";]+)\"?/i);
            const fallbackExt = fileType === "excel" ? "xlsx" : fileType;
            const filename = match?.[1] || `${stem}_cleaned.${fallbackExt}`;
            const url = URL.createObjectURL(blob);
            const a = document.createElement("a");
            a.href = url;
            a.download = filename;
            document.body.appendChild(a);
            a.click();
            a.remove();
            URL.revokeObjectURL(url);
        } catch (error) {
            showMessage(error.message || "Téléchargement impossible.", "error");
        } finally {
            if (button) {
                button.disabled = false;
                button.textContent = previous;
            }
        }
    }

    async function deleteDataset(fileType, stem, filename, button = null) {
        const confirmed = window.confirm(
            `Supprimer définitivement « ${filename} » ?\n\nCette action supprime aussi son DataFrame, sa validation, son profilage, son fichier nettoyé, son rapport de nettoyage, ses conversations et les audits de nettoyage.`
        );
        if (!confirmed) return;

        const previous = button?.textContent;
        if (button) {
            button.disabled = true;
            button.textContent = "Suppression…";
        }
        try {
            const response = await fetch(`${BASE_API_URL}/files/${encodeURIComponent(fileType)}/${encodeURIComponent(stem)}`, { method: "DELETE" });
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "Suppression impossible.");

            showMessage(`Dataset « ${filename} » supprimé avec tous ses résultats associés.`, "success");
            [fileInfoContainer, profileResultContainer, cleaningResultContainer, resultContainer].forEach(node => {
                if (node) node.innerHTML = "";
            });
            await loadFilesList();
            window.InsightFlowDashboard?.refreshDatasetActions?.();
            window.InsightFlowDashboard?.refreshServiceStatus?.();
        } catch (error) {
            showMessage(error.message || "Suppression impossible.", "error");
        } finally {
            if (button?.isConnected) {
                button.disabled = false;
                button.textContent = previous;
            }
        }
    }

    /**
     * Récupère et affiche la liste des fichiers enregistrés sur le serveur
     */
    async function loadFilesList() {
        if (!filesListContainer) return;

        try {
            const response = await fetch(`${BASE_API_URL}/files`);
            if (!response.ok) {
                filesListContainer.innerHTML = `<p class="subtitle">Erreur lors de la récupération des fichiers.</p>`;
                return;
            }

            const files = await response.json();

            if (!files || files.length === 0) {
                filesListContainer.innerHTML = `<p class="subtitle" style="padding: 15px; text-align: center;">Aucun fichier enregistré pour le moment.</p>`;
                return;
            }

            let tableHtml = `
                <table class="preview-table">
                    <thead>
                        <tr>
                            <th>Nom du fichier</th>
                            <th>Format</th>
                            <th>Taille</th>
                            <th>Date</th>
                            <th>Statut</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody>
            `;

            files.forEach(f => {
                const typeClass = `badge-${escapeHtml(f.file_type)}`;
                const hasDfBadge = f.has_dataframe
                    ? `<span class="badge badge-excel" title="DataFrame disponible">DF Prêt</span>`
                    : `<span class="badge">Pas de DF</span>`;

                const hasProfileBadge = f.has_profile
                    ? `<span class="badge badge-profile-ready" title="Rapport IA généré">✨ Profil Prêt</span>`
                    : ``;

                const hasCleaningBadge = f.has_cleaning
                    ? `<span class="badge badge-cleaning-ready" title="Rapport de nettoyage généré">🧹 Nettoyage Prêt</span>`
                    : ``;

                const profileButton = f.has_profile
                    ? `<button class="btn-action btn-action-view-profile" data-type="${escapeHtml(f.file_type)}" data-stem="${escapeHtml(f.stem)}">
                         ${actionIcon('chart')}<span>Voir le profil</span>
                       </button>`
                    : `<button class="btn-action btn-action-profile" data-type="${escapeHtml(f.file_type)}" data-stem="${escapeHtml(f.stem)}">
                         ${actionIcon('chart')}<span>Lancer le profil</span>
                       </button>`;

                const cleanButton = `<button class="btn-action btn-action-clean" data-type="${escapeHtml(f.file_type)}" data-stem="${escapeHtml(f.stem)}">
                         ${actionIcon('sparkles')}<span>Nettoyer avec l’IA</span>
                       </button>`;

                const viewCleaningButton = f.has_cleaning
                    ? `<button class="btn-action btn-action-view-clean" data-type="${escapeHtml(f.file_type)}" data-stem="${escapeHtml(f.stem)}">
                         ${actionIcon('eye')}<span>Voir le nettoyage</span>
                       </button>`
                    : ``;

                const downloadCleanedButton = f.has_cleaned_data
                    ? `<button class="btn-action btn-action-download-cleaned" data-type="${escapeHtml(f.file_type)}" data-stem="${escapeHtml(f.stem)}">
                         ${actionIcon('download')}<span>Télécharger</span>
                       </button>`
                    : ``;

                const deleteButton = `<button class="btn-action btn-action-delete" data-type="${escapeHtml(f.file_type)}" data-stem="${escapeHtml(f.stem)}" data-filename="${escapeHtml(f.filename)}">
                         ${actionIcon('trash')}<span>Supprimer</span>
                       </button>`;

                tableHtml += `
                    <tr>
                        <td><strong>${escapeHtml(f.filename)}</strong></td>
                        <td><span class="badge ${typeClass}">${escapeHtml(f.file_type)}</span></td>
                        <td>${escapeHtml(f.size_kb)} KB</td>
                        <td>${escapeHtml(f.updated_at)}</td>
                        <td>
                            <div style="display: flex; gap: 4px; flex-wrap: wrap;">
                                ${hasDfBadge}
                                <span class="badge">${f.has_validation ? '✓ Fichier validé' : 'Validation à effectuer'}</span>
                                ${hasProfileBadge}
                                ${hasCleaningBadge}
                            </div>
                        </td>
                        <td>
                            <div class="btn-action-group">
                                <button class="btn-action btn-validate-file" data-type="${escapeHtml(f.file_type)}" data-stem="${escapeHtml(f.stem)}" data-validated="${!!f.has_validation}">${actionIcon('check')}<span>${f.has_validation ? 'Validation' : 'Valider'}</span></button>
                                <button class="btn-action btn-read-df" data-type="${escapeHtml(f.file_type)}" data-stem="${escapeHtml(f.stem)}">
                                    ${actionIcon('eye')}<span>Voir les données</span>
                                </button>
                                ${profileButton}
                                ${cleanButton}
                                ${viewCleaningButton}
                                ${downloadCleanedButton}
                                ${deleteButton}
                            </div>
                        </td>
                    </tr>
                `;
            });

            tableHtml += `</tbody></table>`;
            filesListContainer.innerHTML = tableHtml;
            filesListContainer.querySelectorAll('.btn-validate-file').forEach(button => button.addEventListener('click', async () => {
                button.disabled = true;
                try {
                    const saved = button.dataset.validated === 'true';
                    const response = await fetch(`${BASE_API_URL}/${saved ? 'validations' : 'validate'}/${button.dataset.type}/${button.dataset.stem}`, {method: saved ? 'GET' : 'POST'});
                    const data = await response.json();
                    if (!response.ok && !data.validation) throw new Error(data.detail);
                    window.ValidationUI.render(data.validation || data, response.ok ? () => runProfilingAgent(button.dataset.type, button.dataset.stem) : null);
                    document.getElementById('validationResult').scrollIntoView({behavior:'smooth', block:'start'});
                    loadFilesList();
                } catch (error) { showMessage(error.message || 'Validation impossible.', 'error'); }
                finally { button.disabled = false; }
            }));

            // Écouteurs sur les boutons "Lire DataFrame"
            document.querySelectorAll(".btn-read-df").forEach(btn => {
                btn.addEventListener("click", () => {
                    const fileType = btn.getAttribute("data-type");
                    const stem = btn.getAttribute("data-stem");
                    readFileDataFrame(fileType, stem);
                });
            });

            // Écouteurs sur les boutons "Profiler"
            document.querySelectorAll(".btn-action-profile").forEach(btn => {
                btn.addEventListener("click", () => {
                    const fileType = btn.getAttribute("data-type");
                    const stem = btn.getAttribute("data-stem");
                    runProfilingAgent(fileType, stem);
                });
            });

            // Écouteurs sur les boutons "Voir Profil"
            document.querySelectorAll(".btn-action-view-profile").forEach(btn => {
                btn.addEventListener("click", () => {
                    const fileType = btn.getAttribute("data-type");
                    const stem = btn.getAttribute("data-stem");
                    viewProfilingReport(fileType, stem);
                });
            });

            // Écouteurs sur les boutons "Nettoyer"
            document.querySelectorAll(".btn-action-clean").forEach(btn => {
                btn.addEventListener("click", () => {
                    const fileType = btn.getAttribute("data-type");
                    const stem = btn.getAttribute("data-stem");
                    runCleaningAgent(fileType, stem);
                });
            });

            // Écouteurs sur les boutons "Voir Nettoyage"
            document.querySelectorAll(".btn-action-view-clean").forEach(btn => {
                btn.addEventListener("click", () => {
                    const fileType = btn.getAttribute("data-type");
                    const stem = btn.getAttribute("data-stem");
                    viewCleaningReport(fileType, stem);
                });
            });

            filesListContainer.querySelectorAll(".btn-action-download-cleaned").forEach(btn => {
                btn.addEventListener("click", () => downloadCleanedDataset(btn.dataset.type, btn.dataset.stem, btn));
            });

            filesListContainer.querySelectorAll(".btn-action-delete").forEach(btn => {
                btn.addEventListener("click", () => deleteDataset(btn.dataset.type, btn.dataset.stem, btn.dataset.filename, btn));
            });

        } catch (err) {
            console.error("Erreur lors du chargement des fichiers :", err);
            filesListContainer.innerHTML = `<p class="subtitle" style="padding: 15px; color: #b91c1c;">Impossible de charger la liste des fichiers.</p>`;
        }
    }

    /**
     * Lit un DataFrame stocké via l'API et affiche ses informations
     */
    async function readFileDataFrame(fileType, stem) {
        window.InsightFlowDashboard?.show("validation");
        clearDisplays();
        showMessage(`Lecture du DataFrame [${fileType}] ${stem}...`, "info");

        try {
            const response = await fetch(`${BASE_API_URL}/files/${fileType}/${stem}`);
            const data = await response.json();

            if (!response.ok) {
                showMessage(data.detail || "Erreur lors de la lecture du DataFrame.", "error");
                return;
            }

            showMessage(`DataFrame chargé avec succès depuis le stockage !`, "success");
            displayMetadata(data);
            displayTablePreview(data);

            // Scroll vers les métadonnées
            fileInfoContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });

        } catch (err) {
            console.error("Erreur lors de la lecture du DataFrame :", err);
            showMessage("Erreur de communication avec le serveur lors de la lecture.", "error");
        }
    }

    /**
     * Exécute l'agent de profilage IA Google ADK + Gemini sur le dataset
     */
    async function runProfilingAgent(fileType, stem) {
        window.InsightFlowDashboard?.show("profiling");
        window.ValidationUI.stage('profiling');
        clearDisplays();
        showLoadingProfiling(stem);

        try {
            const response = await fetch(`${BASE_API_URL}/profile/${fileType}/${stem}`, {
                method: "POST",
            });
            const data = await response.json();

            if (!response.ok) {
                showMessage(data.detail || "Erreur lors de l'exécution de l'agent de profilage.", "error");
                return;
            }

            showMessage(`Profilage IA terminé avec succès pour ${stem} !`, "success");
            displayProfilingReport(data, fileType, stem);
            loadFilesList(); // Actualiser le badge

            if (profileResultContainer) {
                profileResultContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        } catch (err) {
            console.error("Erreur lors du profilage :", err);
            showMessage("Erreur de communication avec le serveur lors du profilage IA.", "error");
        }
    }

    /**
     * Affiche un rapport de profilage existant
     */
    async function viewProfilingReport(fileType, stem) {
        window.InsightFlowDashboard?.show("profiling");
        clearDisplays();
        showMessage(`Chargement du profil existant [${fileType}] ${stem}...`, "info");

        try {
            const response = await fetch(`${BASE_API_URL}/profiles/${fileType}/${stem}`);
            const data = await response.json();

            if (!response.ok) {
                showMessage(data.detail || "Erreur lors de la récupération du profil.", "error");
                return;
            }

            showMessage(`Rapport de profilage chargé avec succès !`, "success");
            displayProfilingReport(data, fileType, stem);

            if (profileResultContainer) {
                profileResultContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        } catch (err) {
            console.error("Erreur lors de la récupération du profil :", err);
            showMessage("Erreur lors de la récupération du profil de données.", "error");
        }
    }

    /**
     * Construit et injecte le rendu graphique du Rapport de Profilage IA
     */
    function displayProfilingReport(data, fileType, stem) {
        if (!profileResultContainer) return;

        const dataset = data.dataset || {};
        const understanding = data.understanding || {};
        const quality = data.quality_summary || {};
        const columns = data.columns || [];
        const problems = data.problems || [];
        const investigations = data.investigations || [];

        const confidencePct = Math.round((understanding.confidence || 0.5) * 100);
        const engineName = quality.engine || "Google ADK + Gemini API";
        const qualityScore = quality.quality_score !== undefined && quality.quality_score !== null ? quality.quality_score : 100;

        let scoreClass = "score-good";
        if (qualityScore < 60) scoreClass = "score-bad";
        else if (qualityScore < 85) scoreClass = "score-warning";

        // Scorecard KPIs
        const kpiHtml = `
            <div class="kpi-grid">
                <div class="kpi-card ${scoreClass}">
                    <div class="kpi-title">Score Qualité Global</div>
                    <div class="kpi-value">${escapeHtml(qualityScore)}<span style="font-size:16px;">/100</span></div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-title">Lignes & Colonnes</div>
                    <div class="kpi-value">${escapeHtml(dataset.rows || 0)} <span style="font-size:14px; color:#64748b;">× ${escapeHtml(dataset.columns || 0)}</span></div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-title">Valeurs Manquantes</div>
                    <div class="kpi-value">${escapeHtml(quality.missing_values || 0)}</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-title">Lignes Dupliquées</div>
                    <div class="kpi-value">${escapeHtml(quality.duplicate_rows || 0)}</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-title">Problèmes Détectés</div>
                    <div class="kpi-value" style="color: ${problems.length > 0 ? '#ea580c' : '#16a34a'};">${escapeHtml(problems.length)}</div>
                </div>
            </div>
        `;

        // Understanding Box
        const understandingHtml = `
            <div class="understanding-box">
                <h3>Compréhension Globale du Dataset</h3>
                <div class="understanding-text">${escapeHtml(understanding.description || "Analyse complétée.")}</div>
                <div class="confidence-bar-wrapper">
                    <span>Indice de Confiance IA : <strong>${confidencePct}%</strong></span>
                    <div class="confidence-bar-bg">
                        <div class="confidence-bar-fill" style="width: ${confidencePct}%;"></div>
                    </div>
                </div>
            </div>
        `;

        // Problems List
        let problemsHtml = "";
        if (problems && problems.length > 0) {
            let problemItems = "";
            problems.forEach(prob => {
                const sev = (prob.severity || "medium").toLowerCase();
                const sevClass = `sev-${sev}`;
                const probConfidence = prob.confidence ? `${Math.round(prob.confidence * 100)}%` : "";

                const scope = (prob.scope || "column").toLowerCase();
                const targetLabel = scope === "dataset"
                    ? `Dataset global`
                    : `Colonne <code>${escapeHtml(prob.column || "unknown")}</code>`;

                problemItems += `
                    <div class="problem-item ${sevClass}">
                        <span class="problem-badge-sev">${escapeHtml(prob.severity || "Alerte")}</span>
                        <div class="problem-details">
                            <div class="problem-header">
                                ${targetLabel} — <em>${escapeHtml(prob.type)}</em>
                                ${probConfidence ? `<span style="font-size:11px; font-weight:normal; color:#64748b;"> (Confiance: ${escapeHtml(probConfidence)})</span>` : ""}
                            </div>
                            <div class="problem-evidence">${escapeHtml(prob.evidence)}</div>
                        </div>
                    </div>
                `;
            });

            problemsHtml = `
                <div class="profile-sub-section">
                    <h3>⚠️ Problèmes et Anomalies Détectés (${problems.length})</h3>
                    <div class="problems-list">
                        ${problemItems}
                    </div>
                </div>
            `;
        } else {
            problemsHtml = `
                <div class="profile-sub-section">
                    <h3>⚠️ Problèmes Détectés</h3>
                    <div style="background:#f0fdf4; border:1px solid #bbf7d0; color:#166534; padding:12px 16px; border-radius:8px; font-size:13px; font-weight:500;">
                        ✅ Aucun problème critique de qualité détecté par l'agent de profilage.
                    </div>
                </div>
            `;
        }

        // Column Understanding Grid
        let columnsHtml = "";
        if (columns && columns.length > 0) {
            let colCards = "";
            columns.forEach(c => {
                const colConf = c.confidence ? `${Math.round(c.confidence * 100)}%` : "N/A";
                let obsItems = "";
                if (c.observations && c.observations.length > 0) {
                    c.observations.forEach(o => {
                        obsItems += `<li>${escapeHtml(o)}</li>`;
                    });
                }

                colCards += `
                    <div class="column-card">
                        <div class="column-card-header">
                            <span class="column-card-title">${escapeHtml(c.name)}</span>
                            <span class="column-type-pill">${escapeHtml(c.observed_type)}</span>
                        </div>
                        <div class="column-role-row">
                            <span class="column-role-pill">${escapeHtml(c.likely_role)}</span>
                            <span style="font-size:11px; font-weight:600; color:#64748b;">Confiance: ${escapeHtml(colConf)}</span>
                        </div>
                        <ul class="column-obs-list">
                            ${obsItems}
                        </ul>
                    </div>
                `;
            });

            columnsHtml = `
                <div class="profile-sub-section">
                    <h3>📊 Compréhension et Rôle des Colonnes (${columns.length})</h3>
                    <div class="columns-grid">
                        ${colCards}
                    </div>
                </div>
            `;
        }

        // Investigations
        let investigationsHtml = "";
        if (investigations && investigations.length > 0) {
            let invCards = "";
            investigations.forEach(inv => {
                let examplePills = "";
                if (inv.examples && inv.examples.length > 0) {
                    inv.examples.forEach(ex => {
                        examplePills += `<span class="investigation-example-tag">${escapeHtml(ex)}</span>`;
                    });
                }

                invCards += `
                    <div class="investigation-card">
                        <div class="investigation-card-title">🔍 Investigation : ${escapeHtml(inv.column)}</div>
                        <div class="investigation-card-obs">${escapeHtml(inv.observation)}</div>
                        ${examplePills ? `<div class="investigation-examples">${examplePills}</div>` : ""}
                    </div>
                `;
            });

            investigationsHtml = `
                <div class="profile-sub-section">
                    <h3>🔬 Investigations & Découvertes Spécifiques</h3>
                    <div class="investigations-grid">
                        ${invCards}
                    </div>
                </div>
            `;
        }

        // Full Profile Card
        const fullProfileHtml = `
            <div class="profile-card">
                <div class="profile-header-banner">
                    <div class="profile-title-area">
                        <h2>🔍 Rapport de Profilage IA — ${escapeHtml(stem)}</h2>
                        <span class="profile-engine-tag">Agent : ${escapeHtml(engineName)}</span>
                    </div>
                    <div class="profile-actions-bar">
                        <button id="btnDownloadProfileJson" class="btn-secondary">📥 Télécharger JSON</button>
                        <button id="btnToggleRawProfileJson" class="btn-secondary">💻 JSON Brut</button>
                        <button id="btnRerunProfile" class="btn-primary" style="padding: 6px 14px; font-size: 13px; min-width: auto;">🔄 Re-profiler</button>
                    </div>
                </div>

                ${understandingHtml}
                ${kpiHtml}
                ${problemsHtml}
                ${columnsHtml}
                ${investigationsHtml}

                <div id="rawProfileJsonContainer" class="raw-json-container">
                    <code>${escapeHtml(JSON.stringify(data, null, 2))}</code>
                </div>
            </div>
        `;

        profileResultContainer.innerHTML = fullProfileHtml;

        // Bouton Télécharger JSON
        const btnDownload = document.getElementById("btnDownloadProfileJson");
        if (btnDownload) {
            btnDownload.addEventListener("click", () => {
                const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `${stem}_profile.json`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            });
        }

        // Bouton Toggle JSON Brut
        const btnToggleJson = document.getElementById("btnToggleRawProfileJson");
        const rawJsonBox = document.getElementById("rawProfileJsonContainer");
        if (btnToggleJson && rawJsonBox) {
            btnToggleJson.addEventListener("click", () => {
                if (rawJsonBox.style.display === "block") {
                    rawJsonBox.style.display = "none";
                    btnToggleJson.textContent = "💻 JSON Brut";
                } else {
                    rawJsonBox.style.display = "block";
                    btnToggleJson.textContent = "Masquer JSON";
                }
            });
        }

        // Bouton Re-profiler
        const btnRerun = document.getElementById("btnRerunProfile");
        if (btnRerun) {
            btnRerun.addEventListener("click", () => {
                runProfilingAgent(fileType, stem);
            });
        }
    }

    /**
     * Affiche un indicateur de chargement pour le profilage
     */
    function showLoadingProfiling(stem) {
        if (!profileResultContainer) return;
        profileResultContainer.innerHTML = `
            <div class="spinner-container">
                <div class="spinner"></div>
                <div>
                    <strong>🔍 Profilage IA en cours pour <em>${escapeHtml(stem)}</em>...</strong>
                    <div style="font-size: 12px; color: #64748b; margin-top: 2px;">
                        L'Agent Google ADK explore le dataset, exécute des outils d'investigation et analyse la qualité des données.
                    </div>
                </div>
            </div>
        `;
    }

    /**
     * Exécute l'agent de nettoyage IA Google ADK + Gemini sur le dataset
     */
    async function runCleaningAgent(fileType, stem) {
        window.InsightFlowDashboard?.show("cleaning");
        window.ValidationUI.stage('cleaning');
        clearDisplays();
        showLoadingCleaning(stem);

        try {
            const response = await fetch(`${BASE_API_URL}/clean/${fileType}/${stem}`, {
                method: "POST",
                headers: {"Content-Type": "application/json"},
                body: JSON.stringify({approval_mode: document.getElementById("cleaningApprovalMode")?.value || "auto_safe"}),
            });
            const data = await response.json();

            if (!response.ok) {
                showMessage(data.detail || "Erreur lors de l'exécution de l'agent de nettoyage.", "error");
                return;
            }

            showMessage(`Analyse de nettoyage terminée pour ${stem}. Consultez les résultats et les décisions en attente.`, "info");
            displayCleaningReport(data, fileType, stem);
            loadFilesList(); // Actualiser le badge

            if (cleaningResultContainer) {
                cleaningResultContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        } catch (err) {
            console.error("Erreur lors du nettoyage :", err);
            showMessage("Erreur de communication avec le serveur lors du nettoyage IA.", "error");
        } finally {
            const spinner = cleaningResultContainer?.querySelector('.spinner-container');
            if (spinner) {
                spinner.textContent = "Le nettoyage s’est arrêté. Consultez le message d’erreur, puis relancez depuis la liste des fichiers.";
            }
        }
    }

    /**
     * Affiche un rapport de nettoyage existant
     */
    async function viewCleaningReport(fileType, stem) {
        window.InsightFlowDashboard?.show("cleaning");
        clearDisplays();
        showMessage(`Chargement du rapport de nettoyage existant [${fileType}] ${stem}...`, "info");

        try {
            const response = await fetch(`${BASE_API_URL}/cleanings/${fileType}/${stem}`);
            const data = await response.json();

            if (!response.ok) {
                showMessage(data.detail || "Erreur lors de la récupération du rapport de nettoyage.", "error");
                return;
            }

            showMessage(`Rapport de nettoyage chargé avec succès !`, "success");
            displayCleaningReport(data, fileType, stem);

            if (cleaningResultContainer) {
                cleaningResultContainer.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        } catch (err) {
            console.error("Erreur lors de la récupération du rapport de nettoyage :", err);
            showMessage("Erreur lors de la récupération du rapport de nettoyage.", "error");
        }
    }

    /**
     * Traduit le statut technique du rapport de nettoyage en phrase simple pour l'utilisateur
     */
    function statusToPlainFrench(status) {
        if (status === "reviewed") return "✅ Revue terminée — décisions enregistrées";
        if (status === "cleaned") return "✅ Nettoyage terminé";
        if (status === "partially_cleaned") return "⚠️ Nettoyage partiel — certains éléments nécessitent votre attention";
        if (status === "unchanged") return "ℹ️ Aucune modification nécessaire";
        return escapeHtml(status || "Statut inconnu");
    }

    /**
     * Construit et injecte le rendu graphique du Rapport de Nettoyage IA
     */
    function executionLabel(turn) {
        if (turn.role === "user") return "";
        const label = turn.execution_status === "applied" ? "Modification vérifiée"
            : turn.execution_status === "accepted" ? "Accepté en l’état — discussion clôturée"
            : turn.execution_status === "reopened" ? "Discussion rouverte"
            : turn.execution_status === "no_change" ? "Aucune modification"
            : "Ancienne réponse — exécution non vérifiée";
        return `<div class="chat-turn-role">${label}</div>`;
    }

    function renderProposals(proposals) {
        const ready = (proposals || []).filter(proposal => proposal.status === "ready");
        if (!ready.length) return "";
        return `<div class="cleaning-options"><h4>Options à comparer · aperçu uniquement</h4>
            ${ready.map((proposal, index) => `<article class="cleaning-proposal">
                <h4>${index + 1}. ${escapeHtml(proposal.title)}</h4>
                <p>Aperçu uniquement — les données enregistrées ne sont pas encore modifiées.</p>
                ${window.CleaningUI.proposal(proposal)}
                <div class="proposal-metrics">${escapeHtml(proposal.rows_affected)} ligne(s) concernée(s) · ${escapeHtml(proposal.rows_before)} → ${escapeHtml(proposal.rows_after)} lignes</div>
                ${proposal.impact ? `<p class="proposal-impact">${escapeHtml(proposal.impact)}</p>` : ""}
                ${(proposal.samples || []).length ? `<div class="proposal-preview"><table><thead><tr><th>Ligne</th><th>Avant</th><th>Après</th></tr></thead><tbody>
                    ${proposal.samples.map(sample => `<tr><td>${escapeHtml(sample.row)}</td><td>${escapeHtml(sample.before ?? "Valeur manquante")}</td><td>${escapeHtml(sample.after ?? "Valeur manquante")}</td></tr>`).join("")}
                </tbody></table></div>` : ""}
                <div class="proposal-buttons">
                    <button class="btn-primary btn-proposal-decision" data-proposal-id="${escapeHtml(proposal.id)}" data-decision="apply">Appliquer cette option</button>
                    <button class="btn-secondary btn-proposal-decision" data-proposal-id="${escapeHtml(proposal.id)}" data-decision="dismiss">Écarter</button>
                </div>
            </article>`).join("")}
            <p class="chat-hint">Écrivez le numéro (ex. « 1 ») dans le chat de ce problème pour appliquer l’option. Écrivez « autre » ou décrivez directement votre solution. « Oui » applique l’unique proposition disponible.</p>
        </div>`;
    }

    function displayCleaningReport(data, fileType, stem) {
        if (!cleaningResultContainer) return;

        // Préserve l'état ouvert/fermé du panneau technique lors des
        // re-rendus déclenchés par l'envoi d'un message à l'agent, pour ne
        // pas perdre le fil de la conversation en cours.
        const existingDetails = cleaningResultContainer.querySelector(".technical-details");
        const detailsWasOpen = existingDetails ? existingDetails.open : false;

        const status = data.status || "unchanged";
        const summary = data.summary || "";
        const actions = data.actions || [];
        const remainingIssues = data.remaining_issues || [];
        const validation = data.validation || {};
        const qualityBefore = data.quality_before !== undefined && data.quality_before !== null ? data.quality_before : 0;
        const qualityAfter = data.quality_after !== undefined && data.quality_after !== null ? data.quality_after : 0;
        const engineName = validation.engine || "Google ADK + Gemini API";

        // La couleur du score "Après" est conditionnée par le statut, pas seulement par le nombre :
        // un score élevé à côté de problèmes non résolus serait trompeur.
        let afterScoreClass;
        if (status === "cleaned") {
            afterScoreClass = qualityAfter < 60 ? "score-bad" : (qualityAfter < 85 ? "score-warning" : "score-good");
        } else {
            afterScoreClass = qualityAfter < 60 ? "score-bad" : "score-warning";
        }

        // ---- 1. Résumé en langage simple (toujours visible en premier) ----
        const plainSummaryHtml = `
            <div class="plain-summary-box">
                <div class="plain-summary-status">${statusToPlainFrench(status)}</div>
                <div class="plain-summary-text">${escapeHtml(summary)}</div>
            </div>
        `;

        // ---- 2. KPI grid ----
        const kpiHtml = `
            <div class="kpi-grid">
                <div class="kpi-card score-good">
                    <div class="kpi-title">Qualité Avant</div>
                    <div class="kpi-value">${escapeHtml(qualityBefore)}<span style="font-size:16px;">/100</span></div>
                </div>
                <div class="kpi-card ${afterScoreClass}">
                    <div class="kpi-title">Qualité Après</div>
                    <div class="kpi-value">${escapeHtml(qualityAfter)}<span style="font-size:16px;">/100</span></div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-title">Statut</div>
                    <div class="kpi-value" style="font-size:16px;">${statusToPlainFrench(status)}</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-title">Opérations Appliquées</div>
                    <div class="kpi-value">${escapeHtml(data.changes_applied || 0)}</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-title">Lignes Affectées</div>
                    <div class="kpi-value">${escapeHtml(data.rows_affected || 0)}</div>
                </div>
            </div>
        `;

        // ---- 3. Actions appliquées ----
        let actionsHtml = "";
        if (actions.length > 0) {
            let actionItems = "";
            actions.forEach(a => {
                const confPct = a.confidence !== undefined && a.confidence !== null ? `${Math.round(a.confidence * 100)}%` : "";
                const validationText = a.validation ? JSON.stringify(a.validation) : "";
                actionItems += `
                    <div class="action-item">
                        <div class="problem-details">
                            <div class="problem-header">
                                ${escapeHtml(a.action || "action")} — <em>${escapeHtml(a.problem || "")}</em>
                                ${confPct ? `<span style="font-size:11px; font-weight:normal; color:#64748b;"> (Confiance: ${escapeHtml(confPct)})</span>` : ""}
                            </div>
                            <div class="problem-evidence">${escapeHtml(a.reason || "")}</div>
                            ${a.generated_code ? `${window.CleaningUI.risk({level:a.risk_level})}<p>Appliqué par : ${a.resolved_by === 'user' ? 'Utilisateur' : 'Agent'} · Code : ${escapeHtml(a.code_hash || '')}</p>${window.CleaningUI.validation(a.validation)}${window.CleaningUI.code(a.generated_code)}` : ''}
                            <div class="problem-evidence" style="margin-top:4px;">Lignes concernées : ${escapeHtml(a.rows_affected || 0)}</div>
                            <details class="generated-code"><summary>Détails de validation</summary><pre>${escapeHtml(validationText)}</pre></details>
                        </div>
                    </div>
                `;
            });
            actionsHtml = `
                <div class="profile-sub-section">
                    <h3>✅ Actions Appliquées (${actions.length})</h3>
                    <div class="actions-list">
                        ${actionItems}
                    </div>
                </div>
            `;
        } else {
            actionsHtml = `
                <div class="profile-sub-section">
                    <h3>✅ Actions Appliquées</h3>
                    <div style="background:#f8fafc; border:1px solid #e2e8f0; color:#475569; padding:12px 16px; border-radius:8px; font-size:13px; font-weight:500;">
                        Aucune modification automatique appliquée.
                    </div>
                </div>
            `;
        }

        // ---- 4. Problèmes non résolus ----
        const issueConversations = data.issue_conversations || {};
        let remainingHtml = "";
        if (remainingIssues.length > 0) {
            let issueItems = "";
            remainingIssues.forEach((issue, idx) => {
                const sev = (issue.severity || "medium").toLowerCase();
                const sevClass = `sev-${sev}`;
                const scope = (issue.scope || "column").toLowerCase();
                const targetLabel = scope === "dataset"
                    ? `Dataset global`
                    : `Colonne <code>${escapeHtml(issue.column || "unknown")}</code>`;

                const canDiscuss = Boolean(issue.id);

                let chatFormHtml = "";
                if (canDiscuss) {
                    const thread = issueConversations[issue.id] || [];
                    let threadHtml = "";
                    thread.forEach(turn => {
                        const isUser = turn.role === "user";
                        threadHtml += `
                            <div class="chat-turn ${isUser ? "chat-turn-user" : "chat-turn-agent"}">
                                <span class="chat-turn-role">${isUser ? "🧑 Vous" : "🤖 Agent"}</span>
                                <div class="chat-turn-text">${escapeHtml(turn.text || "")}</div>
                                ${executionLabel(turn)}
                            </div>
                        `;
                    });

                    chatFormHtml = `
                        <div class="issue-chat" data-issue-id="${escapeHtml(issue.id)}">
                            ${threadHtml ? `<div class="issue-chat-thread" aria-live="polite">${threadHtml}</div>` : ""}
                            ${data.run_id ? '' : renderProposals((data.issue_proposals || {})[issue.id])}
                            <div class="chat-suggestions">
                                <button type="button" class="btn-secondary btn-review-decision" data-issue-id="${escapeHtml(issue.id)}" data-decision="keep">Conserver et clôturer</button>
                                <button type="button" class="btn-secondary btn-issue-explain">Comparer les options</button>
                                <button type="button" class="btn-secondary btn-chat-suggestion" data-message="Que recommandes-tu pour conserver les lignes et préserver au mieux les statistiques ?">Préserver les statistiques</button>
                                ${/email/i.test(issue.column || "") ? `<button type="button" class="btn-secondary btn-chat-suggestion" data-message="Propose des e-mails fictifs uniques et séquentiels pour les valeurs manquantes.">E-mails fictifs uniques</button>` : ""}
                                ${/negative|négati/i.test(`${issue.evidence || ""} ${issue.type || ""}`) ? `<button type="button" class="btn-secondary btn-chat-suggestion" data-message="Prépare un aperçu pour convertir uniquement les valeurs négatives en valeurs absolues.">Appliquer abs aux négatifs</button>` : ""}
                            </div>
                            <div class="issue-chat-input-row">
                                <textarea class="issue-chat-input" rows="2" maxlength="4000" aria-label="Message pour l’agent de nettoyage" placeholder="Décrivez ce que vous souhaitez : une solution, une règle ou une autre proposition…"></textarea>
                                <button class="btn-secondary btn-issue-chat-send">💬 Envoyer à l'agent</button>
                            </div>
                            <div class="chat-hint">Entrée pour envoyer · Maj + Entrée pour une nouvelle ligne</div>
                            <div class="chat-request-status" role="status" aria-live="polite"></div>
                        </div>
                    `;
                }

                issueItems += `
                    <div class="problem-item ${sevClass}">
                        <span class="problem-badge-sev">${escapeHtml(issue.severity || "Alerte")}</span>
                        <div class="problem-details">
                            <div class="problem-header">
                                ${targetLabel} — <em>${escapeHtml(issue.type || "")}</em>
                            </div>
                <div class="problem-evidence">${escapeHtml(issue.current_evidence || issue.evidence || "")}</div>
                            ${issue.reason_not_fixed ? `<div class="problem-evidence" style="margin-top:4px; font-style:italic;">Pourquoi non corrigé : ${escapeHtml(issue.reason_not_fixed)}</div>` : ""}
                            ${chatFormHtml}
                        </div>
                    </div>
                `;
            });
            remainingHtml = `
                <div class="profile-sub-section">
                    <h3>⚠️ Problèmes Non Résolus (${remainingIssues.length})</h3>
                    <div class="problems-list">
                        ${issueItems}
                    </div>
                </div>
            `;
        } else {
            remainingHtml = `
                <div class="profile-sub-section">
                    <h3>⚠️ Problèmes Non Résolus</h3>
                    <div style="background:#f0fdf4; border:1px solid #bbf7d0; color:#166534; padding:12px 16px; border-radius:8px; font-size:13px; font-weight:500;">
                        ✅ Aucun problème en attente de décision.
                    </div>
                </div>
            `;
        }

        const acceptedIssues = data.accepted_issues || [];
        const acceptedHtml = acceptedIssues.length ? `<div class="profile-sub-section">
            <h3>Décisions enregistrées (${acceptedIssues.length})</h3>
            <p>Ces problèmes sont acceptés en l’état. Ils restent pris en compte dans le score ; leur discussion est clôturée.</p>
            ${acceptedIssues.map(issue => `<div class="cleaning-proposal">
                <strong>${escapeHtml(issue.column || "Dataset")} — valeurs conservées</strong>
            <p>${escapeHtml(issue.current_evidence || issue.evidence || "")}</p>
                <button class="btn-secondary btn-review-decision" data-issue-id="${escapeHtml(issue.id)}" data-decision="reopen">Rouvrir ce problème</button>
            </div>`).join("")}
        </div>` : "";
        const activeIssueIds = new Set(remainingIssues.map(issue => issue.id));
        const conversationHistoryHtml = Object.entries(issueConversations)
            .filter(([id]) => !activeIssueIds.has(id))
            .map(([id, turns]) => `
                <details class="profile-sub-section">
                    <summary>Conversation terminée — ${escapeHtml(id)}</summary>
                    ${turns.map(turn => `<div class="chat-turn ${turn.role === "user" ? "chat-turn-user" : "chat-turn-agent"}">
                        <span class="chat-turn-role">${turn.role === "user" ? "Vous" : "Agent"}</span>
                        <div class="chat-turn-text">${escapeHtml(turn.text || "")}</div>
                        ${executionLabel(turn)}
                    </div>`).join("")}
                </details>`).join("");

        // ---- 5. Détails techniques (repliés par défaut) ----
        const technicalDetailsHtml = `
            <details class="technical-details"${detailsWasOpen ? " open" : ""}>
                <summary class="technical-details-toggle">🔍 Voir les détails techniques</summary>
                <div class="technical-details-body">
                    ${window.CleaningUI.attempts(data)}
                    <div id="rawCleaningJsonContainer" class="raw-json-container">
                        <code>${escapeHtml(JSON.stringify(data, null, 2))}</code>
                    </div>
                </div>
            </details>
        `;

        // ---- Full Cleaning Card ----
        const fullCleaningHtml = `
            <div class="profile-card">
                <div class="profile-header-banner">
                    <div class="profile-title-area">
                        <h2>🧹 Rapport de Nettoyage IA — ${escapeHtml(stem)}</h2>
                        <span class="profile-engine-tag">Agent : ${escapeHtml(engineName)}</span>
                    </div>
                    <div class="profile-actions-bar">
                        <button id="btnDownloadCleanedFile" class="btn-primary">⬇ Télécharger fichier nettoyé · ${cleanedFormatLabel(fileType)}</button>
                        <button id="btnDownloadCleaningJson" class="btn-secondary">📥 Rapport JSON</button>
                        <button id="btnToggleRawCleaningJson" class="btn-secondary">💻 JSON Brut</button>
                        <button id="btnRerunCleaning" class="btn-primary" style="padding: 6px 14px; font-size: 13px; min-width: auto;">🔄 Re-nettoyer</button>
                    </div>
                </div>

                ${plainSummaryHtml}
                ${validation.synthetic_cells ? `<p class="proposal-impact">${escapeHtml(validation.synthetic_cells)} cellule(s) contiennent des e-mails fictifs marqués. Le score de complétude ne signifie pas que ces contacts sont réels.</p>` : ""}
                ${kpiHtml}
                ${window.CleaningUI.plan(data)}
                ${actionsHtml}
                ${data.run_id && Object.values(data.issue_proposals || {}).some(items => items.some(p => p.status === 'ready')) ? `<section class="profile-sub-section"><h3>Transformations en attente de revue</h3>${Object.entries(data.issue_proposals || {}).filter(([, items]) => items.some(p => p.status === 'ready')).map(([id, items]) => `<div class="generated-review" data-issue-id="${escapeHtml(id)}">${renderProposals(items)}</div>`).join('')}</section>` : ''}
                ${validation.last_issue_resolution_engine ? `<p class="plain-summary-text">Moteur du dernier échange : ${escapeHtml(validation.last_issue_resolution_engine)}</p>` : ""}
                ${remainingHtml}
                ${acceptedHtml}
                ${conversationHistoryHtml}
                ${technicalDetailsHtml}
            </div>
        `;

        cleaningResultContainer.innerHTML = fullCleaningHtml;
        cleaningResultContainer.querySelectorAll('.generated-review .btn-proposal-decision').forEach(button => {
            button.addEventListener('click', () => sendIssueMessage(fileType, stem,
                button.closest('.generated-review').dataset.issueId,
                button.dataset.decision === 'apply' ? 'Appliquer cette proposition' : 'Écarter cette proposition',
                {proposal_id: button.dataset.proposalId, decision: button.dataset.decision}));
        });

        const btnDownloadCleaned = document.getElementById("btnDownloadCleanedFile");
        if (btnDownloadCleaned) {
            btnDownloadCleaned.addEventListener("click", () => downloadCleanedDataset(fileType, stem, btnDownloadCleaned));
        }

        // Bouton Télécharger le rapport JSON
        const btnDownload = document.getElementById("btnDownloadCleaningJson");
        if (btnDownload) {
            btnDownload.addEventListener("click", () => {
                const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
                const url = URL.createObjectURL(blob);
                const a = document.createElement("a");
                a.href = url;
                a.download = `${stem}_cleaning.json`;
                document.body.appendChild(a);
                a.click();
                document.body.removeChild(a);
                URL.revokeObjectURL(url);
            });
        }

        // Bouton Toggle JSON Brut
        const btnToggleJson = document.getElementById("btnToggleRawCleaningJson");
        const rawJsonBox = document.getElementById("rawCleaningJsonContainer");
        if (btnToggleJson && rawJsonBox) {
            btnToggleJson.addEventListener("click", () => {
                if (rawJsonBox.style.display === "block") {
                    rawJsonBox.style.display = "none";
                    btnToggleJson.textContent = "💻 JSON Brut";
                } else {
                    rawJsonBox.style.display = "block";
                    btnToggleJson.textContent = "Masquer JSON";
                }
            });
        }

        // Bouton Re-nettoyer
        const btnRerun = document.getElementById("btnRerunCleaning");
        if (btnRerun) {
            btnRerun.addEventListener("click", () => {
                runCleaningAgent(fileType, stem);
            });
        }

        cleaningResultContainer.querySelectorAll(".btn-review-decision").forEach(button => {
            button.addEventListener("click", () => sendIssueMessage(fileType, stem, button.dataset.issueId,
                button.dataset.decision === "keep" ? "Conserver et clôturer" : "Rouvrir ce problème",
                { decision: button.dataset.decision }));
        });
        document.querySelectorAll(".issue-chat").forEach(container => {
            const input = container.querySelector(".issue-chat-input");
            container.querySelector(".btn-issue-explain").addEventListener("click", () => {
                input.value = "Compare les solutions possibles et prépare des aperçus adaptés à ce problème, sans modifier les données.";
                input.focus();
            });
            container.querySelectorAll(".btn-chat-suggestion").forEach(button => {
                button.addEventListener("click", () => {
                    input.value = button.dataset.message;
                    input.focus();
                });
            });
            container.querySelectorAll(".btn-proposal-decision").forEach(button => {
                button.addEventListener("click", () => sendIssueMessage(fileType, stem, container.dataset.issueId,
                    button.dataset.decision === "apply" ? "Appliquer cette proposition" : "Écarter cette proposition",
                    { proposal_id: button.dataset.proposalId, decision: button.dataset.decision }));
            });
            input.addEventListener("keydown", event => {
                if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
                    event.preventDefault();
                    container.querySelector(".btn-issue-chat-send").click();
                }
            });
        });

        // Boutons "Envoyer à l'agent" pour discuter d'un problème non résolu
        document.querySelectorAll(".btn-issue-chat-send").forEach(btn => {
            btn.addEventListener("click", () => {
                const container = btn.closest(".issue-chat");
                const issueId = container.getAttribute("data-issue-id");
                const input = container.querySelector(".issue-chat-input");
                const message = (input.value || "").trim();
                if (!message) {
                    showMessage("Veuillez écrire un message avant de l'envoyer.", "error");
                    return;
                }
                sendIssueMessage(fileType, stem, issueId, message);
            });
        });
    }

    /**
     * Envoie le message de l'utilisateur dans la conversation de résolution
     * d'un problème non résolu précis. C'est l'agent (Gemini) qui décide,
     * tour par tour, d'appeler un outil ou de répondre en clarifiant —
     * l'utilisateur ne déclenche jamais directement une opération fixe.
     */
    async function sendIssueMessage(fileType, stem, issueId, message, decision = {}) {
        if (issueMessagePending) return;
        issueMessagePending = true;
        const controls = [...cleaningResultContainer.querySelectorAll("button, input, textarea")];
        const chats = [...cleaningResultContainer.querySelectorAll(".issue-chat")];
        const drafts = new Map(chats.map(chat => [chat.dataset.issueId, chat.querySelector(".issue-chat-input").value]));
        const activeChat = chats.find(chat => chat.dataset.issueId === issueId);
        const requestStatus = activeChat?.querySelector(".chat-request-status");
        if (requestStatus) requestStatus.textContent = decision.decision === "apply" ? "Application et vérification en cours…" : "L’agent examine votre demande…";
        controls.forEach(control => { control.disabled = true; });
        cleaningResultContainer.setAttribute("aria-busy", "true");
        showMessage("Envoi de votre message à l'agent...", "info");

        try {
            const response = await fetch(`${BASE_API_URL}/cleanings/${fileType}/${stem}/issues/message`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ issue_id: issueId, message, ...decision }),
            });
            const data = await response.json();

            if (!response.ok) {
                showMessage(data.detail || "Erreur lors de l'envoi du message à l'agent.", "error");
                return;
            }

            showMessage("Réponse de l'agent reçue.", "success");
            displayCleaningReport(data, fileType, stem);
            cleaningResultContainer.querySelectorAll(".issue-chat").forEach(element => {
                if (element.dataset.issueId !== issueId || decision.decision) {
                    element.querySelector(".issue-chat-input").value = drafts.get(element.dataset.issueId) || "";
                }
            });
            const chat = [...cleaningResultContainer.querySelectorAll(".issue-chat")]
                .find(element => element.dataset.issueId === issueId);
            if (chat) {
                chat.scrollIntoView({ behavior: "smooth", block: "nearest" });
                chat.querySelector(".issue-chat-input").focus();
            } else {
                const histories = cleaningResultContainer.querySelectorAll("details.profile-sub-section");
                histories.forEach(history => { history.open = true; });
            }
            loadFilesList();
        } catch (err) {
            console.error("Erreur lors de l'envoi du message à l'agent :", err);
            showMessage("Erreur de communication avec le serveur.", "error");
        } finally {
            issueMessagePending = false;
            controls.forEach(control => { control.disabled = false; });
            if (requestStatus) requestStatus.textContent = "";
            cleaningResultContainer.removeAttribute("aria-busy");
        }
    }

    /**
     * Affiche un indicateur de chargement pour le nettoyage
     */
    function showLoadingCleaning(stem) {
        if (!cleaningResultContainer) return;
        cleaningResultContainer.innerHTML = `
            <div class="spinner-container">
                <div class="spinner"></div>
                <div>
                    <strong>🧹 L'Agent nettoie le dataset <em>${escapeHtml(stem)}</em>...</strong>
                    <div style="font-size: 12px; color: #64748b; margin-top: 2px;">
                        Parcours prévu : analyse du profil → plan de nettoyage → génération Pandas → sandbox → validation avant/après → rapport.
                        <br>Traitement en cours ; ces étapes décrivent le parcours, pas une progression mesurée.
                    </div>
                </div>
            </div>
        `;
    }

    /**
     * Active ou désactive l'état de chargement sur l'interface
     */
    function setLoadingState(isLoading) {
        if (isLoading) {
            uploadButton.disabled = true;
            uploadButton.textContent = "Traitement...";
        } else {
            uploadButton.disabled = false;
            uploadButton.textContent = "Valider et importer →";
        }
    }

    /**
     * Réinitialise les zones de contenu
     */
    function clearDisplays() {
        messageContainer.innerHTML = "";
        fileInfoContainer.innerHTML = "";
        if (profileResultContainer) profileResultContainer.innerHTML = "";
        if (cleaningResultContainer) cleaningResultContainer.innerHTML = "";
        resultContainer.innerHTML = "";
    }

    /**
     * Affiche un message d'état (info, success, error)
     */
    function showMessage(msg, type) {
        messageContainer.innerHTML = `<div class="message ${escapeHtml(type)}">${escapeHtml(msg)}</div>`;
    }

    /**
     * Échappe les caractères spéciaux pour se prémunir des failles XSS
     */
    function escapeHtml(text) {
        if (text === null || text === undefined) {
            return "";
        }
        return String(text)
            .replace(/&/g, "&amp;")
            .replace(/</g, "&lt;")
            .replace(/>/g, "&gt;")
            .replace(/"/g, "&quot;")
            .replace(/'/g, "&#039;");
    }

    /**
     * Affiche les métadonnées retournées par l'API
     */
    function displayMetadata(data) {
        const fileTypeClass = `badge-${escapeHtml(data.file_type)}`;

        const html = `
            <div class="info-card">
                <h2>Métadonnées du DataFrame</h2>
                <div class="info-grid">
                    <div class="info-item"><strong>Nom du fichier :</strong> ${escapeHtml(data.filename)}</div>
                    <div class="info-item"><strong>Type d'origine :</strong> <span class="badge ${fileTypeClass}">${escapeHtml(data.file_type)}</span></div>
                    <div class="info-item"><strong>Nombre de lignes :</strong> ${escapeHtml(data.rows)}</div>
                    <div class="info-item"><strong>Nombre de colonnes :</strong> ${escapeHtml(data.columns.length)} (${escapeHtml(data.columns.join(", "))})</div>
                    <div class="info-item"><strong>Fichier source :</strong> <code>${escapeHtml(data.uploaded_file)}</code></div>
                    <div class="info-item"><strong>Fichier DataFrame (.pkl) :</strong> <code class="df-path">${escapeHtml(data.processed_file)}</code></div>
                </div>
            </div>
        `;
        fileInfoContainer.innerHTML = html;
    }

    /**
     * Construit et affiche le tableau HTML complet du DataFrame
     */
    function displayTablePreview(data) {
        const rows = data.data || data.preview;

        if (!rows || rows.length === 0 || !data.columns || data.columns.length === 0) {
            resultContainer.innerHTML = `<p class="subtitle">Aucune donnée à afficher pour ce fichier.</p>`;
            return;
        }

        const columns = data.columns;

        let tableHeaderHtml = "<tr>";
        tableHeaderHtml += `<th style="width: 50px;">#</th>`;
        columns.forEach(col => {
            tableHeaderHtml += `<th>${escapeHtml(col)}</th>`;
        });
        tableHeaderHtml += "</tr>";

        let tableBodyHtml = "";
        rows.forEach((row, index) => {
            tableBodyHtml += "<tr>";
            tableBodyHtml += `<td style="color: #94a3b8; font-weight: 500;">${index + 1}</td>`;
            columns.forEach(col => {
                let cellValue = row[col] !== undefined && row[col] !== null ? row[col] : "";
                if (typeof cellValue === "object") {
                    cellValue = JSON.stringify(cellValue);
                }
                tableBodyHtml += `<td>${escapeHtml(cellValue)}</td>`;
            });
            tableBodyHtml += "</tr>";
        });

        const tableHtml = `
            <div class="table-header-info">
                <h2 class="table-title">Contenu complet du DataFrame</h2>
                <span class="badge badge-csv">${escapeHtml(rows.length)} lignes affichées</span>
            </div>
            <div class="table-wrapper">
                <table class="preview-table">
                    <thead>${tableHeaderHtml}</thead>
                    <tbody>${tableBodyHtml}</tbody>
                </table>
            </div>
        `;

        resultContainer.innerHTML = tableHtml;
    }

    // Public UI bridge used by the service dashboards. Core dataset logic stays
    // in this file; the dashboard only delegates to these existing actions.
    window.InsightFlowData = {
        loadFilesList,
        readFileDataFrame,
        runProfilingAgent,
        viewProfilingReport,
        runCleaningAgent,
        viewCleaningReport,
        downloadCleanedDataset,
        deleteDataset
    };
    window.dispatchEvent(new CustomEvent("insightflow:data-ui-ready"));
});
