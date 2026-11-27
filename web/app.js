const $ = (selector, root = document) => root.querySelector(selector);
const authView = $('#auth-view');
const dashboard = $('#dashboard');
const authForm = $('#auth-form');
let authMode = 'login';
let eventsCache = [];

async function api(url, options = {}) {
  const response = await fetch(url, { ...options, headers: { 'Content-Type': 'application/json', ...(options.headers || {}) } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || 'Ocurrió un error.');
  return data;
}
function toast(message) {
  const el = $('#toast'); el.textContent = message; el.classList.add('show');
  clearTimeout(toast.timer); toast.timer = setTimeout(() => el.classList.remove('show'), 3400);
}
function esc(value) {
  return String(value).replace(/[&<>"']/g, ch => ({ '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;' }[ch]));
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
$('#new-event-button').addEventListener('click', () => $('#event-dialog').showModal());
$('#empty-new-event').addEventListener('click', () => $('#event-dialog').showModal());
$('#event-form').addEventListener('submit', async event => {
  if (event.submitter?.value === 'cancel') return;
  event.preventDefault();
  const form = event.currentTarget;
  const values = Object.fromEntries(new FormData(form));
  values.outcomes = values.outcomes.split(',').map(value => value.trim()).filter(Boolean);
  $('#event-error').textContent = '';
  try {
    await api('/api/events', { method: 'POST', body: JSON.stringify(values) });
    form.reset(); $('#event-dialog').close(); await loadEvents(); toast('Evento agregado. Ahora captura sus cuotas.');
  } catch (error) { $('#event-error').textContent = error.message; }
});
$('#email-form').addEventListener('submit', async event => {
  event.preventDefault();
  try {
    await api('/api/profile', { method: 'POST', body: JSON.stringify({ alert_email: $('#alert-email').value }) });
    $('#email-status').textContent = 'Correo de alertas guardado.'; $('#email-status').style.color = '#187c4b';
  } catch (error) { $('#email-status').textContent = error.message; $('#email-status').style.color = '#ba4b43'; }
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
function renderEvent(event) {
  const calc = event.calculation;
  const card = document.createElement('article'); card.className = 'event-card';
  const startText = event.starts_at ? ` · ${esc(event.starts_at.replace('T', ' '))}` : '';
  let tableRows = event.outcomes.map(outcome => {
    const quote = calc.best_odds[outcome];
    return `<tr><td>${esc(outcome)}</td><td class="${quote ? 'odds-best' : ''}">${quote ? esc(quote.decimal_odds) : '—'}</td><td>${quote ? esc(quote.bookmaker) : 'Sin cuota'}</td><td>${quote ? new Date(quote.observed_at).toLocaleString('es-MX') : '—'}</td></tr>`;
  }).join('');
  const alert = event.alerts[0];
  const alertNote = alert ? `<div class="alert-note ${alert.status !== 'sent' ? 'pending' : ''}">${alert.status === 'sent' ? '✓ Correo enviado' : '◷ Correo pendiente'} · ${esc(alert.message)} (${(alert.probability_sum * 100).toFixed(2)}%)</div>` : '';
  card.innerHTML = `<div class="event-header"><div><h3 class="event-title">${esc(event.name)}</h3><div class="event-meta">${esc(event.sport || 'Deporte sin especificar')}${startText}</div></div><span class="market-badge">${esc(event.market)}</span></div>
    <div class="calc-strip"><div><div class="calc-label">Suma de probabilidades implícitas</div><div class="calc-value ${calc.qualifies ? 'good' : calc.complete ? '' : 'wait'}">${calc.probability_percent === null ? '—' : `${calc.probability_percent.toFixed(2)}%`}</div><div class="calc-sub">${calc.complete ? `Underround teórico: ${calc.underround_percent.toFixed(2)}%` : 'Faltan resultados para completar el mercado'}</div></div><span class="status-pill ${calc.qualifies ? 'good' : calc.complete ? '' : 'incomplete'}">${calc.qualifies ? 'Umbral alcanzado' : calc.complete ? 'En seguimiento' : 'Incompleto'}</span></div>
    <div class="odds-table-wrap"><table class="odds-table"><thead><tr><th>RESULTADO</th><th>MEJOR CUOTA</th><th>CASA</th><th>CAPTURADA</th></tr></thead><tbody>${tableRows}</tbody></table></div>
    ${alertNote}<form class="capture-row"><label>Resultado<select name="outcome" required>${event.outcomes.map(outcome => `<option value="${esc(outcome)}">${esc(outcome)}</option>`).join('')}</select></label><label>Casa<input name="bookmaker" required placeholder="Casa de juego"></label><label>Cuota decimal<input name="decimal_odds" type="number" min="1.01" max="100000" step="0.01" required placeholder="2.10"></label><button class="button button-secondary" type="submit">Guardar cuota</button></form>`;
  $('.capture-row', card).addEventListener('submit', eventSubmit => { eventSubmit.preventDefault(); addOdds(event.id, card); });
  return card;
}
async function loadEvents() {
  const data = await api('/api/events'); eventsCache = data.events;
  const list = $('#events-list'); list.replaceChildren(...eventsCache.map(renderEvent));
  $('#empty-state').classList.toggle('hidden', eventsCache.length > 0);
  $('#stat-events').textContent = eventsCache.length;
  $('#stat-complete').textContent = eventsCache.filter(event => event.calculation.complete).length;
  $('#stat-alerts').textContent = eventsCache.filter(event => event.calculation.qualifies).length;
}
async function showDashboard() {
  try {
    const me = await api('/api/me');
    $('#alert-email').value = me.alert_email;
    authView.classList.add('hidden'); dashboard.classList.remove('hidden'); $('#logout').classList.remove('hidden');
    await loadEvents();
  } catch { dashboard.classList.add('hidden'); authView.classList.remove('hidden'); $('#logout').classList.add('hidden'); }
}
showDashboard();
