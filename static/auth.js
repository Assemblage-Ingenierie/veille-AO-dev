/* Veille AO — connexion et identité, partagé par toutes les pages.
 *
 * À charger dans <head>, APRÈS supabase-js (version UMD) :
 *   <script src="https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.117.3/dist/umd/supabase.js"></script>
 *   <script src="/static/auth.js"></script>
 *
 * Attribut <html data-auth="…"> :
 *   (absent)  page réservée aux membres validés (redirection vers /login sinon)
 *   "admin"   page réservée aux administrateurs
 *   "public"  page accessible sans connexion (page de connexion)
 *
 * API exposée : VeilleAuth.onReady(fn), VeilleAuth.user, VeilleAuth.signOut(),
 *               VeilleAuth.getClient(), VeilleAuth.refreshAssignBadge()
 */
(function () {
  'use strict';

  const MODE = document.documentElement.dataset.auth || 'member';
  const PUBLIC = MODE === 'public';
  const originalFetch = window.fetch.bind(window);
  let client = null;
  let configPromise = null;

  // Rien n'est affiché tant que l'accès n'est pas vérifié.
  if (!PUBLIC) {
    const style = document.createElement('style');
    style.textContent = 'html.auth-pending body{visibility:hidden}';
    document.head.appendChild(style);
    document.documentElement.classList.add('auth-pending');
  }

  async function getClient() {
    if (client) return client;
    configPromise = configPromise || originalFetch('/api/config').then((r) => r.json());
    const cfg = await configPromise;
    client = window.supabase.createClient(cfg.supabase_url, cfg.supabase_publishable_key, {
      auth: { persistSession: true, autoRefreshToken: true, detectSessionInUrl: true, flowType: 'pkce' },
    });
    return client;
  }

  async function token() {
    const c = await getClient();
    const { data } = await c.auth.getSession();
    return data && data.session ? data.session.access_token : null;
  }

  function loginUrl() {
    return '/login?next=' + encodeURIComponent(location.pathname + location.search);
  }

  // Ajoute le jeton à chaque appel /api/… (sauf /api/config, publique).
  window.fetch = async function (input, init) {
    const url = typeof input === 'string' ? input : input.url;
    const isApi = url.startsWith('/api/') && !url.startsWith('/api/config');
    if (!isApi) return originalFetch(input, init);

    const opts = Object.assign({}, init);
    const headers = new Headers(opts.headers || (typeof input === 'string' ? undefined : input.headers));
    const t = await token();
    if (t) headers.set('Authorization', 'Bearer ' + t);
    opts.headers = headers;

    const res = await originalFetch(input, opts);
    if (!PUBLIC && res.status === 401) location.replace(loginUrl());
    if (!PUBLIC && res.status === 403) {
      try {
        const body = await res.clone().json();
        if (['pending', 'rejected', 'revoked'].includes(body.code)) location.replace(loginUrl());
      } catch (e) { /* réponse non JSON : on laisse la page gérer */ }
    }
    return res;
  };

  const domReady = new Promise((resolve) => {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', resolve, { once: true });
    else resolve();
  });
  let resolveReady;
  const ready = new Promise((resolve) => { resolveReady = resolve; });

  async function signOut() {
    try { await (await getClient()).auth.signOut(); } catch (e) { /* session déjà expirée */ }
    location.replace('/login');
  }

  async function refreshAssignBadge() {
    const badge = document.getElementById('assign-badge');
    const mBadge = document.getElementById('mobile-badge');
    if (!badge && !mBadge) return;
    let unread = 0;
    try {
      const r = await window.fetch('/api/assignments/me');
      if (r.ok) unread = (await r.json()).unread || 0;
    } catch (e) { /* réseau */ }
    for (const el of [badge, mBadge]) {
      if (!el) continue;
      el.textContent = unread;
      el.style.display = unread > 0 ? '' : 'none';
    }
  }

  function renderSidebar(me) {
    // Bloc « utilisateur connecté » à la place de l'ancien sélecteur « Je suis ».
    const box = document.querySelector('.sidebar-user');
    if (box) {
      box.innerHTML = '';
      const label = document.createElement('div');
      label.className = 'sidebar-user-label';
      label.textContent = me.role === 'admin' ? 'Connecté · admin' : 'Connecté';
      const name = document.createElement('div');
      name.style.cssText = 'font-size:.8rem;font-weight:600;color:var(--ai-violet);overflow:hidden;text-overflow:ellipsis;white-space:nowrap';
      name.textContent = me.name;
      name.title = me.email;
      const out = document.createElement('button');
      out.type = 'button';
      out.textContent = 'Se déconnecter';
      out.style.cssText = 'margin-top:.35rem;background:none;border:none;padding:0;cursor:pointer;font-family:inherit;font-size:.72rem;color:var(--ai-rouge);text-decoration:underline';
      out.addEventListener('click', signOut);
      box.append(label, name, out);
    }

    // Lien « Administration » pour les admins.
    const spacer = document.querySelector('.sidebar .sidebar-spacer');
    if (me.role === 'admin' && spacer && !document.querySelector('.sidebar a[href="/admin"]')) {
      const a = document.createElement('a');
      a.className = 'sidebar-nav' + (location.pathname === '/admin' ? ' active' : '');
      a.href = '/admin';
      a.textContent = 'Administration';
      spacer.parentNode.insertBefore(a, spacer);
    }

    // Éléments réservés aux admins (ex. bouton « Actualiser »).
    if (me.role !== 'admin') {
      document.querySelectorAll('[data-admin-only]').forEach((el) => { el.style.display = 'none'; });
    }
  }

  async function guard() {
    if (PUBLIC) return;
    if (!(await token())) { location.replace(loginUrl()); return; }
    const r = await window.fetch('/api/me');
    if (!r.ok) { location.replace(loginUrl()); return; }
    const me = await r.json();
    if (me.status !== 'approved') { location.replace(loginUrl()); return; }
    if (MODE === 'admin' && me.role !== 'admin') { location.replace('/'); return; }

    api.user = me;
    await domReady;
    renderSidebar(me);
    document.documentElement.classList.remove('auth-pending');
    resolveReady(me);
    refreshAssignBadge();
    setInterval(refreshAssignBadge, 2 * 60 * 1000);
  }

  const api = {
    user: null,
    ready,
    onReady(fn) { ready.then(fn); },
    signOut,
    getClient,
    refreshAssignBadge,
    loginUrl,
  };
  window.VeilleAuth = api;

  guard().catch((e) => {
    console.error('Vérification de connexion impossible :', e);
    if (!PUBLIC) location.replace(loginUrl());
  });
})();
