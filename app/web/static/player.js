/* The player of an owned album: a bar stuck to the bottom of the page with play/pause, previous,
   next, a seek bar, volume, shuffle and repeat (off, all, one). The tracks of the album are the
   queue; each row has a button that starts that track in the player.

   The data comes from <script type="application/json" id="player-data"> on the page:
   {album, artist, cover, queue: [{row, id, title, artist, url, seconds, number}]}. */
document.addEventListener('alpine:init', () => {
  Alpine.data('albumPlayer', () => ({
    data: { queue: [], album: '', artist: '', cover: '' },
    order: [],
    pos: -1,
    playing: false,
    shuffle: false,
    repeat: 'off',
    time: 0,
    duration: 0,
    volume: 1,
    failed: false,
    infoOpen: false,
    audio: null,

    init() {
      const el = document.getElementById('player-data');
      if (el) { this.data = JSON.parse(el.textContent); }
      this.order = this.data.queue.map((_, i) => i);
      const a = (this.audio = new Audio());
      a.preload = 'none';
      a.addEventListener('timeupdate', () => { this.time = a.currentTime; });
      a.addEventListener('durationchange', () => { this.duration = isFinite(a.duration) ? a.duration : 0; });
      a.addEventListener('play', () => { this.playing = true; this.failed = false; this.session(); });
      a.addEventListener('pause', () => { this.playing = false; });
      a.addEventListener('ended', () => this.advance(true));
      a.addEventListener('error', () => {
        this.failed = true;
        this.playing = false;
        // a file that is missing or broken: say so, then go on with the next one (if any)
        setTimeout(() => { if (this.failed && this.pos + 1 < this.order.length) { this.advance(false); } }, 1500);
      });
      this.$watch('volume', (v) => { a.volume = Number(v); });
      if ('mediaSession' in navigator) {
        const s = navigator.mediaSession;
        s.setActionHandler('play', () => this.toggle());
        s.setActionHandler('pause', () => this.toggle());
        s.setActionHandler('previoustrack', () => this.prev());
        s.setActionHandler('nexttrack', () => this.next());
      }
    },

    get current() { return this.pos >= 0 ? this.data.queue[this.order[this.pos]] : null; },
    isCurrent(row) { return !!this.current && this.current.row === row; },
    isPlaying(row) { return this.isCurrent(row) && this.playing; },
    playable(row) { return this.data.queue.some((t) => t.row === row); },

    /* The order tracks are played in: the album's own, or shuffled with `first` at the front. */
    arrange(first) {
      const all = this.data.queue.map((_, i) => i);
      if (!this.shuffle) { this.order = all; this.pos = first == null ? -1 : first; return; }
      const rest = all.filter((i) => i !== first);
      for (let i = rest.length - 1; i > 0; i--) {
        const j = Math.floor(Math.random() * (i + 1));
        [rest[i], rest[j]] = [rest[j], rest[i]];
      }
      this.order = first == null ? rest : [first, ...rest];
      this.pos = first == null ? -1 : 0;
    },

    /* Start the track of a row (or pause it again when it is the one playing). */
    play(row) {
      if (this.isCurrent(row)) { this.toggle(); return; }
      const index = this.data.queue.findIndex((t) => t.row === row);
      if (index < 0) { return; }
      this.arrange(index);
      this.load();
    },

    load() {
      const t = this.current;
      if (!t) { return; }
      this.failed = false;
      this.time = 0;
      this.duration = t.seconds || 0;
      this.audio.src = t.url;
      this.audio.volume = Number(this.volume);
      this.audio.play().catch(() => { this.failed = true; });
    },

    toggle() {
      if (!this.current) { if (this.data.queue.length) { this.arrange(0); this.load(); } return; }
      if (this.audio.paused) { this.audio.play().catch(() => { this.failed = true; }); } else { this.audio.pause(); }
    },

    /* After a track: `ended` is true when it finished by itself (repeat one then repeats it). */
    advance(ended) {
      if (ended && this.repeat === 'one') { this.audio.currentTime = 0; this.audio.play(); return; }
      if (this.pos + 1 < this.order.length) { this.pos += 1; this.load(); return; }
      if (this.repeat === 'all') { this.arrange(null); this.pos = 0; this.load(); return; }
      if (ended) { this.audio.pause(); this.audio.currentTime = 0; this.time = 0; this.playing = false; }
    },
    next() { if (this.current) { this.advance(false); } },
    prev() {
      if (!this.current) { return; }
      if (this.audio.currentTime > 3 || this.pos === 0) { this.audio.currentTime = 0; return; }
      this.pos -= 1;
      this.load();
    },

    seek(event) {
      const v = Number(event.target.value);
      if (this.duration) { this.audio.currentTime = (v / 1000) * this.duration; }
    },
    get progress() { return this.duration ? Math.round((this.time / this.duration) * 1000) : 0; },

    toggleShuffle() {
      this.shuffle = !this.shuffle;
      const first = this.current ? this.order[this.pos] : null;
      this.arrange(first);
    },
    cycleRepeat() { this.repeat = { off: 'all', all: 'one', one: 'off' }[this.repeat]; },

    fmt(s) {
      s = Math.max(0, Math.round(s || 0));
      return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    },

    session() {
      const t = this.current;
      if (!t || !('mediaSession' in navigator) || typeof MediaMetadata === 'undefined') { return; }
      navigator.mediaSession.metadata = new MediaMetadata({
        title: t.title, artist: t.artist || this.data.artist, album: this.data.album,
        artwork: this.data.cover ? [{ src: this.data.cover }] : [],
      });
    },

    openInfo() {
      this.infoOpen = true;
      const body = document.getElementById('track-info-body');
      if (body) { body.innerHTML = '<p class="text-sm text-slate-500">' + (body.dataset.loading || '…') + '</p>'; }
    },
  }));
});
