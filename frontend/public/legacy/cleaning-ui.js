/* Presentational helpers only: generated Python is always escaped, never executed. */
window.CleaningUI = (() => {
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    function code(value) {
        return value ? `<details class="generated-code"><summary>Voir le code généré</summary><pre><code>${escape(value)}</code></pre></details>` : '';
    }
    function risk(value) {
        const level = ['low', 'medium', 'high'].includes(value?.level) ? value.level : 'medium';
        return `<span class="risk-${level}">${{low:'Risque faible', medium:'Risque moyen', high:'Risque élevé'}[level]}</span>
            ${value?.requires_human_review ? '<span class="review-required-badge">Validation humaine requise</span>' : ''}`;
    }
    function validation(value) {
        if (!value) return '';
        return `<div class="validation-grid"><span>${value.passed ? '✅ Validation réussie' : '❌ Validation refusée'}</span>
            <span>${escape(value.changed_cells || 0)} cellules modifiées</span>
            <span>${escape(value.new_missing_values || 0)} nouvelles valeurs manquantes</span></div>
            ${(value.warnings || []).map(w => `<p class="proposal-impact">${escape(w)}</p>`).join('')}`;
    }
    function proposal(p) {
        if (!p.generated_code) return '';
        return `${risk(p.risk)}${validation(p.validation)}<span class="sandbox-status">📦 Aperçu exécuté en sandbox · ${escape(p.attempt_count || 1)} tentative(s)</span>${code(p.generated_code)}`;
    }
    function plan(data) {
        const allItems = Array.isArray(data.plan) ? data.plan : data.plan?.items || [];
        // Keep execution history; show only the latest advice per profiling finding.
        const items = allItems.filter((item, index) => item.intent !== 'recommend' ||
            !allItems.slice(index + 1).some(other => other.intent === 'recommend' && other.problem_id === item.problem_id));
        if (!data.run_id && !items.length) return '';
        return `<section class="cleaning-plan"><h3>🧠 Plan de nettoyage IA</h3>
            <p>${escape(data.plan?.dataset_understanding || '')}</p>
            ${data.sandbox?.error ? `<p class="proposal-impact">${escape(data.sandbox.error)}</p>` : ''}
            ${items.map((item, index) => `<article class="plan-item"><h4>${index+1}. ${escape(item.strategy)}</h4>
                <p>Problème : ${escape(item.problem_id)} · Colonnes : ${escape(item.columns.join(', ') || 'Dataset')}</p>
                <p>${escape(item.diagnosis)}</p><p>Effet attendu : ${escape(item.expected_effect)}</p>
                ${risk({level:item.risk_hint, requires_human_review:item.requires_human_review})}
                <span class="sandbox-status">${escape({planned:'Planifié', awaiting_decision:'Recommandation sans modification — décision attendue', waiting_review:'En attente de revue', applied:'Appliqué', stale:'Aperçu périmé — à régénérer', dismissed:'Proposition écartée', failed:'Échec de préparation — consulter les tentatives'}[item.status] || item.status)}</span>
            </article>`).join('') || '<p>Aucune transformation planifiée.</p>'}
        </section>`;
    }
    function attempts(data) {
        const attempts = data.code_attempts || [];
        if (!attempts.length) return '';
        const labels = {generating:'💻 Génération', safety_passed:'🛡 Contrôle statique réussi', validation_passed:'📦 Sandbox exécuté · ✅ Validation réussie', sandbox_error:'❌ Erreur sandbox', validation_failed:'❌ Validation refusée', failed:'❌ Tentative refusée'};
        return `<details class="execution-timeline"><summary>Exécution et tentatives (${attempts.length})</summary>${attempts.map(a => `<div class="execution-step code-attempt"><strong>${escape(a.plan_item_id)} · Tentative ${escape(a.attempt)}</strong><p>${escape(labels[a.stage] || a.stage)}</p><pre>${escape(a.error || a.execution?.exception || '')}</pre></div>`).join('')}</details>`;
    }
    return {code, risk, validation, proposal, plan, attempts};
})();
