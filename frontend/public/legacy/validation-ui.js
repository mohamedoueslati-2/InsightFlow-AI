window.ValidationUI = (() => {
    const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[c]));
    function stage(name) {
        document.querySelectorAll('[data-step]').forEach(element => {
            if (element.dataset.step === name) element.setAttribute('aria-current', 'step');
            else element.removeAttribute('aria-current');
        });
    }
    function render(report, onContinue) {
        const container = document.getElementById('validationResult');
        const accepted = report.status === 'accepted';
        stage('validation');
        container.innerHTML = `<article class="validation-card ${accepted ? 'accepted' : 'rejected'}">
            <div class="validation-heading"><span class="validation-icon">${accepted ? '✓' : '!'}</span><div><span class="eyebrow">ÉTAPE 01 · VALIDATION TECHNIQUE</span><h2>${accepted ? 'Votre fichier est prêt' : 'Le fichier doit être corrigé'}</h2></div></div>
            <p>${escape(report.message)}</p>
            ${accepted ? `<div class="validation-facts"><span><strong>${escape(report.rows)}</strong> lignes</span><span><strong>${escape(report.columns_count)}</strong> colonnes</span><span><strong>${escape(report.format?.toUpperCase())}</strong> format</span></div>
                <div class="validation-checks">${(report.checks || []).map(check => `<span>✓ ${escape(check)}</span>`).join('')}</div>
                ${(report.warnings || []).map(w => `<p class="validation-warning">${escape(w)}</p>`).join('')}
                <p class="validation-note">Le fichier est lisible. Le profilage vérifiera ensuite les valeurs manquantes, les doublons et les anomalies.</p>
                ${onContinue ? '<button class="btn-primary" id="continueProfiling">Continuer vers le profilage →</button>' : ''}` : `<p class="validation-warning">${escape(report.suggested_fix || '')}</p><p class="validation-note">Aucun fichier rejeté n’a été enregistré. Corrigez le fichier puis importez-le à nouveau.</p>`}
            <details><summary>Détails techniques</summary><pre>${escape(JSON.stringify(report.details || {reason: report.reason}, null, 2))}</pre></details>
        </article>`;
        container.querySelector('#continueProfiling')?.addEventListener('click', onContinue);
    }
    return {stage, render};
})();
