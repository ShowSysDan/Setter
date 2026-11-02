/* Setter front-end. No framework, no build step. Every button on the dashboard
   sends a control string through /api/command, the same path the UDP listener
   uses, so the activity log doubles as a tutorial for the text protocol. */

const Setter = (() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  let toastTimer = null;

  function toast(text, isErr = false) {
    const el = $('#toast');
    if (!el) return;
    el.textContent = text;
    el.classList.toggle('err', isErr);
    el.classList.add('show');
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove('show'), isErr ? 5000 : 2200);
  }

  async function api(path, opts = {}) {
    const init = { method: opts.method || 'GET', headers: {} };
    if (opts.body !== undefined) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
    const res = await fetch(path, init);
    const data = await res.json().catch(() => ({}));
    if (!res.ok && !data.replies && !data.error) throw new Error(res.statusText || 'request failed');
    return data;
  }

  async function send(text) {
    try {
      const data = await api('/api/command', { method: 'POST', body: { text } });
      toast(data.replies.join('\n'), !data.ok);
      return data;
    } catch (err) {
      toast(`Could not reach Setter: ${err.message}`, true);
      return { ok: false, replies: [`ERR ${err.message}`] };
    }
  }

  function fmtTime(ts) {
    const d = new Date(ts * 1000);
    return d.toTimeString().slice(0, 8);
  }

  function escape(s) {
    return String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  /* ---------------------------------------------------------------- dashboard */
  function dashboard(initial) {
    let status = initial;
    let me = 0;
    let lastEventId = 0;
    let busSig = '';
    let outputsSig = '';
    const multiMe = () => status.settings.me_count > 1;
    const meSuffix = () => (multiMe() ? ` ME${me + 1}` : '');
    const sw = () => status.switcher || {};
    const fps = () => (sw().video_mode && sw().video_mode.fps) || 25;
    const allSources = () => [...status.inputs, ...status.internal_sources];

    /* clicks: anything with data-cmd sends that string (+ ME suffix for ME-scoped commands) */
    document.addEventListener('click', ev => {
      const b = ev.target.closest('[data-cmd]');
      if (b && !b.disabled) {
        const cmd = b.dataset.cmd;
        const meScoped = !/^(AUX|DSK|MACRO)\b/.test(cmd);
        send(meScoped ? cmd + meSuffix() : cmd).then(refresh);
        return;
      }
      const a = ev.target.closest('[data-action]');
      if (a) {
        api('/api/' + a.dataset.action, { method: 'POST' })
          .then(() => refresh())
          .catch(err => toast(err.message, true));
      }
    });

    /* transition controls */
    const styleSel = $('#style');
    const rateIn = $('#rate');
    const ftbRateIn = $('#ftb-rate');
    const fadeSrc = $('#fade-src');
    styleSel.addEventListener('change', () => send(`STYLE ${styleSel.value}${meSuffix()}`).then(refresh));
    rateIn.addEventListener('change', () => {
      send(`RATE ${rateIn.value}${meSuffix()}`).then(refresh);
      rateIn.blur();
    });
    ftbRateIn.addEventListener('change', () => {
      send(`FTBRATE ${ftbRateIn.value}${meSuffix()}`).then(refresh);
      ftbRateIn.blur();
    });
    rateIn.addEventListener('input', showRateSeconds);
    $('#btn-fade').addEventListener('click', () => {
      send(`FADE ${fadeSrc.value} ${rateIn.value}${meSuffix()}`).then(refresh);
    });

    /* control string + raw forms */
    $('#cmd-form').addEventListener('submit', async ev => {
      ev.preventDefault();
      const input = $('#cmd-text');
      const text = input.value.trim();
      if (!text) return;
      const data = await send(text);
      const out = $('#cmd-reply');
      out.hidden = false;
      out.textContent = data.replies.join('\n');
      out.classList.toggle('err', !data.ok);
      input.select();
      refresh();
    });
    $('#raw-form').addEventListener('submit', async ev => {
      ev.preventDefault();
      const out = $('#raw-reply');
      try {
        const data = await api('/api/raw', { method: 'POST', body: { name: $('#raw-name').value.trim(), hex: $('#raw-hex').value } });
        out.hidden = false;
        out.classList.toggle('err', !data.ok);
        out.textContent = data.ok ? 'Sent and acknowledged by the switcher' : (data.error || 'Sent, but the switcher did not acknowledge it');
      } catch (err) {
        out.hidden = false;
        out.classList.add('err');
        out.textContent = err.message;
      }
      refresh();
    });

    function showRateSeconds() {
      const f = parseInt(rateIn.value, 10);
      $('#rate-secs').textContent = f > 0 ? `${(f / fps()).toFixed(2)} s` : '';
    }

    function keyHtml(src, cmdPrefix) {
      const plain = src.label === String(src.id) && src.name === `Input ${src.id}`;
      const sub = plain || src.name === src.label ? '' : `<small title="${escape(src.name)}">${escape(src.name)}</small>`;
      return `<button class="key" type="button" data-id="${src.id}" data-cmd="${cmdPrefix} ${escape(src.token)}" title="${escape(src.name)}">${escape(src.label)}${sub}</button>`;
    }

    function renderBuses() {
      const sources = allSources();
      const sig = JSON.stringify([status.settings.me_count, sources.map(s => [s.id, s.label, s.name])]);
      if (sig !== busSig) {
        busSig = sig;
        const rows = { pgm: $('#pgm-keys'), pvw: $('#pvw-keys') };
        for (const [bus, row] of Object.entries(rows)) {
          const prefix = bus === 'pgm' ? 'PGM' : 'PVW';
          row.innerHTML = status.inputs.map(s => keyHtml(s, prefix)).join('')
            + '<div class="gap"></div>'
            + status.internal_sources.map(s => keyHtml(s, prefix)).join('');
        }
        fadeSrc.innerHTML = sources.map(s => `<option value="${escape(s.token)}" title="${escape(s.name)}">${escape(s.label)}</option>`).join('');
        const tabs = $('#me-tabs');
        tabs.hidden = !multiMe();
        tabs.innerHTML = Array.from({ length: status.settings.me_count }, (_, i) =>
          `<button class="btn small" type="button" data-me="${i}" aria-pressed="${i === me}">ME ${i + 1}</button>`).join('');
        tabs.querySelectorAll('[data-me]').forEach(b => b.addEventListener('click', () => { me = +b.dataset.me; render(); }));
      }
      const pgm = sw().program ? sw().program[me] : undefined;
      const pvw = sw().preview ? sw().preview[me] : undefined;
      const connected = status.connection.state === 'connected';
      document.querySelectorAll('#pgm-keys .key').forEach(k => k.classList.toggle('on-pgm', connected && +k.dataset.id === pgm));
      document.querySelectorAll('#pvw-keys .key').forEach(k => k.classList.toggle('on-pvw', connected && +k.dataset.id === pvw));
      const name = id => { const s = sources.find(x => x.id === id); return s ? s.label : (id === undefined ? '–' : '#' + id); };
      $('#pgm-current').textContent = connected ? name(pgm) : '–';
      $('#pvw-current').textContent = connected ? name(pvw) : '–';
      $('#me-tabs').querySelectorAll('[data-me]').forEach(b => b.setAttribute('aria-pressed', String(+b.dataset.me === me)));
    }

    function renderTransition() {
      const t = (sw().transition || {})[me] || {};
      const busy = !!t.in_transition;
      $('#btn-auto').classList.toggle('busy', busy);
      $('#trans-state').textContent = busy ? `in transition, ${t.frames_remaining ?? '?'} frames left` : '';
      const styleNames = ['MIX', 'DIP', 'WIPE', 'DVE', 'STING'];
      if (document.activeElement !== styleSel && t.style !== undefined) styleSel.value = styleNames[t.style] || 'MIX';
      const rate = (sw().mix_rate || {})[me];
      if (document.activeElement !== rateIn && rate !== undefined) rateIn.value = rate;
      showRateSeconds();
      const ftb = (sw().ftb || {})[me] || {};
      const ftbBtn = $('#btn-ftb');
      ftbBtn.classList.toggle('black', !!ftb.full_black);
      ftbBtn.classList.toggle('busy', !!ftb.in_transition && !ftb.full_black);
      ftbBtn.textContent = ftb.full_black ? 'In black — fade up' : 'Fade to black';
      if (document.activeElement !== ftbRateIn && ftb.rate !== undefined) ftbRateIn.value = ftb.rate;
    }

    function renderOutputs() {
      const s = status.settings;
      const sources = allSources();
      const sig = JSON.stringify([s.aux_count, s.dsk_count, s.macro_count, sources.map(x => [x.id, x.label])]);
      if (sig !== outputsSig) {
        outputsSig = sig;
        const opts = sources.map(x => `<option value="${x.id}" data-token="${escape(x.token)}">${escape(x.label)}${x.name !== x.label ? ' — ' + escape(x.name) : ''}</option>`).join('');
        $('#aux-list').innerHTML = Array.from({ length: s.aux_count }, (_, i) =>
          `<div class="aux-row"><span class="aux-name">Aux ${i + 1}</span><select data-aux="${i}">${opts}</select></div>`).join('');
        $('#aux-list').querySelectorAll('select').forEach(sel => sel.addEventListener('change', () => {
          const token = sel.selectedOptions[0].dataset.token;
          send(`AUX ${+sel.dataset.aux + 1} ${token}`).then(refresh);
          sel.blur();
        }));
        $('#dsk-list').innerHTML = Array.from({ length: s.dsk_count }, (_, i) =>
          `<div class="dsk-row"><span class="dsk-name">DSK ${i + 1}</span>
             <button class="btn small" type="button" data-dsk="${i}" data-cmd="DSK ${i + 1} TOGGLE">On air</button>
             <button class="btn small" type="button" data-cmd="DSK ${i + 1} AUTO">Auto</button></div>`).join('');
        $('#macro-list').innerHTML = Array.from({ length: s.macro_count }, (_, i) =>
          `<button class="key" type="button" data-macro="${i}" data-cmd="MACRO ${i + 1}">Macro ${i + 1}</button>`).join('');
      }
      const aux = sw().aux || {};
      $('#aux-list').querySelectorAll('select').forEach(sel => {
        const cur = aux[sel.dataset.aux];
        if (document.activeElement !== sel && cur !== undefined) sel.value = String(cur);
      });
      const dsk = sw().dsk || {};
      document.querySelectorAll('[data-dsk]').forEach(b => {
        const d = dsk[b.dataset.dsk] || {};
        b.classList.toggle('on-air', !!d.on_air);
        b.textContent = d.on_air ? 'On air' : 'Off air';
      });
      const macros = sw().macros || {};
      document.querySelectorAll('[data-macro]').forEach(b => {
        const m = macros[b.dataset.macro];
        if (m) {
          b.textContent = m.is_used && m.name ? m.name : `Macro ${+b.dataset.macro + 1}`;
          b.disabled = !m.is_used;
        } else {
          b.disabled = false;
        }
      });
    }

    function renderStrip() {
      const c = status.connection;
      const conn = $('#conn');
      conn.dataset.state = c.state;
      $('.conn-text', conn).textContent = c.product || c.host;
      $('#strip-model').textContent = c.product || ({ disabled: 'Not connected', connecting: 'Connecting…', loading: 'Loading switcher state…' }[c.state] || 'ATEM');
      $('#strip-host').textContent = `${c.host}:${c.port}`;
      const vm = sw().video_mode;
      $('#strip-mode').textContent = vm && vm.name ? `${vm.name}` + (c.protocol_version ? ` · protocol ${c.protocol_version}` : '') : '';
      const u = status.udp;
      $('#strip-udp').textContent = u.enabled ? (u.running ? `UDP control on port ${u.port}` : `UDP control not running`) : 'UDP control off';
      $('#btn-connect').hidden = c.state !== 'disabled';
      $('#btn-disconnect').hidden = c.state === 'disabled';

      const notice = $('#notice');
      let text = '';
      let isErr = false;
      if (u.enabled && !u.running && u.error) { text = `UDP listener is not running: ${u.error}`; isErr = true; }
      else if (c.state === 'connecting') text = `Connecting to ${c.host}:${c.port}` + (c.last_error ? ` — ${c.last_error}. Retrying.` : '…');
      else if (c.state === 'disabled') text = 'Not connected. Press Connect, or turn on auto-connect in Settings.';
      notice.hidden = !text;
      notice.textContent = text;
      notice.classList.toggle('error', isErr);
      if (u.last) {
        $('#udp-last').textContent = `Last UDP from ${u.last.from}: ${u.last.text} → ${(u.last.reply || []).join(' | ')}`;
      }
      const disconnected = c.state !== 'connected' && c.state !== 'loading';
      document.querySelectorAll('#pgm-keys .key, #pvw-keys .key, .transition .key, #btn-fade, .macro-list .key, .dsk-row .btn')
        .forEach(b => { b.disabled = disconnected; });
      document.querySelectorAll('#aux-list select').forEach(s => { s.disabled = disconnected; });
    }

    function renderEvents(events) {
      if (!events.length) return;
      const log = $('#log');
      const html = events.slice().reverse().map(e => {
        const { via, ...rest } = e.fields || {};
        const fields = Object.entries(rest).map(([k, v]) => `${k}=${escape(v)}`).join(' ');
        return `<li class="${e.level}"><time>${fmtTime(e.ts)}</time><span class="via">${escape(via || '')}</span><span><span class="msg">${escape(e.message)}</span> <span class="fields">${fields}</span></span></li>`;
      }).join('');
      log.insertAdjacentHTML('afterbegin', html);
      while (log.children.length > 200) log.removeChild(log.lastChild);
      lastEventId = events[events.length - 1].id;
    }

    function render() {
      renderStrip();
      renderBuses();
      renderTransition();
      renderOutputs();
    }

    let inFlight = false;
    async function refresh() {
      if (inFlight || document.hidden) return;
      inFlight = true;
      try {
        const [st, ev] = await Promise.all([api('/api/status'), api(`/api/events?since=${lastEventId}`)]);
        status = st;
        render();
        renderEvents(ev.events);
      } catch (err) {
        $('#conn').dataset.state = 'connecting';
        $('.conn-text', $('#conn')).textContent = 'Setter unreachable';
      } finally {
        inFlight = false;
      }
    }

    render();
    refresh();
    setInterval(refresh, 1000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(); });
  }

  /* ---------------------------------------------------------------- settings */
  function settingsPage() {
    const form = $('#settings-form');
    const result = $('#settings-result');
    const syslogStatus = $('#syslog-status');
    const syslogTest = $('#btn-syslog-test');

    function showSyslog(st) {
      if (!syslogStatus) return;
      syslogStatus.textContent = st.running
        ? `Sent ${st.sent}, failed ${st.failed}` + (st.last_error ? ` — last error: ${st.last_error}` : '')
        : 'Not running';
      if (syslogTest) syslogTest.disabled = !st.running;
    }
    if (syslogTest) {
      syslogTest.addEventListener('click', async () => {
        syslogTest.disabled = true;
        try {
          const res = await api('/api/syslog/test', { method: 'POST' });
          showSyslog(res.status);
          toast(res.ok ? 'Test message delivered — check the server' : `Not delivered: ${res.error}`, !res.ok);
        } catch (err) {
          toast(err.message, true);
        } finally {
          syslogTest.disabled = false;
        }
      });
    }
    form.addEventListener('submit', async ev => {
      ev.preventDefault();
      const data = {};
      for (const el of form.elements) {
        if (!el.name) continue;
        data[el.name] = el.type === 'checkbox' ? el.checked : el.value;
      }
      result.textContent = 'Saving…';
      try {
        const res = await api('/api/settings', { method: 'PUT', body: data });
        if (!res.ok) throw new Error(res.error || 'could not save');
        const n = Object.keys(res.changed).length;
        result.textContent = n ? `Saved ${n} change${n === 1 ? '' : 's'}` : 'Nothing changed';
        toast(n ? 'Settings saved' : 'Nothing changed');
        if (syslogStatus) api('/api/syslog').then(showSyslog).catch(() => {});
      } catch (err) {
        result.textContent = err.message;
        toast(err.message, true);
      }
    });
  }

  /* Bootstrap from the page marker rather than an inline <script>, so the
     Content-Security-Policy can forbid inline script entirely. */
  document.addEventListener('DOMContentLoaded', () => {
    const page = document.body.dataset.page;
    if (page === 'dashboard') {
      const el = $('#initial-status');
      let initial = null;
      try { initial = JSON.parse(el.textContent); } catch (e) { initial = null; }
      if (initial) dashboard(initial);
    } else if (page === 'settings') {
      settingsPage();
    }
  });

  return { dashboard, settingsPage, send, toast };
})();
