// Audio is sent directly between participants; HTTP carries SDP and ICE only.
export class VoiceChat {
  constructor({send, servers, status, changed, error, diagnostic = () => {}}) {
    this.send = send;
    this.servers = servers;
    this.status = status;
    this.changed = changed;
    this.error = error;
    this.diagnostic = diagnostic;
    this.events = [];
    this.hasTurn = servers.some(server => [server.urls].flat().some(url => /^turns?:/.test(url)));
    this.peers = new Map();
    this.pending = new Map();
    this.retryAt = new Map();
    this.stream = null;
    this.self = null;
    this.muted = false;
    this.generation = 0;
  }

  record(event, peer, extra = {}) {
    const entry = {
      event,
      connection: peer.pc.connectionState,
      ice: peer.pc.iceConnectionState,
      gathering: peer.pc.iceGatheringState,
      signaling: peer.pc.signalingState,
      local_types: [...peer.localTypes],
      remote_types: [...peer.remoteTypes],
      ...extra,
    };
    this.events.push({time: new Date().toISOString(), ...entry});
    if (this.events.length > 100) this.events.shift();
    console.info('[MRC voice]', entry);
    if (['failed', 'ice-error', 'signal-error', 'manual'].includes(event)) {
      Promise.resolve(this.diagnostic(entry)).catch(() => {});
    }
  }

  diagnostics() {
    for (const peer of this.peers.values()) this.record('manual', peer);
    return {
      secure_context: window.isSecureContext,
      turn_configured: this.hasTurn,
      microphone_active: Boolean(this.stream),
      muted: this.muted,
      connected: [...this.peers.values()].filter(peer => peer.pc.connectionState === 'connected').length,
      events: this.events,
    };
  }

  async join(self) {
    if (!window.isSecureContext || !navigator.mediaDevices?.getUserMedia) {
      throw new Error('Для микрофона открой сайт по HTTPS или через localhost.');
    }
    const generation = ++this.generation;
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: false,
        audio: {echoCancellation: true, noiseSuppression: true, autoGainControl: true},
      });
    } catch (error) {
      const messages = {
        NotAllowedError: 'Разреши доступ к микрофону в настройках браузера и попробуй снова.',
        NotFoundError: 'Микрофон не найден. Подключи его и попробуй снова.',
        NotReadableError: 'Микрофон занят другим приложением или недоступен.',
      };
      throw new Error(messages[error.name] || 'Не удалось включить микрофон.');
    }
    if (generation !== this.generation) {
      stream.getTracks().forEach(track => track.stop());
      return;
    }
    this.stream = stream;
    this.self = self;
    this.muted = false;
    try {
      await this.status(true, false);
      if (generation !== this.generation) return;
      stream.getAudioTracks()[0].onended = () => {
        this.leave();
        this.error('Микрофон отключён. Подключи его и войди в голосовой чат снова.');
      };
      this.changed();
    } catch (error) {
      this.stop();
      throw error;
    }
  }

  async toggleMute() {
    if (!this.stream) return;
    const muted = !this.muted;
    // Stop transmission immediately, even when the server cannot be reached.
    this.stream.getAudioTracks().forEach(track => { track.enabled = !muted; });
    this.muted = muted;
    this.changed();
    await this.status(true, muted);
  }

  stop() {
    this.generation++;
    this.stream?.getTracks().forEach(track => {
      track.onended = null;
      track.stop();
    });
    this.stream = null;
    this.muted = false;
    for (const id of [...this.peers.keys()]) this.remove(id);
    this.pending.clear();
    this.retryAt.clear();
    this.changed();
  }

  async leave() {
    this.stop();
    try { await this.status(false, false); }
    catch (error) { this.error(error.message); }
  }

  remove(id) {
    const peer = this.peers.get(id);
    if (!peer) return;
    this.peers.delete(id);
    peer.pc.onicecandidate = null;
    peer.pc.onicecandidateerror = null;
    peer.pc.onconnectionstatechange = null;
    peer.pc.close();
    peer.audio.srcObject = null;
    peer.audio.remove();
    clearTimeout(peer.disconnectTimer);
    this.changed();
  }

  create(id, call) {
    this.remove(id);
    const pc = new RTCPeerConnection({iceServers: this.servers});
    const audio = document.createElement('audio');
    audio.autoplay = true;
    document.querySelector('#remote-audio').append(audio);
    const peer = {pc, audio, call, sendQueue: Promise.resolve(), disconnectTimer: null, localTypes: new Set(), remoteTypes: new Set()};
    this.peers.set(id, peer);
    this.stream.getTracks().forEach(track => pc.addTrack(track, this.stream));
    pc.onicecandidate = ({candidate}) => {
      if (candidate) {
        if (candidate.type) peer.localTypes.add(candidate.type);
        this.transmit(id, peer, {type: 'candidate', candidate: candidate.toJSON()})
          .catch(error => {
            this.record('signal-error', peer, {phase: 'candidate', error_code: error.status || 0});
            this.error(error.message);
          });
      }
    };
    pc.onicecandidateerror = event => this.record('ice-error', peer, {error_code: event.errorCode});
    pc.ontrack = ({streams}) => {
      audio.srcObject = streams[0];
      audio.play().catch(() => { document.querySelector('#audio-unlock').hidden = false; });
    };
    pc.onconnectionstatechange = () => {
      this.record(pc.connectionState === 'failed' ? 'failed' : 'state', peer);
      this.changed();
      if (pc.connectionState === 'failed') {
        this.retryAt.set(id, Date.now() + 5000);
        this.remove(id);
        this.error(this.hasTurn
          ? 'Голосовая связь не установлена. Проверьте диагностику: возможна недоступность STUN/TURN или блокировка сети.'
          : 'Прямое голосовое соединение не установлено. TURN-сервер не настроен; он нужен, если ваши сети блокируют связь между браузерами.');
      } else if (pc.connectionState === 'disconnected') {
        clearTimeout(peer.disconnectTimer);
        peer.disconnectTimer = setTimeout(() => {
          if (pc.connectionState === 'disconnected') this.remove(id);
        }, 10000);
      } else if (pc.connectionState === 'connected') {
        clearTimeout(peer.disconnectTimer);
      }
    };
    return peer;
  }

  transmit(id, peer, payload) {
    // Preserve SDP / candidate ordering despite independent HTTP requests.
    peer.sendQueue = peer.sendQueue.catch(() => {}).then(() => {
      if (this.peers.get(id) !== peer || !this.stream) return;
      return this.send(id, {...payload, call: peer.call});
    });
    return peer.sendQueue;
  }

  async offer(id) {
    if (!this.stream || this.peers.has(id)) return;
    const peer = this.create(id, crypto.randomUUID());
    try {
      const offer = await peer.pc.createOffer();
      await peer.pc.setLocalDescription(offer);
      await this.transmit(id, peer, {type: 'offer', sdp: offer.sdp});
    } catch (error) {
      if (this.peers.get(id) === peer) {
        this.record('signal-error', peer, {phase: 'offer', error_code: error.status || 0});
        this.retryAt.set(id, Date.now() + 5000);
        this.remove(id);
        this.error('Голосовое подключение прервалось. Повторяем попытку…');
      }
    }
  }

  async receive(id, payload) {
    if (!this.stream || id === this.self || typeof payload.call !== 'string') return;
    let peer = this.peers.get(id);
    try {
      if (payload.type === 'offer') {
        // Only the lexicographically smaller member makes offers: no offer glare.
        if (id >= this.self || typeof payload.sdp !== 'string') return;
        peer = this.create(id, payload.call);
        await peer.pc.setRemoteDescription({type: 'offer', sdp: payload.sdp});
        await this.drain(id, peer);
        const answer = await peer.pc.createAnswer();
        await peer.pc.setLocalDescription(answer);
        await this.transmit(id, peer, {type: 'answer', sdp: answer.sdp});
      } else if (payload.type === 'answer') {
        if (!peer || peer.call !== payload.call || peer.pc.signalingState !== 'have-local-offer') return;
        await peer.pc.setRemoteDescription({type: 'answer', sdp: payload.sdp});
        await this.drain(id, peer);
      } else if (payload.type === 'candidate') {
        const type = payload.candidate?.candidate?.match(/\btyp (host|srflx|prflx|relay)\b/)?.[1];
        if (type && peer) peer.remoteTypes.add(type);
        if (peer?.call === payload.call && peer.pc.remoteDescription) {
          await peer.pc.addIceCandidate(payload.candidate);
        } else {
          const key = `${id}:${payload.call}`;
          const queue = this.pending.get(key) || [];
          if (queue.length < 100) queue.push(payload.candidate);
          // Bound memory for obsolete calls from a participant.
          if (this.pending.size > 40) this.pending.delete(this.pending.keys().next().value);
          this.pending.set(key, queue);
        }
      }
    } catch (error) {
      if (peer && this.peers.get(id) === peer) {
        this.record('signal-error', peer, {phase: 'receive', error_code: error.status || 0});
        this.retryAt.set(id, Date.now() + 5000);
        this.remove(id);
      }
      this.error('Не удалось согласовать голосовое соединение. Повторяем попытку…');
    }
  }

  async drain(id, peer) {
    const key = `${id}:${peer.call}`;
    const candidates = this.pending.get(key) || [];
    this.pending.delete(key);
    for (const candidate of candidates) {
      const type = candidate?.candidate?.match(/\btyp (host|srflx|prflx|relay)\b/)?.[1];
      if (type) peer.remoteTypes.add(type);
      await peer.pc.addIceCandidate(candidate);
    }
  }

  reconcile(members) {
    const active = new Set(members.filter(member => member.voice && member.id !== this.self).map(member => member.id));
    for (const id of this.peers.keys()) {
      if (!active.has(id) || !this.stream) this.remove(id);
    }
    if (!this.stream) return;
    for (const id of active) {
      if (this.self < id && !this.peers.has(id) && Date.now() >= (this.retryAt.get(id) || 0)) {
        this.offer(id);
      }
    }
  }

  async unlockAudio() {
    let success = true;
    for (const {audio} of this.peers.values()) {
      if (audio.srcObject) {
        try { await audio.play(); } catch { success = false; }
      }
    }
    document.querySelector('#audio-unlock').hidden = success;
  }
}
