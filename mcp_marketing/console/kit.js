// Kit Admin Console (salinan identik ada di repo mcp & llm-genai).
// Aturan keamanan: teks dari server SELALU lewat textContent (tanpa innerHTML); admin key hanya di
// variabel memori (tidak di localStorage/sessionStorage/cookie) dan terhapus saat tab ditutup,
// tombol "Kunci" ditekan, atau idle IDLE_LOCK_MS.
'use strict';

const IDLE_LOCK_MS = 15 * 60 * 1000;

const Kit = (() => {
  let adminKey = null;
  let idleTimer = null;
  let onLock = () => {};

  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? '' : String(v));
    }
    for (const c of children.flat()) {
      if (c === null || c === undefined || c === false) continue;
      el.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return el;
  }

  // ------------------------------------------------------------------ sesi
  function resetIdle() {
    if (!adminKey) return;
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => lock('Sesi dikunci otomatis setelah 15 menit tidak aktif.'), IDLE_LOCK_MS);
  }
  ['click', 'keydown'].forEach((ev) => document.addEventListener(ev, resetIdle, { passive: true }));

  function unlock(key) {
    adminKey = key;
    resetIdle();
  }
  function lock(reason) {
    adminKey = null;
    clearTimeout(idleTimer);
    onLock(reason);
  }
  function setOnLock(fn) {
    onLock = fn;
  }

  // ------------------------------------------------------------------- api
  class ApiError extends Error {
    constructor(status, code, message) {
      super(message);
      this.status = status;
      this.code = code;
    }
  }

  async function api(method, path, body, { key } = {}) {
    const token = key || adminKey;
    const res = await fetch(path, {
      method,
      cache: 'no-store',
      credentials: 'omit',
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body !== undefined ? { 'Content-Type': 'application/json' } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    const text = await res.text();
    let data = null;
    try {
      data = text ? JSON.parse(text) : null;
    } catch {
      data = null;
    }
    if (!res.ok) {
      const err = (data && (data.error || data.detail)) || {};
      const code = typeof err === 'object' ? err.code || err.type || `HTTP_${res.status}` : `HTTP_${res.status}`;
      const message = typeof err === 'object' ? err.message || code : String(err);
      if ((res.status === 401) && !key) lock('Admin key ditolak atau sudah dicabut. Masuk lagi.');
      throw new ApiError(res.status, code, message);
    }
    return data;
  }

  // ---------------------------------------------------------------- toast
  function toast(message, tone = 'ok') {
    const host = document.getElementById('toasts');
    const el = h('div', { class: `toast toast-${tone}`, role: tone === 'err' ? 'alert' : 'status' }, message);
    host.append(el);
    setTimeout(() => el.classList.add('toast-out'), 4200);
    setTimeout(() => el.remove(), 4700);
  }
  function fail(err) {
    toast(err instanceof Error ? err.message : String(err), 'err');
  }

  // --------------------------------------------------------------- dialog
  function dialog(title, body, actions) {
    const dlg = h('dialog', { class: 'dlg', 'aria-label': title });
    const form = h('form', { method: 'dialog', class: 'dlg-inner' }, h('h2', { class: 'dlg-title', text: title }), body, h('div', { class: 'dlg-actions' }, actions));
    dlg.append(form);
    document.body.append(dlg);
    dlg.addEventListener('close', () => setTimeout(() => dlg.remove(), 0));
    dlg.showModal();
    return dlg;
  }

  function confirm({ title, message, confirmLabel = 'Lanjutkan', danger = false, typeToConfirm = null }) {
    return new Promise((resolve) => {
      const input = typeToConfirm ? h('input', { class: 'input', 'aria-label': `Ketik ${typeToConfirm}`, autocomplete: 'off' }) : null;
      const ok = h('button', { class: danger ? 'btn btn-danger' : 'btn btn-primary', value: 'ok', disabled: !!typeToConfirm }, confirmLabel);
      if (input) input.addEventListener('input', () => (ok.disabled = input.value.trim() !== typeToConfirm));
      const body = h('div', { class: 'dlg-body' }, h('p', { text: message }), typeToConfirm ? h('label', { class: 'field' }, h('span', { class: 'field-label' }, 'Ketik ', h('code', { text: typeToConfirm }), ' untuk konfirmasi'), input) : null);
      const dlg = dialog(title, body, [h('button', { class: 'btn btn-ghost', value: 'cancel' }, 'Batal'), ok]);
      dlg.addEventListener('close', () => resolve(dlg.returnValue === 'ok'));
    });
  }

  // Secret yang tampil SEKALI: harus dicentang "sudah disimpan" sebelum ditutup.
  function showSecret({ title, secret, note, envLine }) {
    return new Promise((resolve) => {
      const value = envLine ? `${envLine}=${secret}` : secret;
      const field = h('input', { class: 'input mono secret-field', readonly: true, value, 'aria-label': 'Nilai rahasia' });
      const copy = h('button', { type: 'button', class: 'btn btn-ghost' }, 'Salin');
      copy.addEventListener('click', async () => {
        try {
          await navigator.clipboard.writeText(value);
          copy.textContent = 'Tersalin ✓';
        } catch {
          field.select();
          copy.textContent = 'Tekan Ctrl+C';
        }
      });
      const agree = h('input', { type: 'checkbox' });
      const done = h('button', { class: 'btn btn-primary', value: 'ok', disabled: true }, 'Selesai');
      agree.addEventListener('change', () => (done.disabled = !agree.checked));
      const body = h(
        'div',
        { class: 'dlg-body' },
        h('p', { class: 'once-banner', text: 'Nilai ini hanya ditampilkan SEKALI. Setelah ditutup tidak bisa dilihat lagi.' }),
        note ? h('p', { class: 'muted', text: note }) : null,
        h('div', { class: 'secret-row' }, field, copy),
        h('label', { class: 'check' }, agree, ' Saya sudah menyimpan nilai ini di tempat aman'),
      );
      const dlg = dialog(title, body, [done]);
      dlg.addEventListener('cancel', (e) => e.preventDefault());
      dlg.addEventListener('close', () => {
        field.value = '';
        resolve();
      });
      field.select();
    });
  }

  // ------------------------------------------------------- pemilih model
  // Dropdown yang bisa dicari. items: [{value, label, group, sub, search}] (value unik).
  // multiple=false → value string; multiple=true → array (chip). Hanya nilai dari items yang
  // bisa dipilih (anti salah ketik); nilai lama yang tidak ada di items tetap ditampilkan.
  function picker({ items, value, multiple = false, placeholder = 'Ketik untuk mencari…' }) {
    const known = new Map(items.map((it) => [it.value, it]));
    let selected = multiple ? [...(value || [])] : value ? [value] : [];
    let active = -1;
    let shown = [];
    const chips = h('div', { class: 'picker-chips' });
    const search = h('input', { class: 'input', type: 'text', placeholder, autocomplete: 'off', spellcheck: 'false', role: 'combobox', 'aria-expanded': 'false' });
    const list = h('div', { class: 'picker-list', role: 'listbox', hidden: true });
    const root = h('div', { class: 'picker' + (multiple ? ' picker-multi' : '') }, multiple ? chips : null, search, list);

    const labelOf = (v) => (known.get(v) || { label: v }).label;
    const hay = (it) => `${it.value} ${it.label} ${it.group || ''} ${it.search || ''}`.toLowerCase();

    function renderChips() {
      if (!multiple) return;
      chips.replaceChildren(
        ...selected.map((v) =>
          h('span', { class: 'picker-chip' + (known.has(v) ? '' : ' picker-unknown'), title: known.has(v) ? v : `${v} (tidak ada di katalog)` }, v,
            h('button', { type: 'button', class: 'picker-x', 'aria-label': `hapus ${v}`, onclick: () => { selected = selected.filter((x) => x !== v); renderChips(); renderList(); } }, '×')),
        ),
      );
    }

    function renderList() {
      const q = search.value.trim().toLowerCase();
      const singleText = !multiple && selected.length && search.value === selected[0];
      shown = items.filter((it) => !(multiple && selected.includes(it.value)) && (!q || singleText || hay(it).includes(q)));
      active = Math.min(Math.max(active, 0), shown.length - 1);
      const rows = [];
      let group = null;
      shown.forEach((it, i) => {
        if (it.group !== group) {
          group = it.group;
          if (group) rows.push(h('div', { class: 'picker-group', text: group }));
        }
        rows.push(
          h('div', {
            class: 'picker-opt' + (i === active ? ' is-active' : '') + (selected.includes(it.value) ? ' is-selected' : ''),
            role: 'option',
            onmousedown: (e) => { e.preventDefault(); choose(it.value); },
          }, h('span', { class: 'picker-label', text: it.label }), it.sub ? h('span', { class: 'picker-sub', text: it.sub }) : null),
        );
      });
      list.replaceChildren(...(rows.length ? rows : [h('div', { class: 'picker-empty', text: 'Tidak ada yang cocok.' })]));
      const cur = list.querySelector('.is-active');
      if (cur) cur.scrollIntoView({ block: 'nearest' });
    }

    function open() {
      list.hidden = false;
      search.setAttribute('aria-expanded', 'true');
      renderList();
    }
    function close() {
      list.hidden = true;
      search.setAttribute('aria-expanded', 'false');
      if (!multiple) search.value = selected[0] || '';
    }
    function choose(v) {
      if (multiple) {
        if (!selected.includes(v)) selected.push(v);
        search.value = '';
        renderChips();
        renderList();
      } else {
        selected = [v];
        search.value = v;
        close();
        search.title = labelOf(v);
      }
    }

    search.addEventListener('focus', open);
    search.addEventListener('click', open);
    search.addEventListener('blur', close);
    search.addEventListener('input', () => { active = 0; open(); });
    search.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        if (list.hidden) return open();
        active = (active + (e.key === 'ArrowDown' ? 1 : -1) + shown.length) % Math.max(shown.length, 1);
        renderList();
      } else if (e.key === 'Enter') {
        e.preventDefault(); // jangan submit dialog
        if (!list.hidden && shown[active]) choose(shown[active].value);
      } else if (e.key === 'Escape' && !list.hidden) {
        e.preventDefault();
        e.stopPropagation(); // tutup daftar dulu, bukan dialog
        close();
      } else if (e.key === 'Backspace' && multiple && !search.value && selected.length) {
        selected.pop();
        renderChips();
        renderList();
      }
    });

    search.value = multiple ? '' : selected[0] || '';
    renderChips();
    return {
      el: root,
      getValue: () => (multiple ? [...selected] : selected[0] || ''),
      get value() { return multiple ? selected.join(',') : selected[0] || ''; },
    };
  }

  // Label harga ESTIMASI dari `pricing` katalog Gateway (model: kind; route: kinds + rentang).
  const usdFmt = (n) => `$${Number(n).toLocaleString('en-US', { maximumFractionDigits: 3 })}`;
  const range = (r) => (r[0] === r[1] ? usdFmt(r[0]) : `${usdFmt(r[0])}–${usdFmt(r[1])}`);
  function priceLabel(p) {
    if (!p) return '';
    if (p.kind) {
      if (p.kind === 'free') return 'GRATIS';
      if (p.kind === 'local') return 'lokal (gratis)';
      if (p.kind === 'unset') return 'harga belum diisi';
      if (p.per_image) return `${usdFmt(p.per_image)}/gambar (estimasi)`;
      return `${usdFmt(p.input_per_mtok)} in / ${usdFmt(p.output_per_mtok)} out per 1 jt token (estimasi)`;
    }
    const kinds = p.kinds || [];
    if (!kinds.length) return 'tanpa model aktif';
    const parts = [];
    if (kinds.includes('free') || kinds.includes('local')) parts.push(kinds.includes('paid') ? 'gratis s/d' : 'GRATIS');
    if (p.input_per_mtok) parts.push(`${range(p.input_per_mtok)} in / ${range(p.output_per_mtok)} out per 1 jt token (estimasi)`);
    if (kinds.includes('unset')) parts.push(parts.length ? '+ ada yang harganya belum diisi' : 'harga belum diisi');
    return parts.join(' ');
  }

  // fields: [{name,label,type,help,required,placeholder,value,min,max,step,options,secret,rows}]
  function form({ title, intro, fields, submitLabel = 'Simpan' }) {
    return new Promise((resolve) => {
      const inputs = {};
      const rows = fields.map((f) => {
        let input;
        if (f.type === 'select') {
          input = h('select', { class: 'input', name: f.name }, f.options.map((o) => h('option', { value: o.value, selected: o.value === f.value }, o.label)));
        } else if (f.type === 'checkbox') {
          input = h('input', { type: 'checkbox', name: f.name, checked: !!f.value });
          inputs[f.name] = input;
          return h('label', { class: 'check' }, input, ' ', f.label, f.help ? h('small', { class: 'field-help', text: f.help }) : null);
        } else if (f.type === 'checks') {
          const box = h('div', { class: 'checks' });
          input = { boxes: f.options.map((o) => {
            const cb = h('input', { type: 'checkbox', value: o.value, checked: (f.value || []).includes(o.value) });
            box.append(h('label', { class: 'chip-check' }, cb, ' ', o.label));
            return cb;
          }) };
          inputs[f.name] = input;
          return h('div', { class: 'field' }, h('span', { class: 'field-label', text: f.label }), box, f.help ? h('small', { class: 'field-help', text: f.help }) : null);
        } else if (f.type === 'picker') {
          const p = picker({ items: f.items, value: f.value, multiple: f.multiple, placeholder: f.placeholder });
          inputs[f.name] = p;
          return h('div', { class: 'field' }, h('span', { class: 'field-label' }, f.label, f.required ? h('span', { class: 'req', text: ' *' }) : null), p.el, f.help ? h('small', { class: 'field-help', text: f.help }) : null);
        } else if (f.type === 'textarea') {
          input = h('textarea', { class: 'input', name: f.name, rows: f.rows || 8, placeholder: f.placeholder, required: f.required, spellcheck: 'false' });
          input.value = f.value || '';
        } else {
          input = h('input', {
            class: f.secret ? 'input mono' : 'input',
            name: f.name,
            type: f.secret ? 'password' : f.type || 'text',
            placeholder: f.placeholder,
            value: f.value,
            min: f.min,
            max: f.max,
            step: f.step,
            required: f.required,
            autocomplete: 'off',
            spellcheck: 'false',
          });
        }
        inputs[f.name] = input;
        return h('label', { class: 'field' }, h('span', { class: 'field-label' }, f.label, f.required ? h('span', { class: 'req', text: ' *' }) : null), input, f.help ? h('small', { class: 'field-help', text: f.help }) : null);
      });
      const errorBox = h('p', { class: 'form-error', role: 'alert' });
      const body = h('div', { class: 'dlg-body' }, intro ? h('p', { class: 'muted', text: intro }) : null, rows, errorBox);
      const submit = h('button', { class: 'btn btn-primary', value: 'ok' }, submitLabel);
      const dlg = dialog(title, body, [h('button', { class: 'btn btn-ghost', value: 'cancel', formnovalidate: true }, 'Batal'), submit]);
      const formEl = dlg.querySelector('form');
      formEl.addEventListener('submit', (e) => {
        if (e.submitter && e.submitter.value === 'cancel') return;
        const missing = fields.find((f) => f.required && !String(inputs[f.name].value || '').trim());
        if (missing) {
          e.preventDefault();
          errorBox.textContent = `${missing.label} wajib diisi.`;
        }
      });
      dlg.addEventListener('close', () => {
        if (dlg.returnValue !== 'ok') return resolve(null);
        const out = {};
        for (const f of fields) {
          const el = inputs[f.name];
          if (f.type === 'checkbox') out[f.name] = el.checked;
          else if (f.type === 'checks') out[f.name] = el.boxes.filter((b) => b.checked).map((b) => b.value);
          else if (f.type === 'picker') out[f.name] = el.getValue();
          else if (f.type === 'number') out[f.name] = el.value === '' ? null : Number(el.value);
          else out[f.name] = el.value.trim();
          if (f.secret) el.value = '';
        }
        resolve(out);
      });
    });
  }

  // ----------------------------------------------------------- komponen
  function help(title, paragraphs, { open = false } = {}) {
    return h(
      'details',
      { class: 'help', open },
      h('summary', {}, h('span', { class: 'help-icon', 'aria-hidden': 'true', text: '?' }), title),
      h('div', { class: 'help-body' }, paragraphs.map((p) => (p instanceof Node ? p : h('p', { text: p })))),
    );
  }

  const STATE_LABEL = {
    ok: 'Terisi',
    missing: 'Kosong',
    placeholder: 'Masih CHANGE_ME',
    weak: 'Terlalu pendek',
    invalid: 'Format salah',
    not_needed: 'Tidak perlu',
    active: 'Aktif',
    revoked: 'Dicabut',
    invalid_key: 'Ditolak Gateway',
    expired: 'Kedaluwarsa',
    suspended: 'Ditangguhkan',
    archived: 'Diarsipkan',
  };
  const STATE_TONE = {
    ok: 'ok', active: 'ok', not_needed: 'mute', missing: 'warn', placeholder: 'err', weak: 'err',
    invalid: 'err', revoked: 'mute', invalid_key: 'err', expired: 'mute', suspended: 'warn', archived: 'mute',
  };
  function badge(state, label) {
    return h('span', { class: `badge badge-${STATE_TONE[state] || 'mute'}` }, h('span', { class: 'lamp', 'aria-hidden': 'true' }), label || STATE_LABEL[state] || state);
  }

  // columns: [{label, render(row) -> Node|string, class}]
  function table(columns, rows, { empty = 'Belum ada data.' } = {}) {
    if (!rows.length) return h('div', { class: 'empty', text: empty });
    return h(
      'div',
      { class: 'table-wrap' },
      h(
        'table',
        { class: 'table' },
        h('thead', {}, h('tr', {}, columns.map((c) => h('th', { scope: 'col', class: c.class, text: c.label })))),
        h('tbody', {}, rows.map((r) => h('tr', {}, columns.map((c) => h('td', { class: c.class }, c.render(r)))))),
      ),
    );
  }

  function btn(label, onclick, variant = 'ghost', extra = {}) {
    return h('button', { type: 'button', class: `btn btn-${variant}`, onclick, ...extra }, label);
  }

  // Tombol async: nonaktif selama berjalan agar tidak dobel klik.
  function action(label, fn, variant = 'ghost', extra = {}) {
    const b = btn(label, async () => {
      b.disabled = true;
      try {
        await fn();
      } catch (e) {
        fail(e);
      } finally {
        b.disabled = false;
      }
    }, variant, extra);
    return b;
  }

  function when(iso) {
    if (!iso) return '—';
    const d = new Date(iso);
    return isNaN(d) ? '—' : d.toLocaleString('id-ID', { dateStyle: 'medium', timeStyle: 'short' });
  }

  // Generator secret di browser (crypto.getRandomValues) — tidak lewat server.
  function randomBytes(n) {
    const a = new Uint8Array(n);
    crypto.getRandomValues(a);
    return a;
  }
  function genBase64(nBytes) {
    return btoa(String.fromCharCode(...randomBytes(nBytes)));
  }
  function genUrlSafe(nBytes) {
    return genBase64(nBytes).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  }

  // ------------------------------------------------------- shell & nav
  function shell({ title, subtitle, pages, onLogout }) {
    const nav = h('nav', { class: 'nav', 'aria-label': 'Menu konsol' });
    const main = h('main', { class: 'main', id: 'main', tabindex: '-1' });
    let current = null;
    async function go(id) {
      const page = pages.find((p) => p.id === id) || pages[0];
      current = page.id;
      nav.querySelectorAll('button').forEach((b) => b.setAttribute('aria-current', b.dataset.id === page.id ? 'page' : 'false'));
      main.replaceChildren(h('div', { class: 'loading', text: 'Memuat…' }));
      try {
        const content = await page.render({ go, refresh: () => go(current) });
        if (current === page.id) main.replaceChildren(content);
      } catch (e) {
        main.replaceChildren(h('div', { class: 'card card-err' }, h('h2', { text: 'Gagal memuat halaman' }), h('p', { text: e.message }), btn('Coba lagi', () => go(page.id), 'primary')));
      }
    }
    pages.forEach((p, i) => {
      const b = h('button', { type: 'button', class: 'nav-item', 'data-id': p.id, onclick: () => go(p.id) }, h('span', { class: 'nav-num', text: String(i + 1).padStart(2, '0') }), h('span', {}, h('span', { class: 'nav-label', text: p.label }), h('small', { class: 'nav-hint', text: p.hint })));
      nav.append(b);
    });
    const root = h(
      'div',
      { class: 'app' },
      h(
        'header',
        { class: 'topbar' },
        h('div', { class: 'brand' }, h('span', { class: 'brand-mark', 'aria-hidden': 'true' }), h('div', {}, h('strong', { text: title }), h('small', { text: subtitle }))),
        h('div', { class: 'topbar-right' }, h('span', { class: 'session-pill' }, h('span', { class: 'lamp lamp-ok' }), 'Sesi aktif · terkunci otomatis 15 menit idle'), btn('Kunci sesi', onLogout, 'ghost')),
      ),
      h('div', { class: 'layout' }, nav, main),
    );
    return { root, go };
  }

  function loginScreen({ title, subtitle, intro, keyLabel, keyPattern, howTo, onSubmit }) {
    const input = h('input', { class: 'input mono', type: 'password', autocomplete: 'off', spellcheck: 'false', 'aria-label': keyLabel, placeholder: keyPattern, required: true });
    const err = h('p', { class: 'form-error', role: 'alert' });
    const submit = h('button', { class: 'btn btn-primary btn-lg', type: 'submit' }, 'Masuk');
    const formEl = h(
      'form',
      { class: 'login-card' },
      h('div', { class: 'brand brand-lg' }, h('span', { class: 'brand-mark', 'aria-hidden': 'true' }), h('div', {}, h('strong', { text: title }), h('small', { text: subtitle }))),
      h('p', { class: 'lead', text: intro }),
      h('label', { class: 'field' }, h('span', { class: 'field-label', text: keyLabel }), input),
      err,
      submit,
      h('ul', { class: 'assurances' }, h('li', { text: 'Key hanya disimpan di memori tab ini — tidak di cookie/localStorage.' }), h('li', { text: 'Tutup tab atau tekan "Kunci sesi" = key terhapus.' }), h('li', { text: 'Konsol ini hanya bisa dibuka dari komputer ini (localhost).' })),
      help('Belum punya admin key? Begini cara membuatnya', howTo),
    );
    formEl.addEventListener('submit', async (e) => {
      e.preventDefault();
      err.textContent = '';
      submit.disabled = true;
      const key = input.value.trim();
      try {
        await onSubmit(key);
        input.value = '';
      } catch (ex) {
        err.textContent = ex.message;
      } finally {
        submit.disabled = false;
      }
    });
    setTimeout(() => input.focus(), 0);
    return h('div', { class: 'login' }, formEl);
  }

  // Diagram alur key: [{label, sub, tone}] dihubungkan panah, dengan label key di panah.
  function flow(nodes, edges) {
    const out = h('div', { class: 'flow', role: 'img', 'aria-label': nodes.map((n) => n.label).join(' → ') });
    nodes.forEach((n, i) => {
      out.append(h('div', { class: `flow-node ${n.here ? 'flow-here' : ''}` }, h('strong', { text: n.label }), h('small', { text: n.sub })));
      if (i < edges.length) out.append(h('div', { class: 'flow-edge' }, h('code', { text: edges[i] }), h('span', { class: 'flow-arrow', 'aria-hidden': 'true', text: '→' })));
    });
    return out;
  }

  // Checklist setup: [{done, title, detail, action?: Node}]
  function checklist(items) {
    const done = items.filter((i) => i.done).length;
    return h(
      'section',
      { class: 'card checklist' },
      h('div', { class: 'card-head' }, h('h2', { text: 'Langkah setup' }), h('span', { class: 'progress-label', text: `${done}/${items.length} selesai` })),
      h('div', { class: 'progress', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': String(items.length), 'aria-valuenow': String(done) }, h('span', { style: null, class: 'progress-fill', 'data-pct': String(Math.round((done / items.length) * 100)) })),
      h('ol', { class: 'steps' }, items.map((it) => h('li', { class: it.done ? 'step step-done' : 'step' }, h('span', { class: 'step-mark', 'aria-hidden': 'true', text: it.done ? '✓' : '' }), h('div', { class: 'step-text' }, h('strong', { text: it.title }), h('p', { text: it.detail }), it.action || null)))),
    );
  }

  return { h, api, ApiError, toast, fail, confirm, showSecret, form, picker, priceLabel, help, badge, table, btn, action, when, genBase64, genUrlSafe, shell, loginScreen, flow, checklist, unlock, lock, setOnLock };
})();

// lebar progress diset lewat JS (CSP melarang atribut style inline dari markup)
new MutationObserver(() => {
  document.querySelectorAll('.progress-fill[data-pct]').forEach((el) => {
    el.style.width = `${el.dataset.pct}%`;
    el.removeAttribute('data-pct');
  });
}).observe(document.documentElement, { childList: true, subtree: true });
