// Konsol MCP Marketing: klien mcpk_, key LLM Gateway per agent, status .env (memakai /admin/*).
'use strict';

(() => {
  const { h, api, toast, confirm, showSecret, form, help, badge, table, btn, action, when, genBase64, genUrlSafe } = Kit;
  const root = document.getElementById('root');
  let meta = null;

  const pageHead = (eyebrow, title, desc, actions) =>
    h('div', { class: 'page-head' }, h('div', {}, h('div', { class: 'eyebrow', text: eyebrow }), h('h1', { text: title }), desc ? h('p', { text: desc }) : null), actions ? h('div', { class: 'actions' }, actions) : null);
  const small = { class: 'btn btn-ghost btn-sm' };
  const smallDanger = { class: 'btn btn-danger btn-sm' };

  const SOURCE_META = {
    agent: { state: 'ok', label: 'Key sendiri' },
    system: { state: 'suspended', label: 'Key bersama (system)' },
    env: { state: 'placeholder', label: 'LLM_GATEWAY_API_KEY (.env)' },
  };
  const sourceBadge = (src, envOk) => (src === 'env' && envOk ? badge('suspended', SOURCE_META.env.label) : badge(SOURCE_META[src].state, SOURCE_META[src].label));

  // ============================================================== 01 Mulai
  async function startPage({ go }) {
    const [env, creds, clients] = await Promise.all([api('GET', '/admin/env-status'), api('GET', '/admin/llm-credentials'), api('GET', '/admin/clients')]);
    const v = Object.fromEntries(env.vars.map((x) => [x.name, x]));
    const dashOk = ['MCP_SERVER_API_KEY', 'MCP_WEBHOOK_SECRET', 'MCP_SERVICE_TOKEN'].every((n) => v[n].state === 'ok');
    const envKeyOk = creds.env_key_state === 'ok';
    const active = creds.effective.filter((a) => a.is_active);
    const llmOk = active.length > 0 && active.every((a) => a.source !== 'env' || envKeyOk);
    const ownKeys = active.filter((a) => a.source === 'agent').length;
    const externals = clients.clients.filter((c) => c.kind !== 'system' && c.status === 'active');

    return h(
      'div',
      { class: 'stack' },
      pageHead('01 · Mulai di sini', 'Ringkasan kredensial MCP', 'MCP memegang dua jenis kredensial: key klien mcpk_ (siapa yang boleh memanggil MCP) dan key LLM lgw_ per agent (MCP memanggil LLM Gateway).'),
      h(
        'section',
        { class: 'card' },
        h('div', { class: 'card-head' }, h('h2', { text: 'Alur key' })),
        Kit.flow(
          [
            { label: 'Dashboard / Claude', sub: 'pemanggil MCP' },
            { label: 'MCP', sub: 'konsol ini', here: true },
            { label: 'LLM Gateway', sub: 'project marketing-mcp' },
            { label: 'OpenAI / dll.', sub: 'key di .env Gateway' },
          ],
          ['MCP_SERVER_API_KEY / mcpk_', 'lgw_ per agent', 'OPENAI_API_KEY'],
        ),
        h('p', { class: 'muted', text: 'Dashboard memakai MCP_SERVER_API_KEY (sama di kedua .env). Klien lain (Claude Desktop, skrip) memakai key mcpk_ dari menu Klien MCP.' }),
      ),
      Kit.checklist([
        { done: true, title: 'Masuk dengan key klien admin', detail: 'Key ini hanya untuk konsol. Jangan dipasang di Claude Desktop atau aplikasi lain.' },
        {
          done: dashOk,
          title: 'Secret penghubung Dashboard terisi',
          detail: dashOk ? 'MCP_SERVER_API_KEY, MCP_WEBHOOK_SECRET, MCP_SERVICE_TOKEN valid. Pastikan nilainya sama persis di .env backend Dashboard.' : 'Ada secret Dashboard yang kosong/placeholder.',
          action: dashOk ? null : btn('Lihat .env', () => go('env'), 'ghost', small),
        },
        {
          done: creds.secret_key_state === 'ok',
          title: 'MCP_SECRET_KEY diisi (kunci enkripsi key per agent)',
          detail: creds.secret_key_state === 'ok' ? 'Key LLM per agent disimpan terenkripsi AES-256-GCM.' : 'Tanpa ini semua agent berbagi LLM_GATEWAY_API_KEY. Buat nilainya di menu File .env.',
          action: creds.secret_key_state === 'ok' ? null : btn('Buat MCP_SECRET_KEY', () => go('env'), 'ghost', small),
        },
        {
          done: llmOk,
          title: 'Setiap agent punya key LLM yang valid',
          detail: llmOk ? `${ownKeys}/${active.length} agent memakai key sendiri (biaya terlacak per agent).` : 'Ada agent yang jatuh ke LLM_GATEWAY_API_KEY yang masih kosong/placeholder → panggilan LLM gagal 401.',
          action: btn('Atur key LLM', () => go('llm'), 'ghost', small),
        },
        {
          done: externals.length > 0,
          title: '(Opsional) Klien eksternal, mis. Claude Desktop',
          detail: externals.length ? `${externals.length} klien aktif.` : 'Hanya perlu bila ingin memakai MCP dari luar Dashboard.',
        },
      ]),
      help(
        'Istilah yang perlu diketahui',
        [
          h('ul', {}, [
            h('li', {}, h('strong', { text: 'Klien MCP (mcpk_)' }), ' — identitas pemanggil MCP + toolset yang boleh dipakai. Disimpan sebagai hash; key lengkap tampil sekali.'),
            h('li', {}, h('strong', { text: 'Key LLM per agent (lgw_)' }), ' — dibuat di Gateway, disimpan MCP terenkripsi. Urutan pakai: key agent → key system → LLM_GATEWAY_API_KEY di .env.'),
            h('li', {}, h('strong', { text: 'Provision' }), ' — MCP meminta Gateway membuat project + key untuk semua agent sekaligus, memakai admin key Gateway yang Anda ketik (tidak disimpan).'),
            h('li', {}, h('strong', { text: 'Cache 60 detik' }), ' — perubahan key berlaku paling lambat 60 detik di server & worker, tanpa restart.'),
          ]),
        ],
        { open: true },
      ),
    );
  }

  // ======================================================== 02 Klien MCP
  async function clientsPage({ refresh }) {
    const { clients } = await api('GET', '/admin/clients');
    const rows = clients.filter((c) => c.status === 'active').concat(clients.filter((c) => c.status !== 'active'));
    return h(
      'div',
      { class: 'stack' },
      pageHead('02 · Klien MCP', 'Klien & key mcpk_', 'Siapa saja yang boleh memanggil MCP dan toolset apa yang boleh mereka pakai.', [action('+ Klien baru', () => createClient(refresh), 'primary')]),
      h(
        'section',
        { class: 'card' },
        table(
          [
            { label: 'Nama', render: (c) => h('div', {}, h('strong', { text: c.name }), h('span', { class: 'sub', text: c.kind === 'system' ? 'sistem · memakai MCP_SERVER_API_KEY' : c.kind })) },
            { label: 'Prefix', render: (c) => (c.kind === 'system' ? '—' : h('code', { text: `mcpk_${c.key_prefix}` })) },
            { label: 'Toolset', render: (c) => h('div', {}, c.toolsets.map((t) => h('span', { class: 'tag', text: t })), c.is_admin ? h('span', { class: 'tag', text: 'ADMIN' }) : null) },
            { label: 'RPM', class: 'num', render: (c) => c.rpm_limit },
            { label: 'Status', render: (c) => h('div', {}, badge(c.status), c.expires_at ? h('span', { class: 'sub', text: `s.d. ${when(c.expires_at)}` }) : null) },
            { label: 'Terakhir dipakai', render: (c) => when(c.last_used_at) },
            {
              label: 'Aksi',
              class: 'actions',
              render: (c) =>
                c.kind === 'system'
                  ? '—'
                  : h(
                      'span',
                      {},
                      action(c.status === 'active' ? 'Rotasi' : 'Aktifkan (key baru)', () => rotateClient(c, refresh), 'ghost', small),
                      c.status === 'active' ? action('Cabut', () => revokeClient(c, refresh), 'danger', smallDanger) : null,
                    ),
            },
          ],
          rows,
        ),
      ),
      help('Contoh pemakaian', [
        'Claude Desktop: buat klien "claude-desktop" dengan toolset toolbox, data, job, trends. Tempel key mcpk_ ke konfigurasi MCP Claude Desktop (header Authorization: Bearer …).',
        'Klien ADMIN bisa mengelola kredensial lewat konsol ini — berikan hanya untuk Anda sendiri.',
        'Cabut = key tidak berlaku lagi (≤ 60 detik). Rotasi = key baru, key lama langsung mati.',
      ]),
    );
  }

  async function createClient(refresh) {
    const v = await form({
      title: 'Klien MCP baru',
      fields: [
        { name: 'name', label: 'Nama klien', required: true, placeholder: 'mis. claude-desktop', help: 'Unik; dipakai di log audit.' },
        { name: 'toolsets', label: 'Toolset yang diizinkan', type: 'checks', value: meta.default_toolsets, options: meta.toolsets.map((t) => ({ value: t, label: t })), help: 'Beri seperlunya. Toolset db & task bisa menulis data.' },
        { name: 'kind', label: 'Jenis', type: 'select', value: 'external', options: [{ value: 'external', label: 'external — aplikasi/skrip' }, { value: 'agent', label: 'agent — agent Dashboard' }] },
        { name: 'rpm', label: 'Batas request/menit', type: 'number', min: 1, max: 10000, value: 120 },
        { name: 'expires', label: 'Kedaluwarsa', type: 'date', help: 'kosong = tidak kedaluwarsa.' },
        { name: 'is_admin', label: 'Klien ADMIN (bisa membuka konsol ini)', type: 'checkbox', help: 'Hanya untuk Anda sendiri.' },
      ],
      submitLabel: 'Buat klien',
    });
    if (!v) return;
    if (!v.toolsets.length) throw new Error('Pilih minimal satu toolset.');
    const created = await api('POST', '/admin/clients', {
      name: v.name,
      toolsets: v.toolsets,
      kind: v.kind,
      is_admin: v.is_admin,
      rpm_limit: v.rpm || 120,
      expires_at: v.expires ? new Date(`${v.expires}T23:59:59`).toISOString() : null,
    });
    await showSecret({ title: `Key klien ${created.client.name}`, secret: created.api_key, note: 'Pakai sebagai header Authorization: Bearer <key> ke http://127.0.0.1:4100/mcp.' });
    refresh();
  }

  async function rotateClient(c, refresh) {
    const ok = await confirm({ title: `Rotasi key ${c.name}?`, message: c.status === 'active' ? `Key mcpk_${c.key_prefix}… langsung tidak berlaku. Klien harus diberi key baru.` : 'Klien diaktifkan lagi dengan key baru.', confirmLabel: 'Buat key baru', danger: c.status === 'active' });
    if (!ok) return;
    const r = await api('POST', `/admin/clients/${c.id}/rotate`);
    await showSecret({ title: `Key baru ${r.client.name}`, secret: r.api_key });
    refresh();
  }

  async function revokeClient(c, refresh) {
    const ok = await confirm({ title: `Cabut klien ${c.name}?`, message: 'Key klien ini tidak berlaku lagi (≤ 60 detik). Bisa diaktifkan lagi lewat "Aktifkan (key baru)".', confirmLabel: 'Cabut', danger: true, typeToConfirm: c.is_admin ? c.name : null });
    if (!ok) return;
    await api('POST', `/admin/clients/${c.id}/revoke`);
    toast(`Klien ${c.name} dicabut`);
    refresh();
  }

  // ================================================== 03 Key LLM per agent
  async function llmPage({ refresh }) {
    const creds = await api('GET', '/admin/llm-credentials');
    const envOk = creds.env_key_state === 'ok';
    const byName = Object.fromEntries(creds.credentials.map((c) => [c.name, c]));
    const rows = [{ agent_key: 'system', display_name: 'Key bersama (system)', is_active: true, system: true }, ...creds.effective];

    const effective = table(
      [
        { label: 'Agent', render: (a) => h('div', {}, h('strong', { text: a.display_name }), h('span', { class: 'sub', text: a.system ? 'cadangan untuk semua agent' : a.agent_key }), !a.is_active ? badge('archived', 'Nonaktif') : null) },
        { label: 'Key yang DIPAKAI', render: (a) => (a.system ? '—' : h('div', {}, sourceBadge(a.source, envOk), a.key_prefix ? h('span', { class: 'sub' }, h('code', { text: a.key_prefix })) : null)) },
        {
          label: 'Key tersimpan',
          render: (a) => {
            const c = byName[a.system ? 'system' : `agent:${a.agent_key}`];
            if (!c) return h('span', { class: 'muted', text: 'belum ada' });
            const st = c.status === 'invalid' ? 'invalid_key' : c.status;
            return h('div', {}, h('code', { text: c.key_prefix }), ' ', badge(st), c.last_error ? h('span', { class: 'sub', text: c.last_error }) : null, h('span', { class: 'sub', text: c.rotated_at ? `dirotasi ${when(c.rotated_at)}` : `dibuat ${when(c.created_at)}` }));
          },
        },
        {
          label: 'Aksi',
          class: 'actions',
          render: (a) => {
            const c = byName[a.system ? 'system' : `agent:${a.agent_key}`];
            return h(
              'span',
              {},
              creds.enabled ? action('Impor', () => importKey(a.agent_key, refresh), 'ghost', small) : null,
              c && c.status === 'active' ? action('Nonaktifkan', () => disableKey(a, refresh), 'danger', smallDanger) : null,
            );
          },
        },
      ],
      rows,
    );

    return h(
      'div',
      { class: 'stack' },
      pageHead('03 · Key LLM per agent', 'Key LLM Gateway per agent', 'Tiap agent memakai key lgw_ sendiri agar biaya per agent terlihat di Gateway dan satu key bocor tidak mengganggu agent lain.', creds.enabled ? [action('Provision otomatis', () => provisionKeys(refresh), 'accent')] : null),
      !creds.enabled
        ? h('div', { class: 'callout callout-warn' }, h('strong', { text: 'Fitur key per agent belum aktif' }), creds.secret_key_state === 'invalid' ? 'MCP_SECRET_KEY di .env formatnya salah (harus 32 byte base64). ' : 'MCP_SECRET_KEY belum diisi di .env MCP. ', 'Buat nilainya di menu File .env, tempel ke .env MCP, lalu restart server & worker MCP. Sementara itu semua agent memakai LLM_GATEWAY_API_KEY.')
        : null,
      !envOk ? h('div', { class: 'callout callout-warn' }, h('strong', { text: 'LLM_GATEWAY_API_KEY di .env belum valid' }), 'Tidak masalah selama semua agent punya key sendiri atau ada key system — kolom "Key yang DIPAKAI" harus bukan .env.') : null,
      h('section', { class: 'card' }, effective),
      help(
        'Cara termudah: Provision otomatis',
        [
          h('ol', {}, [
            h('li', { text: 'Siapkan admin key LLM Gateway (Konsol Gateway → halaman masuk menjelaskan cara membuatnya).' }),
            h('li', { text: 'Tekan "Provision otomatis", tempel admin key, isi batas biaya (disarankan $5/hari, $50/bulan).' }),
            h('li', { text: `MCP membuat project "${meta.gateway_project}" di Gateway + satu key per agent + key system, lalu menyimpannya terenkripsi.` }),
            h('li', { text: 'Selesai — tidak perlu restart (berlaku ≤ 60 detik). Admin key Gateway tidak disimpan.' }),
          ]),
          'Impor: tempel key lgw_ yang dibuat manual di Konsol Gateway. Nonaktifkan: agent kembali memakai key system / .env.',
          'Status "Ditolak Gateway": key dicabut di Gateway. Jalankan Provision otomatis dengan "buat ulang semua key" atau impor key baru.',
        ],
        { open: !creds.credentials.length },
      ),
    );
  }

  async function provisionKeys(refresh) {
    const agents = meta.agents.filter((a) => a.is_active);
    const v = await form({
      title: 'Provision key LLM otomatis',
      intro: `Gateway: ${meta.gateway_admin_url} · project "${meta.gateway_project}". Admin key hanya dipakai untuk permintaan ini dan tidak disimpan.`,
      fields: [
        { name: 'admin_key', label: 'Admin key LLM Gateway', required: true, secret: true, placeholder: 'lgw_xxxxxxxx_…' },
        { name: 'agents', label: 'Agent', type: 'checks', value: agents.map((a) => a.agent_key), options: agents.map((a) => ({ value: a.agent_key, label: a.display_name })), help: 'Key system selalu ikut dibuat.' },
        { name: 'daily_usd', label: 'Kuota biaya harian project (USD)', type: 'number', min: 0, step: '0.01', value: 5 },
        { name: 'monthly_usd', label: 'Kuota biaya bulanan project (USD)', type: 'number', min: 0, step: '0.01', value: 50 },
        { name: 'rpm', label: 'Batas request/menit per key', type: 'number', min: 1, help: 'kosong = default Gateway.' },
        { name: 'force', label: 'Buat ulang SEMUA key (rotasi; key lama dicabut di Gateway)', type: 'checkbox', help: 'Tanpa centang: hanya membuat key yang belum ada.' },
      ],
      submitLabel: 'Provision sekarang',
    });
    if (!v) return;
    const { report } = await api('POST', '/admin/llm-credentials/provision', {
      admin_key: v.admin_key,
      agents: v.agents,
      daily_usd: v.daily_usd,
      monthly_usd: v.monthly_usd,
      rpm: v.rpm,
      force: v.force,
    });
    v.admin_key = '';
    const changed = report.filter((r) => r.action !== 'skipped').length;
    toast(`Provision selesai: ${changed} key dibuat/dirotasi, ${report.length - changed} sudah ada.`);
    const warn = report.find((r) => r.action.includes('gagal'));
    if (warn) toast(`${warn.name}: ${warn.action}`, 'err');
    refresh();
  }

  async function importKey(agentKey, refresh) {
    const v = await form({
      title: `Impor key LLM · ${agentKey}`,
      intro: 'Tempel key lgw_ yang dibuat di Konsol Gateway (project marketing-mcp). Key disimpan terenkripsi; key lama untuk agent ini diganti.',
      fields: [{ name: 'key', label: 'Key LLM Gateway', required: true, secret: true, placeholder: 'lgw_xxxxxxxx_…' }],
      submitLabel: 'Simpan terenkripsi',
    });
    if (!v) return;
    const r = await api('POST', '/admin/llm-credentials/import', { agent: agentKey, key: v.key });
    toast(`Key ${r.credential.key_prefix} tersimpan untuk ${agentKey}`);
    refresh();
  }

  async function disableKey(a, refresh) {
    const ok = await confirm({ title: `Nonaktifkan key ${a.display_name}?`, message: a.system ? 'Agent tanpa key sendiri akan memakai LLM_GATEWAY_API_KEY dari .env.' : 'Agent ini akan memakai key system (atau LLM_GATEWAY_API_KEY bila tidak ada). Key di Gateway TIDAK dicabut — cabut dari Konsol Gateway bila bocor.', confirmLabel: 'Nonaktifkan', danger: true });
    if (!ok) return;
    await api('POST', '/admin/llm-credentials/disable', { agent: a.agent_key });
    toast('Key dinonaktifkan');
    refresh();
  }

  // ========================================================= 04 .env
  async function envPage() {
    const env = await api('GET', '/admin/env-status');
    const groups = [...new Set(env.vars.map((v) => v.group))];
    const problems = env.vars.filter((v) => v.required && v.state !== 'ok');
    const secretKeySet = env.vars.find((v) => v.name === 'MCP_SECRET_KEY').state === 'ok';

    const genButtons = h(
      'div',
      { class: 'actions' },
      btn('MCP_SECRET_KEY', async () => {
        if (secretKeySet) {
          const ok = await confirm({ title: 'MCP_SECRET_KEY sudah terisi', message: 'Mengganti kunci ini membuat semua key LLM yang tersimpan tidak bisa dibuka. Setelah diganti wajib Provision otomatis dengan "buat ulang semua key".', confirmLabel: 'Tetap buat nilai baru', danger: true, typeToConfirm: 'GANTI KUNCI' });
          if (!ok) return;
        }
        await showSecret({ title: 'MCP_SECRET_KEY baru', envLine: 'MCP_SECRET_KEY', secret: genBase64(32), note: 'Tempel baris ini ke .env MCP (hapus tanda # bila baris lama ter-komentar), lalu restart server & worker MCP.' });
      }, secretKeySet ? 'ghost' : 'accent'),
      ' ',
      ['MCP_SERVER_API_KEY', 'MCP_WEBHOOK_SECRET', 'MCP_SERVICE_TOKEN'].map((name) =>
        btn(name, async () => {
          await showSecret({ title: `${name} baru`, envLine: name, secret: genUrlSafe(48), note: `Ganti baris ${name} di .env MCP DAN .env backend Dashboard dengan nilai yang sama, lalu restart keduanya.` });
        }),
      ),
    );

    return h(
      'div',
      { class: 'stack' },
      pageHead('04 · File .env', 'Status .env MCP', 'Konsol hanya MEMBACA status (terisi / kosong / placeholder). Nilai rahasia tidak pernah ditampilkan dan file .env tidak diubah dari sini.'),
      problems.length ? h('div', { class: 'callout callout-err' }, h('strong', { text: 'Ada variabel wajib yang bermasalah' }), problems.map((p) => p.name).join(', ')) : h('div', { class: 'callout callout-ok' }, h('strong', { text: 'Semua variabel wajib terisi' })),
      groups.map((g) =>
        h(
          'section',
          { class: 'card' },
          h('div', { class: 'card-head' }, h('h2', { text: g })),
          table(
            [
              { label: 'Variabel', render: (v) => h('div', {}, h('code', { text: v.name }), h('span', { class: 'sub', text: v.required ? 'wajib' : 'opsional' })) },
              { label: 'Status', render: (v) => (v.secret ? badge(v.state) : h('code', { text: String(v.value) })) },
              { label: 'Keterangan', render: (v) => v.help },
            ],
            env.vars.filter((v) => v.group === g),
          ),
        ),
      ),
      h('section', { class: 'card' }, h('div', { class: 'card-head' }, h('h2', { text: 'Buat nilai rahasia baru' })), h('p', { class: 'muted', text: 'Dibuat di browser ini (crypto acak), tidak dikirim ke server. Tempel ke .env lalu restart.' }), genButtons),
      help('Cara aman mengubah .env', [
        h('ol', {}, [
          h('li', { text: 'Salin dulu .env menjadi .env.bak (cadangan).' }),
          h('li', { text: 'Ubah satu baris; format NAMA=nilai, tanpa komentar di akhir baris (scripts\\env.bat tidak mendukungnya).' }),
          h('li', { text: 'Secret penghubung Dashboard harus sama persis di .env MCP dan .env backend Dashboard.' }),
          h('li', { text: 'Restart server & worker MCP (scripts\\stop-all.bat lalu start-all.bat). Bila gagal start, pesan error menyebut nama variabel yang salah.' }),
          h('li', { text: 'Token X / Threads: isi di grup "Sumber data", lalu restart worker.' }),
        ]),
      ]),
    );
  }

  // ======================================================== login & boot
  const HOW_TO = [
    h('p', { text: 'Buat sekali lewat terminal (Anaconda Prompt, env mcp) di folder mcp:' }),
    h('p', {}, h('code', { text: 'python scripts\\manage.py create-client --name konsol-admin --toolsets toolbox --admin' })),
    h('p', { text: 'Key mcpk_ tampil sekali — simpan di password manager. Key admin sengaja hanya bisa dibuat dari CLI pertama kali; setelah masuk, klien admin lain bisa dibuat dari konsol.' }),
  ];

  function showLogin(reason) {
    root.replaceChildren(
      Kit.loginScreen({
        title: 'Konsol MCP Marketing',
        subtitle: 'Klien · key LLM per agent · .env',
        intro: reason || 'Masukkan key klien MCP ber-role admin untuk mengelola klien mcpk_, key LLM per agent, dan memeriksa .env.',
        keyLabel: 'Key klien admin MCP',
        keyPattern: 'mcpk_xxxxxxxx_…',
        howTo: HOW_TO,
        onSubmit: async (key) => {
          try {
            meta = await api('GET', '/admin/meta', undefined, { key });
          } catch (e) {
            if (e.status === 403) throw new Error('Key valid, tetapi bukan klien admin.');
            if (e.status === 401) throw new Error('Key tidak dikenali / sudah dicabut / kedaluwarsa.');
            throw e;
          }
          Kit.unlock(key);
          boot();
        },
      }),
    );
  }

  function boot() {
    const { root: app, go } = Kit.shell({
      title: 'Konsol MCP Marketing',
      subtitle: location.host,
      onLogout: () => Kit.lock('Sesi dikunci. Key sudah dihapus dari memori.'),
      pages: [
        { id: 'start', label: 'Mulai di sini', hint: 'alur key & checklist', render: startPage },
        { id: 'clients', label: 'Klien MCP', hint: 'key mcpk_: buat, rotasi, cabut', render: clientsPage },
        { id: 'llm', label: 'Key LLM per agent', hint: 'provision, impor, nonaktifkan', render: llmPage },
        { id: 'env', label: 'File .env', hint: 'status variabel & secret baru', render: envPage },
      ],
    });
    root.replaceChildren(app);
    go('start');
  }

  Kit.setOnLock((reason) => {
    meta = null;
    showLogin(reason);
  });
  showLogin();
})();
