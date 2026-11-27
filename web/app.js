const $ = (selector, root = document) => root.querySelector(selector);
const authView = $('#auth-view');
const dashboard = $('#dashboard');
const authForm = $('#auth-form');
const eventDialog = $('#event-dialog');
const eventForm = $('#event-form');
let authMode = 'login';
let eventsCache = [];
let editingEventId = null;

async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  let data = {};
  try { data = await response.json(); } catch { /* respuesta sin JSON */ }
  if (!response.ok) throw new Error(data.error || `Ocurrió un error (${response.status}).`);
  return data;
}
function toast(message) {
  const el = $('#toast'); el.textContent = message; el.classList.add('show');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 3400);
}
function esc(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[ch]));
}
function fmtPercent(value) { return `${Number(value).toFixed(2).replace(/\.00$/, '')}%`; }
function fmtAge(minutes) {
  if (minutes < 1) return 'hace <1 min';
  if (minutes < 60) return `hace ${Math.round(minutes)} min`;
  if (minutes < 1440) return `hace ${(minutes / 60).toFixed(1)} h`;
  return `hace ${(minutes / 1440).toFixed(1)} d`;
}
function applySettings(settings) {
  document.querySelectorAll('.threshold-text').forEach(el => { el.textContent = fmtPercent(settings.threshold_percent); });
  $('#max-age-text').textContent = settings.max_age_minutes;
}

function setMode(mode) {
  authMode = mode;
  document.querySelectorAll('.auth-tab').forEach(tab => tab.classList.toggle('active', tab.dataset.mode === mode));
  $('#auth-title').textContent = mode === 'login' ? 'Qué bueno verte.' : 'Crea tu espacio.';
  $('#auth-description').textContent = mode === 'login' ? 'Ingresa para revisar tus eventos y alertas.' : 'Tu correo será también el destino inicial de tus alertas.';
  $('#auth-submit').innerHTML = mode === 'login' ? 'Entrar al panel <span>→</span>' : 'Crear cuenta <span>→</span>';
  authForm.elements.password.autocomplete = mode === 'login' ? 'current-password' : 'new-password';
  $('#auth-error').textContent = '';
}
document.querySelectorAll('.auth-tab').forEach(tab => tab.addEventListener('click', () => setMode(tab.dataset.mode)));

authForm.addEventListener('submit', async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(authForm));
  try {
    await api(`/api/${authMode}`, { method: 'POST', body: JSON.stringify(values) });
    authForm.reset(); await showDashboard();
  } catch (error) { $('#auth-error').textContent = error.message; }
});

$('#logout').addEventListener('click', async () => {
  await api('/api/logout', { method: 'POST', body: '{}' });
  dashboard.classList.add('hidden'); authView.classList.remove('hidden'); $('#logout').classList.add('hidden');
});

function openEventDialog(eventData = null) {
  editingEventId = eventData ? eventData.id : null;
  eventForm.reset();
  $('#event-error').textContent = '';
  $('#dialog-eyebrow').textContent = eventData ? 'EDITAR MERCADO' : 'NUEVO MERCADO';
  $('#dialog-title').textContent = eventData ? 'Edita el evento' : 'Registra un evento';
  $('#dialog-help').textContent = eventData
    ? 'Si quitas un resultado, también se borran las cuotas capturadas para él.'
    : 'Escribe todos los resultados posibles de este mercado para calcularlo correctamente.';
  $('#save-event').textContent = eventData ? 'Guardar cambios' : 'Crear evento';
  if (eventData) {
    eventForm.elements.name.value = eventData.name;
    eventForm.elements.sport.value = eventData.sport;
    eventForm.elements.market.value = eventData.market;
    eventForm.elements.outcomes.value = eventData.outcomes.join(', ');
    eventForm.elements.starts_at.value = eventData.starts_at;
  }
  eventDialog.showModal();
}
$('#new-event-button').addEventListener('click', () => openEventDialog());
$('#empty-new-event').addEventListener('click', () => openEventDialog());
eventForm.addEventListener('submit', async event => {
  if (event.submitter?.value === 'cancel') return;
  event.preventDefault();
  const values = Object.fromEntries(new FormData(eventForm));
  values.outcomes = values.outcomes.split(',').map(value => value.trim()).filter(Boolean);
  $('#event-error').textContent = '';
  try {
    if (editingEventId) {
      const result = await api(`/api/events/${editingEventId}`, { method: 'PUT', body: JSON.stringify(values) });
      toast(result.removed_odds ? `Evento actualizado · ${result.removed_odds} cuota(s) eliminadas` : 'Evento actualizado.');
    } else {
      await api('/api/events', { method: 'POST', body: JSON.stringify(values) });
      toast('Evento agregado. Ahora captura sus cuotas.');
    }
    eventDialog.close(); await loadEvents();
  } catch (error) { $('#event-error').textContent = error.message; }
});

$('#email-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api('/api/profile', { method: 'POST', body: JSON.stringify({ alert_email: $('#alert-email').value }) });
    $('#email-status').textContent = 'Correo de alertas guardado.'; $('#email-status').style.color = '#187c4b';
  } catch (error) { $('#email-status').textContent = error.message; $('#email-status').style.color = '#ba4b43'; }
});
$('#settings-form').addEventListener('submit', async event => {
  event.preventDefault();
  const status = $('#settings-status');
  try {
    const result = await api('/api/profile', { method: 'POST', body: JSON.stringify({
      threshold_percent: Number($('#threshold-input').value),
      max_age_minutes: Number($('#max-age-input').value),
    }) });
    applySettings(result);
    status.textContent = 'Ajustes guardados.'; status.style.color = '#187c4b';
    await loadEvents();
    loadIntegrations();
  } catch (error) { status.textContent = error.message; status.style.color = '#ba4b43'; }
});

async function addOdds(eventId, card) {
  const form = $('.capture-row', card);
  const values = Object.fromEntries(new FormData(form));
  try {
    const result = await api(`/api/events/${eventId}/odds`, { method: 'POST', body: JSON.stringify({ ...values, decimal_odds: Number(values.decimal_odds) }) });
    await loadEvents();
    if (result.alert?.created) toast(`Alerta generada: ${result.alert.probability_percent.toFixed(2)}%${result.alert.status === 'sent' ? ' · correo enviado' : ' · correo pendiente de configurar'}`);
    else toast('Cuota guardada.');
  } catch (error) { toast(error.message); }
}
async function deleteEvent(eventData) {
  if (!confirm(`¿Eliminar "${eventData.name}" con todas sus cuotas y alertas?`)) return;
  try { await api(`/api/events/${eventData.id}`, { method: 'DELETE' }); await loadEvents(); toast('Evento eliminado.'); }
  catch (error) { toast(error.message); }
}
async function deleteOdds(eventId, oddsId) {
  if (!confirm('¿Eliminar esta captura de cuota?')) return;
  try { await api(`/api/events/${eventId}/odds/${oddsId}`, { method: 'DELETE' }); await loadEvents(); toast('Cuota eliminada.'); }
  catch (error) { toast(error.message); }
}

function renderHistory(event) {
  const rows = event.calculation.history;
  if (!rows.length) return '';
  const body = rows.map(row => {
    const state = !row.current ? 'Reemplazada' : row.fresh ? 'Vigente' : 'Vencida';
    const cls = !row.current ? 'is-old' : row.fresh ? '' : 'is-stale';
    return `<tr class="${cls}"><td>${esc(row.outcome)}</td><td>${esc(row.bookmaker)}</td><td>${esc(row.decimal_odds)}</td><td>${row.source && row.source !== 'manual' ? 'API' : 'Manual'}</td><td>${fmtAge(row.age_minutes)}</td><td>${state}</td><td><button class="link-button" type="button" data-delete-odds="${row.id}">Eliminar</button></td></tr>`;
  }).join('');
  return `<details class="history"><summary>Historial de capturas (${rows.length})</summary><table><thead><tr><th>Resultado</th><th>Casa</th><th>Cuota</th><th>Origen</th><th>Capturada</th><th>Estado</th><th></th></tr></thead><tbody>${body}</tbody></table></details>`;
}

function renderEvent(event) {
  const calc = event.calculation;
  const card = document.createElement('article'); card.className = 'event-card';
  const startText = event.starts_at ? ` · ${esc(/Z$|[+-]\d\d:\d\d$/.test(event.starts_at) ? new Date(event.starts_at).toLocaleString('es-MX', { dateStyle: 'medium', timeStyle: 'short' }) : event.starts_at.replace('T', ' '))}` : '';
  const tableRows = event.outcomes.map(outcome => {
    const quote = calc.best_odds[outcome];
    const stale = calc.stale_outcomes.includes(outcome);
    return `<tr><td>${esc(outcome)}</td><td class="${quote ? 'odds-best' : ''}">${quote ? esc(quote.decimal_odds) : '—'}</td><td>${quote ? esc(quote.bookmaker) : stale ? 'Solo cuotas vencidas' : 'Sin cuota'}</td><td>${quote ? `${new Date(quote.observed_at).toLocaleString('es-MX')} (${fmtAge(quote.age_minutes)})` : '—'}</td></tr>`;
  }).join('');
  const alert = event.alerts[0];
  const alertNote = alert ? `<div class="alert-note ${alert.status !== 'sent' ? 'pending' : ''}">${alert.status === 'sent' ? '✓ Correo enviado' : '◷ Alerta pendiente'} · ${esc(alert.message)} (${(alert.probability_sum * 100).toFixed(2)}%)</div>` : '';
  const staleNote = calc.ignored_count ? `<div class="stale-note">Se ignoraron ${calc.ignored_count} cuota(s) con más de ${calc.max_age_minutes} min de antigüedad. Vuelve a capturarlas para usarlas.</div>` : '';
  const validUntil = calc.valid_until ? ` · válido hasta ${new Date(calc.valid_until).toLocaleTimeString('es-MX', { hour: '2-digit', minute: '2-digit' })}` : '';
  const pending = calc.stale_outcomes.length ? 'Faltan cuotas vigentes para completar el mercado' : 'Faltan resultados para completar el mercado';
  card.innerHTML = `<div class="event-header"><div><h3 class="event-title">${esc(event.name)}${event.source && event.source !== 'manual' ? `<span class="source-badge">API · ${esc(providerTitle(event.source))}</span>` : ''}</h3><div class="event-meta">${esc(event.sport || 'Deporte sin especificar')}${startText}</div><div class="event-actions"><button class="button button-quiet button-small" type="button" data-action="edit">Editar</button><button class="button button-quiet button-small button-danger" type="button" data-action="delete">Eliminar</button></div></div><span class="market-badge">${esc(event.market)}</span></div>
    <div class="calc-strip"><div><div class="calc-label">Suma de probabilidades implícitas</div><div class="calc-value ${calc.qualifies ? 'good' : calc.complete ? '' : 'wait'}">${calc.probability_percent === null ? '—' : `${calc.probability_percent.toFixed(2)}%`}</div><div class="calc-sub">${calc.complete ? `Underround teórico: ${calc.underround_percent.toFixed(2)}%${validUntil}` : pending}</div></div><span class="status-pill ${calc.qualifies ? 'good' : calc.complete ? '' : 'incomplete'}">${calc.qualifies ? 'Umbral alcanzado' : calc.complete ? 'En seguimiento' : 'Incompleto'}</span></div>
    <div class="odds-table-wrap"><table class="odds-table"><thead><tr><th>RESULTADO</th><th>MEJOR CUOTA VIGENTE</th><th>CASA</th><th>CAPTURADA</th></tr></thead><tbody>${tableRows}</tbody></table></div>
    ${staleNote}${alertNote}<form class="capture-row"><label>Resultado<select name="outcome" required>${event.outcomes.map(outcome => `<option value="${esc(outcome)}">${esc(outcome)}</option>`).join('')}</select></label><label>Casa<input name="bookmaker" required placeholder="Casa de juego"></label><label>Cuota decimal<input name="decimal_odds" type="number" min="1.01" max="100000" step="0.01" required placeholder="2.10"></label><button class="button button-secondary" type="submit">Guardar cuota</button></form>${renderHistory(event)}`;
  $('.capture-row', card).addEventListener('submit', submitEvent => { submitEvent.preventDefault(); addOdds(event.id, card); });
  $('[data-action="edit"]', card).addEventListener('click', () => openEventDialog(event));
  $('[data-action="delete"]', card).addEventListener('click', () => deleteEvent(event));
  card.querySelectorAll('[data-delete-odds]').forEach(button => button.addEventListener('click', () => deleteOdds(event.id, button.dataset.deleteOdds)));
  return card;
}
async function loadEvents() {
  const openHistories = new Set([...document.querySelectorAll('.event-card details[open]')].map(el => el.closest('.event-card').dataset.id));
  const data = await api('/api/events'); eventsCache = data.events;
  applySettings(data);
  const cards = eventsCache.map(event => { const card = renderEvent(event); card.dataset.id = event.id; if (openHistories.has(String(event.id))) $('details', card)?.setAttribute('open', ''); return card; });
  $('#events-list').replaceChildren(...cards);
  $('#empty-state').classList.toggle('hidden', eventsCache.length > 0);
  $('#stat-events').textContent = eventsCache.length;
  $('#stat-complete').textContent = eventsCache.filter(event => event.calculation.complete).length;
  $('#stat-alerts').textContent = eventsCache.filter(event => event.calculation.qualifies).length;
}
async function showDashboard() {
  try {
    const me = await api('/api/me');
    $('#alert-email').value = me.alert_email;
    $('#threshold-input').value = Number(me.threshold_percent.toFixed(2));
    $('#max-age-input').value = me.max_age_minutes;
    applySettings(me);
    authView.classList.add('hidden'); dashboard.classList.remove('hidden'); $('#logout').classList.remove('hidden');
    await loadEvents();
    loadIntegrations();
  } catch { dashboard.classList.add('hidden'); authView.classList.remove('hidden'); $('#logout').classList.add('hidden'); }
}
showDashboard();

// ------------------------------------------------------------ integraciones
let integrations = { providers: [], subscriptions: [] };
let sportsLoadedFor = null;
function providerTitle(key) {
  return integrations.providers.find(p => p.key === key)?.title || ({ 'the-odds-api': 'The Odds API' }[key] ?? key);
}
function fmtDate(value) {
  return value ? new Date(value).toLocaleString('es-MX', { dateStyle: 'short', timeStyle: 'short' }) : 'nunca';
}
async function loadIntegrations() {
  try { integrations = await api('/api/integrations'); }
  catch (error) { $('#integration-status').textContent = error.message; return; }
  const provider = integrations.providers[0];
  if (!provider) return;
  const status = $('#integration-status');
  const form = $('#subscription-form');
  if (!provider.configured) {
    status.className = 'integration-status warn';
    status.innerHTML = `<strong>${esc(provider.title)} no está configurado.</strong> Crea una clave gratuita en the-odds-api.com y define la variable <code>${esc(provider.api_key_env)}</code> antes de iniciar el servidor:<br><code>$env:${esc(provider.api_key_env)} = "tu-clave"; python server.py</code>`;
  } else {
    const st = provider.status || {};
    const remaining = st.requests_remaining ?? null;
    const estimate = integrations.estimated_monthly_credits;
    const tight = remaining !== null && estimate > remaining;
    status.className = `integration-status${st.last_error || tight || integrations.max_age_minutes < integrations.interval_minutes ? ' warn' : ''}`;
    status.innerHTML = `<strong>${esc(provider.title)}</strong> · sincronización automática cada ${integrations.interval_minutes} min, eventos de las próximas ${integrations.horizon_hours} h.<br>
      Última sincronización: ${fmtDate(st.last_sync_at)} · Créditos restantes: ${remaining ?? '—'} · Consumo estimado: ~${estimate.toLocaleString('es-MX')} créditos/mes${tight ? ' (supera tus créditos restantes: sigue menos deportes/regiones o sube ODDS_API_INTERVAL_MINUTES)' : ''}
      ${st.last_error ? `<br>Último error: ${esc(st.last_error)}` : ''}
      ${integrations.max_age_minutes < integrations.interval_minutes ? `<br><strong>Atención:</strong> tu vigencia de cuotas (${integrations.max_age_minutes} min) es menor que el intervalo de sincronización (${integrations.interval_minutes} min), así que las cuotas de la API se marcarán como vencidas entre una sincronización y otra. Sube la vigencia a ${integrations.interval_minutes} min o más, o reduce <code>ODDS_API_INTERVAL_MINUTES</code> (consume más créditos).` : ''}`;
  }
  form.querySelectorAll('select, input, button').forEach(el => { el.disabled = !provider.configured; });
  $('#sync-now').disabled = !provider.configured || !integrations.subscriptions.length;
  const regions = $('#region-options');
  if (!regions.children.length) {
    regions.innerHTML = Object.entries(provider.regions).map(([key, label]) =>
      `<label><input type="checkbox" name="region" value="${esc(key)}" ${key === 'us' ? 'checked' : ''}> ${esc(label)}</label>`).join('');
    regions.querySelectorAll('input').forEach(el => { el.disabled = !provider.configured; });
  }
  if (provider.configured && sportsLoadedFor !== provider.key) {
    try {
      const { sports } = await api(`/api/integrations/${provider.key}/sports`);
      const groups = {};
      sports.forEach(sport => { (groups[sport.group || 'Otros'] ||= []).push(sport); });
      $('#sport-select').innerHTML = '<option value="">Elige un deporte…</option>' + Object.entries(groups).map(([group, items]) =>
        `<optgroup label="${esc(group)}">${items.map(item => `<option value="${esc(item.key)}">${esc(item.title)}</option>`).join('')}</optgroup>`).join('');
      sportsLoadedFor = provider.key;
    } catch (error) { $('#sport-select').innerHTML = `<option value="">${esc(error.message)}</option>`; }
  } else if (!provider.configured) {
    $('#sport-select').innerHTML = '<option value="">Configura la clave de API</option>';
  }
  $('#subscription-list').replaceChildren(...integrations.subscriptions.map(sub => {
    const item = document.createElement('div'); item.className = 'subscription-item';
    const regionText = sub.regions.map(r => provider.regions[r] || r).join(', ');
    item.innerHTML = `<div><strong>${esc(sub.sport_title)}</strong> · ${esc(regionText)}<small>Última sincronización: ${fmtDate(sub.last_sync_at)}</small>${sub.last_error ? `<small class="error">${esc(sub.last_error)}</small>` : ''}</div><button class="button button-quiet button-small button-danger" type="button">Dejar de seguir</button>`;
    $('button', item).addEventListener('click', async () => {
      if (!confirm(`¿Dejar de seguir ${sub.sport_title}? Los eventos ya importados se conservan.`)) return;
      try { await api(`/api/integrations/subscriptions/${sub.id}`, { method: 'DELETE' }); await loadIntegrations(); }
      catch (error) { toast(error.message); }
    });
    return item;
  }));
}
$('#subscription-form').addEventListener('submit', async event => {
  event.preventDefault();
  const select = $('#sport-select');
  const regions = [...document.querySelectorAll('#region-options input:checked')].map(el => el.value);
  $('#subscription-error').textContent = '';
  try {
    await api('/api/integrations/subscriptions', { method: 'POST', body: JSON.stringify({
      provider: integrations.providers[0].key, sport_key: select.value,
      sport_title: select.selectedOptions[0]?.textContent || select.value, regions,
    }) });
    toast('Deporte agregado. Usa "Sincronizar ahora" o espera la siguiente sincronización.');
    await loadIntegrations();
  } catch (error) { $('#subscription-error').textContent = error.message; }
});
$('#sync-now').addEventListener('click', async () => {
  const button = $('#sync-now'); button.disabled = true; button.textContent = 'Sincronizando…';
  try {
    const result = await api('/api/integrations/sync', { method: 'POST', body: '{}' });
    toast(result.errors?.length ? `Con errores: ${result.errors[0]}` : `${result.events} evento(s) y ${result.quotes} cuota(s) actualizadas · ${result.alerts} alerta(s)`);
    await loadEvents();
  } catch (error) { toast(error.message); }
  finally { button.textContent = 'Sincronizar ahora'; await loadIntegrations(); }
});
