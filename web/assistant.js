// Asistente de Cuotas Claro: chat flotante con respuestas en streaming y botones de acción.
(() => {
  const STORAGE_KEY = 'cc-assistant-history';
  const MAX_HISTORY = 6;
  let history = [];
  let busy = false;
  let greeted = false;
  let opened = false;

  const icon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z"/></svg>';
  const sendIcon = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';

  const launcher = document.createElement('button');
  launcher.className = 'cc-chat-launcher';
  launcher.type = 'button';
  launcher.setAttribute('aria-label', 'Abrir asistente de Cuotas Claro');
  launcher.innerHTML = `${icon}<span>¿Te ayudo?</span><span class="cc-dot"></span>`;

  const panel = document.createElement('section');
  panel.className = 'cc-chat';
  panel.setAttribute('role', 'dialog');
  panel.setAttribute('aria-label', 'Asistente de Cuotas Claro');
  panel.innerHTML = `
    <header class="cc-chat-header">
      <div class="cc-avatar">cc</div>
      <div><h2>Asistente Cuotas Claro</h2><p id="cc-status">Te guío paso a paso</p></div>
      <div class="cc-header-actions">
        <button class="cc-icon-button" type="button" data-cc="reset" title="Nueva conversación" aria-label="Nueva conversación">↺</button>
        <button class="cc-icon-button" type="button" data-cc="close" title="Cerrar" aria-label="Cerrar asistente">×</button>
      </div>
    </header>
    <div class="cc-messages" aria-live="polite"></div>
    <form class="cc-form">
      <textarea rows="1" maxlength="800" placeholder="Escribe tu pregunta…" aria-label="Tu pregunta"></textarea>
      <button class="cc-send" type="submit" aria-label="Enviar">${sendIcon}</button>
    </form>
    <div class="cc-footer">Respuestas generadas con IA a partir de la información de Cuotas Claro. Verifica siempre las cuotas.</div>`;

  document.body.append(launcher, panel);
  const messages = panel.querySelector('.cc-messages');
  const form = panel.querySelector('.cc-form');
  const input = form.querySelector('textarea');
  const sendButton = form.querySelector('.cc-send');

  const isLoggedIn = () => {
    const dash = document.getElementById('dashboard');
    return Boolean(dash && !dash.classList.contains('hidden'));
  };
  const escapeHtml = value => String(value).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));

  // Markdown mínimo y seguro: se escapa todo y luego se aplican negritas, cursivas, código y listas.
  function renderMarkdown(text) {
    const inline = line => escapeHtml(line)
      .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
      .replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, '$1<em>$2</em>')
      .replace(/(^|\s)_(.+?)_(?=\s|$|[.,;:])/g, '$1<em>$2</em>')
      .replace(/`([^`]+)`/g, '<code>$1</code>');
    const html = [];
    let list = null;
    for (const raw of String(text).split('\n')) {
      const line = raw.trimEnd();
      const bullet = line.match(/^\s*[-•*]\s+(.*)/);
      const numbered = line.match(/^\s*\d+[.)]\s+(.*)/);
      if (bullet || numbered) {
        const type = bullet ? 'ul' : 'ol';
        if (list !== type) { if (list) html.push(`</${list}>`); html.push(`<${type}>`); list = type; }
        html.push(`<li>${inline((bullet || numbered)[1])}</li>`);
        continue;
      }
      if (list) { html.push(`</${list}>`); list = null; }
      if (line.trim()) html.push(`<p>${inline(line)}</p>`);
    }
    if (list) html.push(`</${list}>`);
    return html.join('');
  }

  function scrollDown() { messages.scrollTop = messages.scrollHeight; }

  function addMessage(role, text, note) {
    const bubble = document.createElement('div');
    bubble.className = `cc-msg ${role === 'user' ? 'cc-msg-user' : 'cc-msg-bot'}`;
    if (role === 'user') bubble.textContent = text;
    else bubble.innerHTML = renderMarkdown(text);
    if (note) {
      const small = document.createElement('div');
      small.className = 'cc-msg-note';
      small.textContent = note;
      bubble.append(small);
    }
    messages.append(bubble);
    scrollDown();
    return bubble;
  }

  function addButtons(buttons) {
    if (!buttons || !buttons.length) return;
    const row = document.createElement('div');
    row.className = 'cc-buttons';
    for (const button of buttons) {
      const chip = document.createElement(button.href ? 'a' : 'button');
      chip.className = 'cc-chip';
      chip.textContent = button.label;
      if (button.href) {
        chip.href = button.href; chip.target = '_blank'; chip.rel = 'noopener noreferrer'; chip.dataset.kind = 'href';
      } else {
        chip.type = 'button';
        chip.addEventListener('click', () => runButton(button));
      }
      row.append(chip);
    }
    messages.append(row);
    scrollDown();
  }

  function highlight(element) {
    if (!element) return;
    element.scrollIntoView({ behavior: 'smooth', block: 'center' });
    element.classList.remove('cc-highlight');
    void element.offsetWidth;
    element.classList.add('cc-highlight');
    closeChat(); // el chat se minimiza para no tapar la sección; la conversación se conserva
  }

  function needLogin() {
    if (typeof setMode === 'function') setMode('login');
    addMessage('bot', 'Para eso necesitas iniciar sesión. Si aún no tienes cuenta, créala en un minuto: solo pide correo y contraseña.');
    addButtons([{ label: 'Iniciar sesión', action: 'open_login' }, { label: 'Crear cuenta', action: 'open_register' }]);
  }

  function runButton(button) {
    if (button.prompt) { send(button.prompt); return; }
    const action = button.action || '';
    if (action === 'open_register' || action === 'open_login') {
      if (isLoggedIn()) { addMessage('bot', 'Ya iniciaste sesión. ¿Quieres crear un evento?'); addButtons([{ label: '＋ Nuevo evento', action: 'new_event' }]); return; }
      if (typeof setMode === 'function') setMode(action === 'open_register' ? 'register' : 'login');
      const form = document.getElementById('auth-form');
      highlight(form);
      form?.querySelector('input[name="email"]')?.focus();
      return;
    }
    if (!isLoggedIn()) { needLogin(); return; }
    if (action === 'new_event') {
      if (typeof openEventDialog === 'function') openEventDialog();
      else document.getElementById('new-event-button')?.click();
      closeChat();
      return;
    }
    if (action === 'sync_now') {
      const syncButton = document.getElementById('sync-now');
      if (syncButton && !syncButton.disabled) { syncButton.click(); highlight(document.getElementById('integrations-panel')); }
      else { addMessage('bot', 'La sincronización no está disponible: falta la clave de API en el servidor o no sigues ningún deporte.'); highlight(document.getElementById('integrations-panel')); }
      return;
    }
    if (action.startsWith('goto:')) {
      const target = document.getElementById(action.slice(5));
      highlight(target?.closest('section') || target);
      target?.querySelector?.('input, select')?.focus({ preventScroll: true });
    }
  }

  function save() {
    try { sessionStorage.setItem(STORAGE_KEY, JSON.stringify(history.slice(-20))); } catch { /* almacenamiento no disponible */ }
  }

  function restore() {
    try {
      const saved = JSON.parse(sessionStorage.getItem(STORAGE_KEY) || '[]');
      if (Array.isArray(saved) && saved.length) {
        history = saved;
        for (const item of history) addMessage(item.role, item.content);
        greeted = true;
        return true;
      }
    } catch { /* ignorar */ }
    return false;
  }

  async function greet() {
    if (greeted) return;
    greeted = true;
    try {
      const response = await fetch('/api/assistant', { headers: { Accept: 'application/json' } });
      const data = await response.json();
      addMessage('bot', data.text);
      addButtons(data.buttons);
      if (!data.ai) document.getElementById('cc-status').textContent = 'Guía rápida';
    } catch {
      addMessage('bot', '¡Hola! Soy el asistente de Cuotas Claro. Pregúntame cómo empezar, cómo se calcula el underround o cómo configurar tus alertas.');
    }
  }

  async function send(text) {
    const question = text.trim();
    if (!question || busy) return;
    busy = true; sendButton.disabled = true;
    addMessage('user', question);
    const bubble = document.createElement('div');
    bubble.className = 'cc-msg cc-msg-bot';
    bubble.innerHTML = '<span class="cc-typing" aria-label="Escribiendo"><span></span><span></span><span></span></span>';
    messages.append(bubble); scrollDown();
    let answer = '';
    let final = null;
    try {
      const response = await fetch('/api/assistant', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ message: question, history: history.slice(-MAX_HISTORY) }),
      });
      if (!response.ok) {
        let message = 'No pude responder en este momento.';
        try { message = (await response.json()).error || message; } catch { /* sin JSON */ }
        throw new Error(message);
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let index;
        while ((index = buffer.indexOf('\n')) >= 0) {
          const line = buffer.slice(0, index).trim();
          buffer = buffer.slice(index + 1);
          if (!line) continue;
          const event = JSON.parse(line);
          if (event.type === 'delta') {
            answer += event.text;
            bubble.innerHTML = renderMarkdown(answer);
            scrollDown();
          } else if (event.type === 'done') {
            final = event;
          }
        }
      }
      if (!final) throw new Error('La respuesta se interrumpió.');
      answer = final.text || answer;
      bubble.innerHTML = renderMarkdown(answer);
      addButtons(final.buttons);
      history.push({ role: 'user', content: question }, { role: 'assistant', content: answer });
      save();
    } catch (error) {
      bubble.innerHTML = renderMarkdown(answer || error.message || 'No pude responder en este momento.');
      if (!answer) addButtons([{ label: '¿Cómo empiezo?', prompt: '¿Cómo empiezo a usar Cuotas Claro?' }]);
    } finally {
      busy = false; sendButton.disabled = false;
      scrollDown();
    }
  }

  function openChat() {
    panel.classList.add('is-open');
    launcher.classList.add('is-open');
    if (!opened) { opened = true; if (!restore()) greet(); }
    setTimeout(() => input.focus(), 150);
  }
  function closeChat() {
    panel.classList.remove('is-open');
    launcher.classList.remove('is-open');
    launcher.focus({ preventScroll: true });
  }

  launcher.addEventListener('click', () => { if (!panel.classList.contains('is-open')) openChat(); });
  panel.querySelector('[data-cc="close"]').addEventListener('click', closeChat);
  panel.querySelector('[data-cc="reset"]').addEventListener('click', () => {
    history = []; save(); messages.replaceChildren(); greeted = false; greet();
  });
  panel.addEventListener('keydown', event => { if (event.key === 'Escape') closeChat(); });
  form.addEventListener('submit', event => { event.preventDefault(); const text = input.value; input.value = ''; input.style.height = ''; send(text); });
  input.addEventListener('keydown', event => {
    if (event.key === 'Enter' && !event.shiftKey) { event.preventDefault(); form.requestSubmit(); }
  });
  input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = `${Math.min(input.scrollHeight, 110)}px`; });
})();
