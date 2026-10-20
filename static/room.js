import {api, csrf, savedName, toast} from './common.js';
import {VoiceChat} from './voice.js?v=diagnostics-1';
import {SyncedPlayer} from './player.js';

const $ = selector => document.querySelector(selector);
const code = $('#room').dataset.code;
const base = `/api/rooms/${code}`;
const player = new SyncedPlayer();
let member = null;
let members = [];
let after = 0;
let signalAfter = 0;
let timer;
let polling = false;
let stopped = false;
let failures = 0;
let lastSuccess = 0;

function voiceError(message) {
  $('#voice-error').textContent = message;
  $('#voice-error').hidden = false;
}

const voice = new VoiceChat({
  servers: JSON.parse($('#ice-config').textContent),
  send: (to, payload) => api(`${base}/signal`, {member, to, payload}),
  status: (enabled, muted) => api(`${base}/voice`, {member, enabled, muted}),
  error: voiceError,
  diagnostic: event => api(`${base}/voice-diagnostics`, {member, ...event}),
  changed: renderVoice,
});

function renderVoice() {
  const connected = Boolean(voice.stream);
  $('#voice-join').hidden = connected;
  $('#voice-leave').hidden = !connected;
  $('#mute').hidden = !connected;
  $('#mute').textContent = voice.muted ? 'Включить микрофон' : 'Выключить микрофон';
  $('#mute').setAttribute('aria-pressed', String(voice.muted));
  const connections = [...voice.peers.values()].filter(peer => peer.pc.connectionState === 'connected').length;
  $('#voice-status').textContent = !connected ? 'Присоединяйся, чтобы слышать и говорить.'
    : voice.muted ? 'Микрофон выключен. Ты слышишь остальных.'
      : connections ? `Микрофон включён · на связи: ${connections + 1}`
        : 'Микрофон включён. Ждём голосового подключения друзей.';
  renderMembers();
}

function renderMembers() {
  $('#member-count').textContent = `${members.length} / 8`;
  const container = $('#members');
  container.replaceChildren();
  for (const item of members) {
    const isSelf = item.id === member;
    const enabled = isSelf ? Boolean(voice.stream) : Boolean(item.voice);
    const muted = isSelf ? voice.muted : item.muted;
    const el = document.createElement('div');
    el.className = `member${enabled ? ' voice' : ''}`;
    const avatar = document.createElement('span');
    avatar.className = 'avatar';
    avatar.textContent = [...item.name][0]?.toUpperCase() || '?';
    const details = document.createElement('div');
    const name = document.createElement('div');
    name.className = 'member-name';
    name.textContent = item.name + (isSelf ? ' · ты' : '');
    const note = document.createElement('div');
    note.className = 'member-note';
    const linked = voice.peers.get(item.id)?.pc.connectionState === 'connected';
    note.textContent = !enabled ? 'в комнате'
      : muted ? 'микрофон выкл.'
        : isSelf ? 'микрофон включён'
          : linked ? 'на связи' : 'подключается…';
    details.append(name, note);
    el.append(avatar, details);
    container.append(el);
  }
}

function appendMessages(messages) {
  const chat = $('#chat');
  const nearBottom = chat.scrollHeight - chat.scrollTop - chat.clientHeight < 80;
  for (const message of messages) {
    if (message.id <= after) continue;
    after = message.id;
    $('#chat-empty')?.remove();
    const el = document.createElement('div');
    el.className = `chat-message${message.member === member ? ' own' : ''}`;
    const meta = document.createElement('div');
    meta.className = 'message-meta';
    const name = document.createElement('strong');
    name.textContent = message.name;
    const time = document.createElement('time');
    const date = new Date(message.created * 1000);
    time.dateTime = date.toISOString();
    time.textContent = date.toLocaleTimeString('ru', {hour: '2-digit', minute: '2-digit'});
    const body = document.createElement('div');
    body.className = 'message-body';
    body.textContent = message.body;
    meta.append(name, time);
    el.append(meta, body);
    chat.append(el);
    while (chat.children.length > 300) chat.firstChild.remove();
  }
  if (nearBottom) chat.scrollTop = chat.scrollHeight;
}

async function poll() {
  if (!member || polling || stopped) return;
  clearTimeout(timer);
  polling = true;
  try {
    const result = await api(`${base}/poll?member=${encodeURIComponent(member)}&after=${after}&signal_after=${signalAfter}`);
    if (stopped) return;
    failures = 0;
    lastSuccess = Date.now();
    $('#connection').textContent = 'В комнате';
    members = result.members;
    renderMembers();
    appendMessages(result.messages);
    player.update(result.state);
    const active = new Set(members.filter(item => item.voice).map(item => item.id));
    for (const signal of result.signals) {
      if (active.has(signal.sender)) await voice.receive(signal.sender, signal.payload);
      signalAfter = signal.id;
    }
    voice.reconcile(members);
  } catch (error) {
    if (stopped) return;
    failures++;
    $('#connection').textContent = 'Переподключение…';
    if (error.status === 403 || error.status === 404) {
      voice.stop();
      member = null;
      members = [];
      renderMembers();
      showJoin(error.message);
    } else if (lastSuccess && Date.now() - lastSuccess > 20000 && voice.stream) {
      voice.stop();
      voiceError('Связь с комнатой потеряна. Микрофон отключён; подключи голос после восстановления связи.');
    }
  } finally {
    polling = false;
    if (member && !stopped) timer = setTimeout(poll, Math.min(1000 * 2 ** Math.min(failures, 3), 8000));
  }
}

function showJoin(message = '') {
  $('#join-name').value = savedName();
  $('#join-error').textContent = message;
  $('#join-error').hidden = !message;
  if (!$('#join-dialog').open) $('#join-dialog').showModal();
}
$('#join-dialog').addEventListener('cancel', event => event.preventDefault());
$('#join-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    const name = $('#join-name').value.trim();
    const result = await api(`${base}/join`, {name});
    member = result.id;
    signalAfter = 0;
    savedName(name);
    $('#join-dialog').close();
    $('#voice-error').hidden = true;
    stopped = false;
    poll();
  } catch (error) {
    $('#join-error').textContent = error.message;
    $('#join-error').hidden = false;
  } finally { button.disabled = false; }
});

$('#invite').addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(location.href);
    toast('Ссылка скопирована. Отправь её друзьям.');
  } catch { window.prompt('Скопируй ссылку приглашения:', location.href); }
});

$('#media-form')?.addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector('button');
  button.disabled = true;
  try {
    const parsed = await api('/api/parse', {url: $('#media-url').value.trim()});
    await api(`${base}/media`, {url: $('#media-url').value.trim()});
    $('#media-hint').textContent = `${parsed.label} · ссылка добавлена.`;
    $('#media-url').value = '';
    poll();
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});

async function control(action, button) {
  if (button.disabled) return;
  button.disabled = true;
  try {
    const raw = $('#seek')?.value;
    if (action === 'seek' && !raw?.trim()) throw new Error('Укажи позицию в секундах.');
    const position = action === 'seek' ? Number(raw) : await player.position();
    await api(`${base}/control`, {action, position});
    poll();
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}
document.querySelectorAll('[data-action]').forEach(button => {
  button.addEventListener('click', () => control(button.dataset.action, button));
});
$('#seek-button')?.addEventListener('click', event => control('seek', event.currentTarget));
$('#sync').addEventListener('click', () => player.synchronize());

$('#chat-form').addEventListener('submit', async event => {
  event.preventDefault();
  const input = $('#chat-input');
  const body = input.value.trim();
  const button = event.currentTarget.querySelector('button');
  if (!body || button.disabled) return;
  button.disabled = true;
  try {
    await api(`${base}/chat`, {member, body});
    if (input.value.trim() === body) input.value = '';
    poll();
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
});
$('#chat-input').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    $('#chat-form').requestSubmit();
  }
});

$('#voice-join').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  $('#voice-error').hidden = true;
  try {
    if (!member) throw new Error('Сначала войди в комнату.');
    await voice.join(member);
    poll();
  } catch (error) { voiceError(error.message); }
  finally { button.disabled = false; }
});
$('#mute').addEventListener('click', async event => {
  const button = event.currentTarget;
  button.disabled = true;
  try { await voice.toggleMute(); }
  catch (error) { voiceError(error.message); }
  finally { button.disabled = false; }
});
$('#voice-leave').addEventListener('click', () => voice.leave());
$('#audio-unlock').addEventListener('click', () => voice.unlockAudio());
$('#voice-diagnostics').addEventListener('click', () => {
  $('#voice-report').textContent = JSON.stringify(voice.diagnostics(), null, 2);
  $('#voice-report').hidden = false;
});

function leavePage() {
  stopped = true;
  clearTimeout(timer);
  voice.stop();
  if (member) {
    fetch(`${base}/leave`, {
      method: 'POST', keepalive: true,
      headers: {'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
      body: JSON.stringify({member}),
    }).catch(() => {});
  }
}
window.addEventListener('pagehide', leavePage);
window.addEventListener('pageshow', event => {
  if (event.persisted) { member = null; stopped = false; showJoin(); }
});
document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });
showJoin();
