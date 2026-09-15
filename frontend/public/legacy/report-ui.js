(() => {
const REPORT_API_BASE = (window.API_BASE_URL || 'http://localhost:8000').replace(/\/$/, '');
const apiUrl = path => /^https?:\/\//i.test(path || '') ? path : `${REPORT_API_BASE}${path || ''}`;
const $ = id => document.getElementById(id);
let selected, result;
const element = (tag, text, cls) => { const node = document.createElement(tag); if (text !== undefined) node.textContent = text; if (cls) node.className = cls; return node; };
function select(file) { selected = file; $('filename').textContent = file?.name || 'No file selected'; }
$('file').addEventListener('change', e => select(e.target.files[0]));
for (const event of ['dragover', 'dragenter']) $('drop').addEventListener(event, e => { e.preventDefault(); $('drop').classList.add('over'); });
for (const event of ['dragleave', 'drop']) $('drop').addEventListener(event, e => { e.preventDefault(); $('drop').classList.remove('over'); });
$('drop').addEventListener('drop', e => { select(e.dataTransfer.files[0]); $('file').required = !selected; });
async function getJSON(url, options) { const response = await fetch(url, options); const data = await response.json(); if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'The request could not be completed.'); return data; }
$('upload').addEventListener('submit', async e => {
  e.preventDefault(); $('status').className = ''; $('results').hidden = true;
  if (!selected || !/\.docx$/i.test(selected.name)) { $('status').textContent = 'Only .docx Word reports are supported.'; $('status').className = 'error'; return; }
  $('extract').disabled = true; $('status').textContent = 'Extracting report text and original assets…';
  try { const body = new FormData(); body.append('file', selected); result = await getJSON(apiUrl('/api/extract'), {method:'POST', body}); render(); $('status').textContent = 'Extraction complete. Original asset bytes preserved.'; window.dispatchEvent(new CustomEvent('insightflow:report-job-created'));  }
  catch (error) { $('status').textContent = error.message; $('status').className = 'error'; }
  finally { $('extract').disabled = false; }
});
function blockNode(block) {
  const li = element('li'); li.append(element('span', `${block.order} · ${block.type}`, 'badge'));
  if (block.text) li.append(element('span', block.text, 'block-text'));
  if (block.assetId) li.append(element('span', block.assetId));
  if (block.cells) { const table = element('table'); for (const row of block.cells) { const tr = element('tr'); for (const cell of row) { const td = element('td'), list = element('ol'); cell.forEach(child => list.append(blockNode(child))); td.append(list); tr.append(td); } table.append(tr); } li.append(table); }
  return li;
}
function render() {
  $('title').textContent = result.report.title; $('summary').textContent = `${result.report.sections.length} sections · ${result.assets.length} original assets`;
  for (const id of ['sections','blocks','gallery','warnings']) $(id).replaceChildren();
  for (const section of result.report.sections) { const article = element('article'); article.append(element('h3', section.title), element('p', section.text)); $('sections').append(article); }
  result.blocks.forEach(block => $('blocks').append(blockNode(block)));
  for (const asset of result.assets) {
    const card = element('article', undefined, 'card'); card.append(element('span','ORIGINAL WORD ASSET','eyebrow'),element('h3',asset.title));
    if (asset.previewSupported) { const img = element('img'); img.src = apiUrl(asset.url); img.alt = asset.title; img.loading = 'lazy'; img.addEventListener('error', () => img.replaceWith(element('p', 'Preview unavailable. Use Open original to access the preserved file.'))); card.append(img); }
    else card.append(element('p','Preview unavailable for this format. The original file is preserved.'));
    const visualContext = asset.context || {};
    const metadata = [
      ['Format', asset.format.toUpperCase()],
      ['Resolution', asset.widthPx != null && asset.heightPx != null ? `${asset.widthPx} × ${asset.heightPx}` : 'Unknown'],
      ['Section', visualContext.sectionTitle || 'Outside main body'],
      ['Associated chart/title', visualContext.chartTitle || visualContext.caption || visualContext.associationText || 'No explicit title detected'],
      ['Association', visualContext.associationMethod ? `${visualContext.associationMethod}${visualContext.associationConfidence != null ? ` · ${Math.round(visualContext.associationConfidence * 100)}%` : ''}` : 'Unknown'],
      ['Order', asset.occurrences.map(c=>c.documentOrder).join(', ') || 'Unplaced'],
      ['SHA-256', asset.sha256]
    ];
    const dl = element('dl'); for (const [name,value] of metadata) dl.append(element('dt',name),element('dd',String(value)));
    const link = element('a','Open original'); link.href = apiUrl(asset.url); link.target = '_blank'; link.rel = 'noopener noreferrer'; card.append(dl,link); $('gallery').append(card);
  }
  if (!result.assets.length) $('gallery').append(element('p','No embedded images found.'));
  result.warnings.forEach(w => $('warnings').append(element('li',w)));
  $('markdown').href = apiUrl(`/api/jobs/${result.jobId}/report`); $('markdown').download = 'report.md'; $('json').hidden = true; $('results').hidden = false;
}
$('raw').addEventListener('click', () => { $('json').textContent = JSON.stringify(result,null,2); $('json').hidden = false; });
$('presentation').addEventListener('click', async () => { try { $('json').textContent = JSON.stringify(await getJSON(apiUrl(`/api/jobs/${result.jobId}/presentation-input`)),null,2); $('json').hidden = false; } catch(error) { $('status').textContent = error.message; $('status').className = 'error'; } });

// -----------------------------------------------------------------------------
// Presentation Assistant — Google ADK planner over existing extractor output
// -----------------------------------------------------------------------------
let presentationContext, presentationPlan, presentationSetupSuggestion;
let agentWorkingTimer = null;

function showAgentWorking(title, detail, steps = []) {
  const panel = $('agent-working');
  $('agent-working-title').textContent = title;
  $('agent-working-detail').textContent = detail;
  const wrap = $('agent-working-steps');
  wrap.replaceChildren();
  steps.forEach(step => wrap.append(element('span', step)));
  panel.hidden = false;
  if (agentWorkingTimer) clearInterval(agentWorkingTimer);
  let index = 0;
  const chips = [...wrap.querySelectorAll('span')];
  const mark = () => { chips.forEach((chip, i) => chip.classList.toggle('active', i === index)); if (chips.length) index = (index + 1) % chips.length; };
  mark();
  agentWorkingTimer = setInterval(mark, 1200);
}

function hideAgentWorking() {
  if (agentWorkingTimer) clearInterval(agentWorkingTimer);
  agentWorkingTimer = null;
  $('agent-working').hidden = true;
}

function renderDownloadPackageSummary(plan) {
  const wrap = $('download-package-files');
  if (!wrap) return;
  wrap.replaceChildren();
  const visuals = [...new Set((plan.slides || []).flatMap(slide => (slide.visuals || []).map(v => v.filename)))];
  wrap.append(element('span', 'presenton_prompt.txt', 'context-chip'));
  wrap.append(element('span', `${plan.slides?.length || 0} planned slides`, 'context-chip'));
  wrap.append(element('span', 'report.docx', 'context-chip'));
  visuals.forEach(name => wrap.append(element('span', name, 'context-chip')));
  if (!visuals.length) wrap.append(element('span', 'No visual selected by agent', 'context-chip'));
}

function sourceItem(title, detail) {
  const item = element('div', undefined, 'source-item');
  item.append(element('strong', title), element('span', detail || ''));
  return item;
}

async function loadPresentationContext() {
  if (!result?.jobId) return;
  const status = $('presentation-status');
  status.className = '';
  status.textContent = 'Reading extracted report sources…';
  presentationContext = await getJSON(apiUrl(`/api/jobs/${result.jobId}/presentation/context`));
  $('presentation-context-summary').replaceChildren(
    element('span', `${presentationContext.sectionCount} sections`, 'context-chip'),
    element('span', `${presentationContext.tableCount} Word tables`, 'context-chip'),
    element('span', `${presentationContext.assetCount} original visuals`, 'context-chip'),
    element('span', presentationContext.agentConfigured ? `Agent ready · ${presentationContext.model}` : `Agent not configured · ${presentationContext.model}`, 'context-chip')
  );
  for (const id of ['source-sections','source-tables','source-assets']) $(id).replaceChildren();
  presentationContext.sections.forEach(s => $('source-sections').append(sourceItem(s.title, s.id)));
  presentationContext.tables.forEach(t => $('source-tables').append(sourceItem(t.title, `${t.id} · ${t.rowCount} rows × ${t.columnCount} cols`)));
  presentationContext.assets.forEach(a => {
    const association = a.chartTitle || a.associationText || a.sectionTitle || 'No strong association found';
    const confidence = a.associationConfidence != null ? ` · ${Math.round(a.associationConfidence * 100)}% match context` : '';
    $('source-assets').append(sourceItem(a.title || a.filename, `${a.id} · ${a.filename} · ↔ ${association}${confidence}`));
  });
  $('source-material').hidden = false;
  status.textContent = presentationContext.agentConfigured
    ? 'Extractor sources are ready. Tell the Presentation Agent what you need.'
    : 'Extractor sources are ready, but the Presentation Agent needs a Google AI Studio Gemini API key in backend/.env.';
}

async function suggestPresentationSetup() {
  if (!result?.jobId) return;
  const status = $('presentation-status');
  status.className = '';
  status.textContent = 'Presentation Agent is preparing setup recommendations…';
  showAgentWorking(
    'Analyzing the report before proposing your presentation',
    'The agent is reading the extracted structure and evidence. These recommendations stay editable.',
    ['Read report structure', 'Review findings', 'Match original visuals', 'Estimate audience', 'Estimate slides & duration']
  );
  try {
    const suggestion = await getJSON(apiUrl(`/api/jobs/${result.jobId}/presentation/suggest-setup`), {method:'POST'});
    presentationSetupSuggestion = suggestion;
    $('pref-audience').value = suggestion.audience;
    $('pref-objective').value = suggestion.objective;
    $('pref-slides').value = suggestion.slideCount;
    $('pref-duration').value = suggestion.durationMinutes;
    $('setup-suggestion-note').hidden = false;
    status.textContent = 'Suggested settings are ready. Review or edit them, then generate the presentation plan.';
    status.className = 'success';
  } finally {
    hideAgentWorking();
  }
}

$('prepare-presentation').addEventListener('click', async () => {
  $('presentation-workspace').hidden = false;
  $('presentation-workspace').scrollIntoView({behavior:'smooth', block:'start'});
  try {
    await loadPresentationContext();
    if (presentationContext.agentConfigured) await suggestPresentationSetup();
  }
  catch (error) { $('presentation-status').textContent = error.message; $('presentation-status').className = 'error'; }
});

$('refresh-setup').addEventListener('click', async () => {
  const button = $('refresh-setup');
  button.disabled = true;
  try { await suggestPresentationSetup(); }
  catch (error) { $('presentation-status').textContent = error.message; $('presentation-status').className = 'error'; }
  finally { button.disabled = false; }
});

function renderPresentationPlan(plan, changedSlideIds = []) {
  presentationPlan = plan;
  // Keep editable setup controls synchronized with changes made through chat.
  $('pref-audience').value = plan.audience || '';
  $('pref-objective').value = plan.objective || '';
  $('pref-language').value = plan.language || 'English';
  $('pref-duration').value = plan.durationMinutes ?? '';
  $('pref-slides').value = plan.slideCount;
  $('pref-style').value = plan.style || 'Professional';
  $('plan-title').textContent = plan.title;
  $('plan-count').textContent = `${plan.slideCount} SLIDES`;
  $('plan-slides').replaceChildren();
  for (const slide of plan.slides) {
    const card = element('article', undefined, changedSlideIds.includes(slide.id) ? 'plan-card changed-slide' : 'plan-card');
    const meta = element('div', undefined, 'plan-meta');
    meta.append(element('span', `SLIDE ${slide.order}`, 'badge'), element('span', slide.purpose, 'badge'), element('span', slide.storyRole || 'evidence', slide.storyRole === 'closing' ? 'badge closing-badge' : 'badge'));
    card.append(meta, element('h4', slide.title), element('p', slide.keyMessage));
    if (slide.bullets?.length) {
      const list = element('ul'); slide.bullets.forEach(b => list.append(element('li', b))); card.append(list);
    }
    if (slide.claims?.length) {
      const claims = element('div', undefined, 'claim-list');
      for (const claim of slide.claims) {
        const row = element('div', undefined, 'claim-row');
        row.append(element('span', claim.type, `claim-badge claim-${claim.type}`), element('span', claim.text, 'claim-text'));
        const refs = (claim.sources || []).map(s => `${s.sourceType}:${s.sourceId}`).join(' · ');
        if (refs) row.append(element('small', `Source: ${refs}`, 'claim-source'));
        claims.append(row);
      }
      card.append(claims);
    }
    if (slide.sources?.length) {
      const refs = slide.sources.map(s => `${s.sourceType}:${s.sourceId}`).join(' · ');
      card.append(element('p', `Slide sources: ${refs}`, 'slide-sources'));
    }
    for (const visual of slide.visuals || []) {
      const visualWrap = element('div', undefined, 'plan-visual');
      visualWrap.append(element('span', `ORIGINAL · ${visual.filename}`, 'original-visual'));
      if (visual.title) visualWrap.append(element('strong', visual.title));
      if (visual.associationText) visualWrap.append(element('small', `Matched from Word context: ${visual.associationText}`));
      card.append(visualWrap);
    }
    $('plan-slides').append(card);
  }
  renderDownloadPackageSummary(plan);
  if ($('download-package')) $('download-package').disabled = false;
  $('plan-result').hidden = false;
}

$('presentation-form').addEventListener('submit', async e => {
  e.preventDefault();
  if (!result?.jobId) return;
  const status = $('presentation-status');
  const button = $('generate-plan');
  button.disabled = true; status.className = ''; status.textContent = 'Google ADK agent is selecting the useful report data and planning the presentation…';
  showAgentWorking(
    'Building the presentation story',
    'The agent is grounding the plan in your extractor output before any Presenton handoff.',
    ['Select strongest evidence', 'Validate fact provenance', 'Match visuals', 'Organize narrative', 'Build closing recommendations']
  );
  const durationRaw = $('pref-duration').value.trim();
  const payload = {
    preferences: {
      audience: $('pref-audience').value.trim() || 'General audience',
      objective: $('pref-objective').value.trim() || 'Present the most important findings',
      language: $('pref-language').value.trim() || 'English',
      durationMinutes: durationRaw ? Number(durationRaw) : null,
      slideCount: Number($('pref-slides').value || presentationSetupSuggestion?.slideCount || 6),
      style: $('pref-style').value.trim() || 'Professional',
      instructions: $('pref-instructions').value.trim() || null
    }
  };
  try {
    const turn = await getJSON(apiUrl(`/api/jobs/${result.jobId}/presentation/plan`), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(payload)});
    $('agent-plan-message').textContent = turn.assistantMessage;
    renderPresentationPlan(turn.plan);
    await refreshPresentonPrompt();
    await loadPresentationConversation();
    status.textContent = 'Presentation plan ready. Review the selected sources, story and Presenton prompt.'; status.className = 'success';
  } catch (error) {
    status.textContent = error.message; status.className = 'error';
  } finally { button.disabled = false; hideAgentWorking(); }
});

$('copy-presenton-prompt').addEventListener('click', async () => {
  try { await navigator.clipboard.writeText($('presenton-prompt').value); $('presentation-status').textContent = 'Presenton prompt copied.'; $('presentation-status').className = 'success'; }
  catch { $('presenton-prompt').select(); document.execCommand('copy'); }
});


async function refreshPresentonPrompt() {
  if (!result?.jobId) return;
  const response = await fetch(apiUrl(`/api/jobs/${result.jobId}/presentation/presenton-prompt`));
  if (!response.ok) throw new Error('Could not load Presenton prompt.');
  $('presenton-prompt').value = await response.text();
}

function chatMessageNode(message) {
  const role = message.role === 'user' ? 'user' : 'assistant';
  const wrap = element('div', undefined, `chat-message ${role}`);
  wrap.append(element('span', role === 'user' ? 'You' : 'Presentation Agent', 'chat-role'));
  wrap.append(element('span', message.content));
  return wrap;
}

function chatThinkingNode() {
  const wrap = element('div', undefined, 'chat-message assistant thinking-message');
  wrap.append(element('span', 'Presentation Agent', 'chat-role'));
  const line = element('span', undefined, 'thinking-line');
  line.append(element('span', 'Working on your request'));
  const dots = element('span', undefined, 'thinking-dots');
  dots.append(element('i'), element('i'), element('i'));
  line.append(dots);
  wrap.append(line);
  return wrap;
}

function renderPresentationConversation(conversation) {
  const log = $('presentation-chat-log');
  log.replaceChildren();
  const messages = conversation?.messages || [];
  if (!messages.length) {
    log.append(chatMessageNode({role:'assistant', content:'The plan is ready. Tell me what you want to change or ask about the presentation story.'}));
  } else {
    messages.forEach(message => log.append(chatMessageNode(message)));
  }
  log.scrollTop = log.scrollHeight;
}

async function loadPresentationConversation() {
  if (!result?.jobId || !presentationPlan) return;
  const conversation = await getJSON(apiUrl(`/api/jobs/${result.jobId}/presentation/conversation`));
  renderPresentationConversation(conversation);
}

async function sendPresentationChat(message) {
  if (!result?.jobId || !presentationPlan) return;
  const text = message.trim();
  if (!text) return;
  const status = $('presentation-chat-status');
  const send = $('presentation-chat-send');
  const input = $('presentation-chat-input');
  const log = $('presentation-chat-log');

  log.append(chatMessageNode({role:'user', content:text}));
  log.scrollTop = log.scrollHeight;
  input.value = '';
  send.disabled = true;
  status.className = '';
  status.textContent = 'Agent is reviewing the current plan and source evidence…';
  const thinking = chatThinkingNode();
  log.append(thinking);
  log.scrollTop = log.scrollHeight;
  showAgentWorking(
    'Refining the current presentation plan',
    'The agent is checking your request against the current plan and protected source facts.',
    ['Understand your request', 'Compare current slides', 'Check source evidence', 'Apply focused changes', 'Revalidate plan']
  );

  try {
    const turn = await getJSON(apiUrl(`/api/jobs/${result.jobId}/presentation/chat`), {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({message:text})
    });
    thinking.remove();
    log.append(chatMessageNode({role:'assistant', content:turn.assistantMessage}));
    log.scrollTop = log.scrollHeight;
    renderPresentationPlan(turn.plan, turn.changedSlideIds || []);
    await refreshPresentonPrompt();
    const changed = turn.changedSlideIds?.length || 0;
    const warningText = (turn.warnings || []).join(' ');
    status.textContent = turn.planChanged
      ? `Plan updated${changed ? ` · ${changed} slide${changed === 1 ? '' : 's'} changed` : ''}.${warningText ? ` ${warningText}` : ''}`
      : `No plan change. ${warningText}`.trim();
    status.className = turn.warnings?.length ? 'error' : 'success';
    $('agent-plan-message').textContent = turn.assistantMessage;
  } catch (error) {
    thinking.remove();
    status.textContent = error.message;
    status.className = 'error';
    // Reload the persisted history so a failed optimistic user bubble does not remain misleadingly.
    try { await loadPresentationConversation(); } catch {}
  } finally {
    hideAgentWorking();
    send.disabled = false;
    input.focus();
  }
}

$('presentation-chat-form').addEventListener('submit', async e => {
  e.preventDefault();
  await sendPresentationChat($('presentation-chat-input').value);
});

for (const button of document.querySelectorAll('.quick-prompt')) {
  button.addEventListener('click', () => sendPresentationChat(button.dataset.message || ''));
}

$('clear-presentation-chat').addEventListener('click', async () => {
  if (!result?.jobId) return;
  const button = $('clear-presentation-chat');
  button.disabled = true;
  try {
    const conversation = await getJSON(apiUrl(`/api/jobs/${result.jobId}/presentation/conversation`), {method:'DELETE'});
    renderPresentationConversation(conversation);
    $('presentation-chat-status').textContent = 'Conversation cleared. The current presentation plan was kept.';
    $('presentation-chat-status').className = 'success';
  } catch (error) {
    $('presentation-chat-status').textContent = error.message;
    $('presentation-chat-status').className = 'error';
  } finally {
    button.disabled = false;
  }
});


$('download-package').addEventListener('click', async () => {
  if (!result?.jobId || !presentationPlan) return;
  const button = $('download-package');
  const status = $('download-package-status');
  const loading = $('download-package-loading');
  button.disabled = true;
  status.className = '';
  status.textContent = 'Preparing ZIP with the latest prompt, report and selected original visuals…';
  loading.hidden = false;
  try {
    const response = await fetch(apiUrl(`/api/jobs/${result.jobId}/presentation/download-package`));
    if (!response.ok) {
      let message = 'Could not prepare the presentation package.';
      try { const data = await response.json(); if (typeof data.detail === 'string') message = data.detail; } catch {}
      throw new Error(message);
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = 'presentation-handoff.zip';
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    status.textContent = 'Package ready. Nothing was sent automatically to Presenton or any external service.';
    status.className = 'success';
  } catch (error) {
    status.textContent = error.message;
    status.className = 'error';
  } finally {
    loading.hidden = true;
    button.disabled = false;
  }
});

})();
