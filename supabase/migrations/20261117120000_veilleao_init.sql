-- ════════════════════════════════════════════════════════════════════════════
-- Veille AO — schéma initial dans le projet Supabase partagé INTERNAL
--
-- Conventions INTERNAL respectées :
--   • tables dans `public`, préfixées par l'application (`veilleao_`) ;
--   • une table de profils propre à l'application (`veilleao_profiles`) ;
--   • RLS activée partout, fonctions SECURITY DEFINER avec search_path vide.
--
-- Choix propres à Veille AO :
--   • fonctions de droits dédiées veilleao_is_admin() / veilleao_is_approved()
--     — on NE réutilise PAS public.is_admin(), qui lit aichantier_profiles ;
--   • pas de trigger sur auth.users : le profil (= la demande d'accès) est créé
--     à la première connexion à Veille AO via veilleao_request_access(), pour
--     ne pas transformer chaque nouveau compte Assemblage en demande d'accès ;
--   • écritures de la collecte (AO, cache, journaux) réservées à service_role
--     (backend Vercel) ; les utilisateurs validés écrivent seulement le tri,
--     les assignations et les mots-clés partagés.
-- ════════════════════════════════════════════════════════════════════════════


-- ── 1. Profils / accès ──────────────────────────────────────────────────────

create table public.veilleao_profiles (
    id                 uuid primary key references auth.users (id) on delete cascade,
    email              text not null,
    full_name          text,
    role               text not null default 'user'
                       check (role in ('user', 'admin')),
    status             text not null default 'pending'
                       check (status in ('pending', 'approved', 'rejected', 'revoked')),
    requested_at       timestamptz not null default now(),
    decided_by         uuid references auth.users (id) on delete set null,
    decided_at         timestamptz,
    admin_notified_at  timestamptz          -- null = demande pas encore signalée aux admins
);
comment on table public.veilleao_profiles is
    'Veille AO — une ligne par personne ayant demandé l''accès (créée à la 1re connexion).';
create index veilleao_profiles_pending_idx
    on public.veilleao_profiles (requested_at)
    where status = 'pending' and admin_notified_at is null;


create or replace function public.veilleao_is_approved()
returns boolean
language sql stable security definer set search_path = ''
as $$
    select exists (
        select 1 from public.veilleao_profiles
        where id = auth.uid() and status = 'approved'
    );
$$;

create or replace function public.veilleao_is_admin()
returns boolean
language sql stable security definer set search_path = ''
as $$
    select exists (
        select 1 from public.veilleao_profiles
        where id = auth.uid() and status = 'approved' and role = 'admin'
    );
$$;


-- Demande d'accès : crée le profil de l'appelant (statut 'pending') s'il n'existe
-- pas, puis renvoie son profil. Les valeurs role/status ne sont pas modifiables
-- par l'appelant.
create or replace function public.veilleao_request_access()
returns public.veilleao_profiles
language plpgsql security definer set search_path = ''
as $$
declare
    v_uid uuid := auth.uid();
    v_row public.veilleao_profiles;
begin
    if v_uid is null then
        raise exception 'Non authentifié' using errcode = '42501';
    end if;

    insert into public.veilleao_profiles (id, email, full_name)
    select u.id,
           u.email,
           coalesce(u.raw_user_meta_data ->> 'full_name', u.raw_user_meta_data ->> 'name')
    from auth.users u
    where u.id = v_uid
    on conflict (id) do nothing;

    select * into v_row from public.veilleao_profiles where id = v_uid;
    return v_row;
end;
$$;


-- Trace de la décision admin (qui / quand) lors d'un changement de statut ou de rôle.
create or replace function public.veilleao_profiles_set_decision()
returns trigger
language plpgsql set search_path = ''
as $$
begin
    if new.status is distinct from old.status or new.role is distinct from old.role then
        new.decided_at := now();
        new.decided_by := coalesce(auth.uid(), new.decided_by);
    end if;
    return new;
end;
$$;

create trigger veilleao_profiles_decision
    before update of status, role on public.veilleao_profiles
    for each row execute function public.veilleao_profiles_set_decision();


-- ── 2. Avis d'appels d'offres (+ tri commun) ────────────────────────────────

create table public.veilleao_notices (
    id                text primary key,          -- préfixes existants : BM-, UNDP-, GIZ-, BERD-, AFD-, TED-, EF-
    bailleur          text not null,
    titre             text not null,
    projet            text,
    secteur           text,
    type_avis         text,
    mode_selection    text,
    langue            text,
    date_publication  date,
    date_limite       date,
    pays              text,
    lien              text,
    pertinence        text,
    texte_complet     text,                      -- non transmis à l'UI (scoring uniquement)
    date_ajout        timestamptz not null default now(),   -- date de (dernière) collecte
    actif             boolean not null default true,        -- false si deadline dépassée
    triage            text check (triage in ('non-pertinent', 'pre-tri', 'pertinent')),
    triaged_by        uuid references auth.users (id) on delete set null,
    triaged_at        timestamptz
);
comment on table public.veilleao_notices is
    'Veille AO — avis collectés (BM, PNUD, GIZ, BERD, AFD, TED, Expertise France) et tri commun.';
create index veilleao_notices_bailleur_idx    on public.veilleao_notices (bailleur);
create index veilleao_notices_date_limite_idx on public.veilleao_notices (date_limite);
create index veilleao_notices_actif_idx       on public.veilleao_notices (actif) where actif;
create index veilleao_notices_triage_idx      on public.veilleao_notices (triage) where triage is not null;


-- Qui a trié et quand : renseigné automatiquement, jamais saisi par le client.
create or replace function public.veilleao_notices_set_triage_meta()
returns trigger
language plpgsql set search_path = ''
as $$
begin
    if new.triage is distinct from old.triage then
        new.triaged_at := case when new.triage is null then null else now() end;
        new.triaged_by := case when new.triage is null then null
                               else coalesce(auth.uid(), new.triaged_by) end;
    end if;
    return new;
end;
$$;

create trigger veilleao_notices_triage_meta
    before update of triage on public.veilleao_notices
    for each row execute function public.veilleao_notices_set_triage_meta();


-- Écriture en lot depuis la collecte (backend, service_role uniquement).
--   p_refresh = false → INSERT … ON CONFLICT DO NOTHING (un AO existant n'est jamais écrasé)
--   p_refresh = true  → met à jour deadline + date de collecte et réactive (cas TED)
-- Les dates vides ('') des collecteurs deviennent NULL.
create or replace function public.veilleao_upsert_notices(p_rows jsonb, p_refresh boolean default false)
returns integer
language plpgsql security definer set search_path = ''
as $$
declare
    v_count integer;
begin
    if p_refresh then
        insert into public.veilleao_notices as t
            (id, bailleur, titre, projet, secteur, type_avis, mode_selection, langue,
             date_publication, date_limite, pays, lien, pertinence, texte_complet)
        select distinct on (r.id)
               r.id, r.bailleur, r.titre, r.projet, r.secteur, r.type_avis, r.mode_selection,
               r.langue, nullif(r.date_publication, '')::date, nullif(r.date_limite, '')::date,
               r.pays, r.lien, r.pertinence, r.texte_complet
        from jsonb_to_recordset(p_rows) as r(
             id text, bailleur text, titre text, projet text, secteur text, type_avis text,
             mode_selection text, langue text, date_publication text, date_limite text,
             pays text, lien text, pertinence text, texte_complet text)
        where r.id is not null
        on conflict (id) do update
            set date_limite = excluded.date_limite,
                date_ajout  = now(),
                actif       = true;
    else
        insert into public.veilleao_notices
            (id, bailleur, titre, projet, secteur, type_avis, mode_selection, langue,
             date_publication, date_limite, pays, lien, pertinence, texte_complet)
        select distinct on (r.id)
               r.id, r.bailleur, r.titre, r.projet, r.secteur, r.type_avis, r.mode_selection,
               r.langue, nullif(r.date_publication, '')::date, nullif(r.date_limite, '')::date,
               r.pays, r.lien, r.pertinence, r.texte_complet
        from jsonb_to_recordset(p_rows) as r(
             id text, bailleur text, titre text, projet text, secteur text, type_avis text,
             mode_selection text, langue text, date_publication text, date_limite text,
             pays text, lien text, pertinence text, texte_complet text)
        where r.id is not null
        on conflict (id) do nothing;
    end if;

    get diagnostics v_count = row_count;
    return v_count;
end;
$$;

-- Passe à actif = false les AO dont la deadline est dépassée (appelé après chaque collecte).
create or replace function public.veilleao_mark_expired()
returns integer
language plpgsql security definer set search_path = ''
as $$
declare
    v_count integer;
begin
    update public.veilleao_notices
       set actif = false
     where actif and date_limite is not null and date_limite < current_date;
    get diagnostics v_count = row_count;
    return v_count;
end;
$$;


-- ── 3. Cache des détails enrichis (TOR, notices PDF…) ───────────────────────

create table public.veilleao_notice_details (
    notice_id   text primary key references public.veilleao_notices (id) on delete cascade,
    detail      jsonb not null,                 -- {_failed: true} si la source était injoignable
    fetched_at  timestamptz not null default now()
);


-- ── 4. Assignations entre membres de l'équipe ───────────────────────────────

create table public.veilleao_assignments (
    notice_id    text not null references public.veilleao_notices (id) on delete cascade,
    assignee     uuid not null references auth.users (id) on delete cascade,
    assigned_by  uuid references auth.users (id) on delete set null,
    assigned_at  timestamptz not null default now(),
    note         text,
    seen         boolean not null default false,
    primary key (notice_id, assignee)
);
create index veilleao_assignments_assignee_idx on public.veilleao_assignments (assignee);

create or replace function public.veilleao_assignments_set_author()
returns trigger
language plpgsql set search_path = ''
as $$
begin
    new.assigned_by := coalesce(auth.uid(), new.assigned_by);
    new.assigned_at := now();
    return new;
end;
$$;

create trigger veilleao_assignments_author
    before insert on public.veilleao_assignments
    for each row execute function public.veilleao_assignments_set_author();


-- ── 5. Réglages partagés (mots-clés) — jamais de secret ici ─────────────────

create table public.veilleao_settings (
    key         text primary key,
    value       jsonb not null,
    updated_by  uuid references auth.users (id) on delete set null,
    updated_at  timestamptz not null default now()
);

create or replace function public.veilleao_settings_set_author()
returns trigger
language plpgsql set search_path = ''
as $$
begin
    new.updated_by := coalesce(auth.uid(), new.updated_by);
    new.updated_at := now();
    return new;
end;
$$;

create trigger veilleao_settings_author
    before insert or update on public.veilleao_settings
    for each row execute function public.veilleao_settings_set_author();


-- ── 6. Journaux : collectes et emails envoyés aux admins ────────────────────

create table public.veilleao_collect_runs (
    id            bigint generated always as identity primary key,
    source        text not null,                -- bm, pnud, giz, berd, afd, ted, ef
    started_at    timestamptz not null default now(),
    finished_at   timestamptz,
    fetched       integer,
    inserted      integer,
    error         text,
    triggered_by  uuid references auth.users (id) on delete set null   -- null = collecte planifiée
);
create index veilleao_collect_runs_started_idx on public.veilleao_collect_runs (started_at desc);

-- Sert au plafond « N emails par heure » : les demandes non signalées restent
-- en file (admin_notified_at null) et partent groupées au passage suivant.
create table public.veilleao_mail_log (
    id           bigint generated always as identity primary key,
    kind         text not null default 'access_request',
    sent_at      timestamptz not null default now(),
    recipients   text[] not null,
    profile_ids  uuid[] not null default '{}',
    error        text
);
create index veilleao_mail_log_sent_idx on public.veilleao_mail_log (sent_at desc);


-- ── 7. Droits : privilèges par table (au plus juste) ────────────────────────
-- Supabase accorde par défaut tous les droits à anon / authenticated sur public :
-- on les retire puis on n'accorde que le nécessaire. service_role garde tout.

revoke all on public.veilleao_profiles        from anon, authenticated;
revoke all on public.veilleao_notices         from anon, authenticated;
revoke all on public.veilleao_notice_details  from anon, authenticated;
revoke all on public.veilleao_assignments     from anon, authenticated;
revoke all on public.veilleao_settings        from anon, authenticated;
revoke all on public.veilleao_collect_runs    from anon, authenticated;
revoke all on public.veilleao_mail_log        from anon, authenticated;

grant select                         on public.veilleao_profiles       to authenticated;
grant update (role, status)          on public.veilleao_profiles       to authenticated;  -- admins (RLS)
grant select                         on public.veilleao_notices        to authenticated;
grant update (triage)                on public.veilleao_notices        to authenticated;  -- tri commun
grant select                         on public.veilleao_notice_details to authenticated;
grant select, insert, delete         on public.veilleao_assignments    to authenticated;
grant update (seen)                  on public.veilleao_assignments    to authenticated;
grant select, insert, update         on public.veilleao_settings       to authenticated;
grant select                         on public.veilleao_collect_runs   to authenticated;
grant select                         on public.veilleao_mail_log       to authenticated;

revoke all on function public.veilleao_is_approved()                 from public, anon;
revoke all on function public.veilleao_is_admin()                    from public, anon;
revoke all on function public.veilleao_request_access()              from public, anon;
revoke all on function public.veilleao_upsert_notices(jsonb, boolean) from public, anon, authenticated;
revoke all on function public.veilleao_mark_expired()                from public, anon, authenticated;
grant execute on function public.veilleao_is_approved()              to authenticated;
grant execute on function public.veilleao_is_admin()                 to authenticated;
grant execute on function public.veilleao_request_access()           to authenticated;
grant execute on function public.veilleao_upsert_notices(jsonb, boolean) to service_role;
grant execute on function public.veilleao_mark_expired()             to service_role;


-- ── 8. Row Level Security ───────────────────────────────────────────────────

alter table public.veilleao_profiles        enable row level security;
alter table public.veilleao_notices         enable row level security;
alter table public.veilleao_notice_details  enable row level security;
alter table public.veilleao_assignments     enable row level security;
alter table public.veilleao_settings        enable row level security;
alter table public.veilleao_collect_runs    enable row level security;
alter table public.veilleao_mail_log        enable row level security;

-- Profils : chacun voit le sien ; les membres validés voient l'équipe validée
-- (liste d'assignation) ; les admins voient tout et décident.
create policy veilleao_profiles_select on public.veilleao_profiles
    for select to authenticated
    using (id = auth.uid()
           or public.veilleao_is_admin()
           or (status = 'approved' and public.veilleao_is_approved()));

create policy veilleao_profiles_admin_update on public.veilleao_profiles
    for update to authenticated
    using (public.veilleao_is_admin())
    with check (public.veilleao_is_admin());

-- AO : lecture et tri pour les comptes validés.
create policy veilleao_notices_select on public.veilleao_notices
    for select to authenticated
    using (public.veilleao_is_approved());

create policy veilleao_notices_triage on public.veilleao_notices
    for update to authenticated
    using (public.veilleao_is_approved())
    with check (public.veilleao_is_approved());

-- Cache des détails : lecture seule pour les comptes validés.
create policy veilleao_notice_details_select on public.veilleao_notice_details
    for select to authenticated
    using (public.veilleao_is_approved());

-- Assignations : visibles par l'équipe validée ; on assigne en son nom propre,
-- uniquement à un membre validé ; l'assigné marque « vu » ; suppression par
-- l'auteur, l'assigné ou un admin.
create policy veilleao_assignments_select on public.veilleao_assignments
    for select to authenticated
    using (public.veilleao_is_approved());

create policy veilleao_assignments_insert on public.veilleao_assignments
    for insert to authenticated
    with check (public.veilleao_is_approved()
                and exists (select 1 from public.veilleao_profiles p
                            where p.id = assignee and p.status = 'approved'));

create policy veilleao_assignments_seen on public.veilleao_assignments
    for update to authenticated
    using (assignee = auth.uid() and public.veilleao_is_approved())
    with check (assignee = auth.uid());

create policy veilleao_assignments_delete on public.veilleao_assignments
    for delete to authenticated
    using (public.veilleao_is_approved()
           and (assigned_by = auth.uid() or assignee = auth.uid() or public.veilleao_is_admin()));

-- Réglages : lecture par l'équipe validée ; les mots-clés partagés sont
-- modifiables par l'équipe, toute autre clé uniquement par un admin.
create policy veilleao_settings_select on public.veilleao_settings
    for select to authenticated
    using (public.veilleao_is_approved());

create policy veilleao_settings_insert on public.veilleao_settings
    for insert to authenticated
    with check (public.veilleao_is_admin()
                or (public.veilleao_is_approved()
                    and key in ('params_keywords', 'exclude_keywords')));

create policy veilleao_settings_update on public.veilleao_settings
    for update to authenticated
    using (public.veilleao_is_admin()
           or (public.veilleao_is_approved()
               and key in ('params_keywords', 'exclude_keywords')))
    with check (public.veilleao_is_admin()
                or (public.veilleao_is_approved()
                    and key in ('params_keywords', 'exclude_keywords')));

-- Journaux : collectes visibles par l'équipe, emails par les admins seulement.
create policy veilleao_collect_runs_select on public.veilleao_collect_runs
    for select to authenticated
    using (public.veilleao_is_approved());

create policy veilleao_mail_log_select on public.veilleao_mail_log
    for select to authenticated
    using (public.veilleao_is_admin());


-- ── 9. Premier administrateur ───────────────────────────────────────────────

insert into public.veilleao_profiles
       (id, email, full_name, role, status, decided_at, admin_notified_at)
select u.id,
       u.email,
       coalesce(u.raw_user_meta_data ->> 'full_name', u.raw_user_meta_data ->> 'name'),
       'admin', 'approved', now(), now()
from auth.users u
where lower(u.email) = 'louis@assemblage.net'
on conflict (id) do update
    set role = 'admin', status = 'approved';


-- ── 10. (Plus tard, après le 1er déploiement Vercel) tâches planifiées ──────
-- Nécessite l'URL de l'application et le secret CRON stockés dans Supabase Vault
-- (noms : veilleao_app_url, veilleao_cron_secret). Non exécuté par cette migration.
--
-- select cron.schedule('veilleao-notify-access', '*/10 * * * *', $job$
--   select net.http_post(
--     url     := (select decrypted_secret from vault.decrypted_secrets where name = 'veilleao_app_url')
--                || '/api/cron/notify-access',
--     headers := jsonb_build_object('Authorization', 'Bearer ' ||
--                (select decrypted_secret from vault.decrypted_secrets where name = 'veilleao_cron_secret')),
--     timeout_milliseconds := 30000);
-- $job$);
--
-- select cron.schedule('veilleao-collecte-quotidienne', '0 5 * * *', $job$
--   select net.http_post(
--     url     := (select decrypted_secret from vault.decrypted_secrets where name = 'veilleao_app_url')
--                || '/api/cron/daily',
--     headers := jsonb_build_object('Authorization', 'Bearer ' ||
--                (select decrypted_secret from vault.decrypted_secrets where name = 'veilleao_cron_secret')),
--     timeout_milliseconds := 300000);
-- $job$);
