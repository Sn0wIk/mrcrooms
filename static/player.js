let sdkPromise;
function loadSDK() {
  if (window.VK?.VideoPlayer) return Promise.resolve();
  if (!sdkPromise) {
    sdkPromise = new Promise((resolve, reject) => {
      const script = document.createElement('script');
      script.src = 'https://vk.ru/js/api/videoplayer.js';
      const timer = setTimeout(() => reject(new Error('VK не отвечает. Чат остаётся доступен.')), 12000);
      script.onload = () => {
        clearTimeout(timer);
        if (window.VK?.VideoPlayer) resolve();
        else reject(new Error('API плеера VK недоступно.'));
      };
      script.onerror = () => { clearTimeout(timer); reject(new Error('Не удалось загрузить плеер VK. Проверь подключение.')); };
      document.head.append(script);
    }).catch(error => { sdkPromise = null; throw error; });
  }
  return sdkPromise;
}

export class SyncedPlayer {
  constructor() {
    this.player = null;
    this.media = null;
    this.latest = null;
    this.revision = -1;
    this.applying = false;
    this.generation = 0;
    this.lastCheck = 0;
    this.ready = false;
    this.readyTimer = null;
  }

  status(text) { document.querySelector('#sync-status').textContent = text; }

  async mount(media) {
    const generation = ++this.generation;
    clearTimeout(this.readyTimer);
    this.player?.destroy?.();
    this.player = null;
    this.ready = false;
    this.media = media.embed;
    this.revision = -1;
    const area = document.querySelector('#player-area');
    area.replaceChildren();
    const frame = document.createElement('iframe');
    frame.title = 'VK Видео — совместный просмотр';
    frame.allow = 'autoplay; encrypted-media; fullscreen; picture-in-picture';
    frame.allowFullscreen = true;
    frame.src = media.embed;
    frame.id = 'vk-player';
    area.append(frame);
    document.querySelector('#video-label').textContent = media.label;
    const source = document.querySelector('#video-source');
    source.href = media.url;
    source.hidden = false;
    this.status('Подключаем плеер VK…');
    try {
      // The iframe may emit "inited" before its load event (VK also loads ads).
      // Attach the API immediately so we do not miss that handshake.
      await loadSDK();
      if (generation !== this.generation) return;
      this.player = window.VK.VideoPlayer(frame);
      this.readyTimer = setTimeout(() => {
        if (!this.ready && generation === this.generation) {
          this.status('Нажмите Play на обложке видео, затем «Синхронизировать».');
        }
      }, 15000);
      const markReady = () => {
        if (generation !== this.generation || this.ready) return;
        const state = this.player.getState?.();
        if (state === 'uninited' || state === 'error') return;
        clearTimeout(this.readyTimer);
        this.ready = true;
        this.apply(true);
      };
      for (const event of ['inited', 'started', 'resumed', 'timeupdate']) {
        this.player.on(event, markReady);
      }
      markReady();
      this.player.on('error', () => {
        clearTimeout(this.readyTimer);
        this.ready = false;
        this.status('VK не может воспроизвести видео. Возможно, оно закрыто или встраивание запрещено.');
      });
      this.player.on('autoplaySoundProhibited', () => {
        this.status('Нажми Play внутри плеера, затем «Синхронизировать»: браузер блокирует автозапуск.');
      });
    } catch (error) {
      if (generation === this.generation) this.status(error.message);
    }
  }

  update(state) {
    this.latest = {...state, received: performance.now()};
    if (state.media && this.media !== state.media.embed) {
      this.mount(state.media);
      return;
    }
    if (this.ready) this.apply();
  }

  async position() {
    if (!this.player || !this.ready) throw new Error('Плеер ещё не готов. Дождись загрузки видео.');
    let timer;
    try {
      const value = await Promise.race([
        Promise.resolve(this.player.getCurrentTime()),
        new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('VK не отвечает. Нажми Play в самом плеере или открой видео в VK.')), 2500); }),
      ]);
      const position = Number(value);
      if (value == null || !Number.isFinite(position) || position < 0) throw new Error('Плеер пока не сообщил позицию видео.');
      return position;
    } finally { clearTimeout(timer); }
  }

  async apply(force = false) {
    if (this.applying || !this.player || !this.ready || !this.latest) return;
    if (!force && this.revision === this.latest.revision && performance.now() - this.lastCheck < 5000) return;
    const generation = this.generation;
    const player = this.player;
    const state = this.latest;
    this.applying = true;
    this.lastCheck = performance.now();
    try {
      const current = await this.position();
      if (generation !== this.generation) return;
      const target = state.position + (state.playing ? (performance.now() - state.received) / 1000 : 0);
      const changed = this.revision !== state.revision;
      const drift = Math.abs(current - target) > 2;
      if (force || changed || drift) {
        player.seek(target);
        if (state.playing) player.play(); else player.pause();
      }
      this.revision = state.revision;
      this.status(`${state.playing ? 'Смотрим вместе' : 'На паузе'} · ${Math.floor(target / 60)}:${String(Math.floor(target % 60)).padStart(2, '0')}`);
    } catch (error) {
      if (generation === this.generation) this.status(error.message);
    } finally { this.applying = false; }
  }

  synchronize() {
    if (!this.latest?.media) return;
    if (!this.player || !this.ready) this.mount(this.latest.media);
    else this.apply(true);
  }
}
