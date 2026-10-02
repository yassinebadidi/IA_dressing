-- =====================================================================
--  Météo-Dressing : schéma PostgreSQL (Supabase compatible)
--  A exécuter dans Supabase > SQL Editor (ou psql), AVANT 02_seed.sql
-- =====================================================================

-- ---------- Tables -----------------------------------------------------

create table if not exists app_users (
  id               serial primary key,
  name             text not null,
  city             text not null default 'Paris',
  latitude         double precision not null default 48.8566,
  longitude        double precision not null default 2.3522,
  preferred_style  text not null default 'casual'
                   check (preferred_style in ('casual','chic','sport','business','streetwear','boheme')),
  default_occasion text not null default 'quotidien',
  cold_sensitivity text not null default 'normal'
                   check (cold_sensitivity in ('frileux','normal','chaud')),
  active           boolean not null default true,
  created_at       timestamptz not null default now()
);

create table if not exists wardrobe_items (
  id            serial primary key,
  user_id       int not null references app_users(id) on delete cascade,
  dataset_code  int,                           -- id dans le CSV d'origine
  name          text not null,
  category      text not null
                check (category in ('base_top','mid_layer','bottom','outerwear','shoes','accessory')),
  subcategory   text,
  color         text,
  color_family  text,                          -- neutre / chaud / froid
  warmth        smallint not null check (warmth between 0 and 5),
  waterproof    boolean not null default false,
  styles        text[] not null default '{casual}',
  formality     smallint not null default 2 check (formality between 1 and 5),
  material      text,
  status        text not null default 'available'
                check (status in ('available','laundry','retired')),
  created_at    timestamptz not null default now()
);
create index if not exists idx_wardrobe_user on wardrobe_items(user_id, status);

create table if not exists outfit_history (
  id               bigserial primary key,
  user_id          int not null references app_users(id) on delete cascade,
  outfit_date      date not null,
  created_at       timestamptz not null default now(),
  source           text not null default 'schedule'
                   check (source in ('schedule','webhook','eval','eval_dry')),
  is_current       boolean not null default true,   -- false = remplacée ou run d'éval "à blanc"
  style            text,
  occasion         text,
  city             text,
  weather          jsonb,                           -- snapshot météo + features calculées
  weather_band     text,                            -- very_cold / cold / cool / mild / warm / hot
  items            jsonb not null,                  -- {"base_top":10,"bottom":34,...}
  item_ids         int[] not null,
  combo_key        text,                            -- "base_top-bottom" ex: "10-34"
  title            text,
  advice           text,
  tips             jsonb,
  generation_mode  text not null default 'llm' check (generation_mode in ('llm','fallback')),
  model            text,
  llm_raw          text,
  latency_ms       int,
  checks           jsonb,                           -- résultats des contrôles automatiques
  rule_score       numeric(4,3),                    -- % de contrôles passés (0..1)
  user_rating      smallint check (user_rating between 1 and 5),
  user_comment     text
);
-- une seule tenue "officielle" par utilisateur et par jour
create unique index if not exists uq_outfit_current
  on outfit_history(user_id, outfit_date) where is_current;
create index if not exists idx_outfit_user_date on outfit_history(user_id, outfit_date desc);

create table if not exists pipeline_errors (
  id            bigserial primary key,
  created_at    timestamptz not null default now(),
  workflow      text,
  node          text,
  message       text,
  execution_id  text,
  execution_url text,
  payload       jsonb
);

-- ---------- Contexte pour le pipeline -----------------------------------
-- Renvoie en UN seul objet JSON : profil, garde-robe disponible enrichie
-- de l'historique (dernier port, nb de ports sur 14 j, note moyenne),
-- les tenues récentes et les pièces déjà proposées/rejetées le jour même.

create or replace function get_outfit_context(p_user_id int, p_date date)
returns jsonb
language sql stable
as $$
with u as (
  select * from app_users where id = p_user_id
),
hist as (   -- tenues officielles avant la date cible (fenêtre 30 j)
  select h.*
  from outfit_history h
  where h.user_id = p_user_id and h.is_current
    and h.outfit_date < p_date and h.outfit_date >= p_date - 30
),
wear as (
  select unnest(item_ids) as item_id, outfit_date, user_rating from hist
),
wear_stats as (
  select item_id,
         max(outfit_date)                                       as last_worn_on,
         count(*) filter (where outfit_date >= p_date - 14)     as wear_count_14d,
         round(avg(user_rating)::numeric, 2)                    as avg_rating
  from wear group by item_id
),
wardrobe as (
  select jsonb_agg(jsonb_build_object(
           'id', w.id, 'name', w.name, 'category', w.category,
           'subcategory', w.subcategory, 'color', w.color,
           'color_family', w.color_family, 'warmth', w.warmth,
           'waterproof', w.waterproof, 'styles', to_jsonb(w.styles),
           'formality', w.formality, 'material', w.material,
           'last_worn_on', s.last_worn_on,
           'days_since_worn', case when s.last_worn_on is null then null
                                   else p_date - s.last_worn_on end,
           'wear_count_14d', coalesce(s.wear_count_14d, 0),
           'avg_rating', s.avg_rating
         ) order by w.category, w.id) as items
  from wardrobe_items w
  left join wear_stats s on s.item_id = w.id
  where w.user_id = p_user_id and w.status = 'available'
),
recent as (
  select jsonb_agg(jsonb_build_object(
           'date', outfit_date, 'title', title, 'item_ids', to_jsonb(item_ids),
           'combo_key', combo_key, 'rating', user_rating
         ) order by outfit_date desc) as outfits
  from hist where outfit_date >= p_date - 14
),
today as (   -- tout ce qui a déjà été proposé pour la date cible
  select coalesce(jsonb_agg(distinct x), '[]'::jsonb) as ids
  from outfit_history h, unnest(h.item_ids) x
  where h.user_id = p_user_id and h.outfit_date = p_date and h.source <> 'eval_dry'
)
select case when (select count(*) from u) = 0 then null else
  jsonb_build_object(
    'user', (select to_jsonb(u) - 'created_at' from u),
    'wardrobe', coalesce((select items from wardrobe), '[]'::jsonb),
    'recent_outfits', coalesce((select outfits from recent), '[]'::jsonb),
    'already_proposed_today', (select ids from today),
    'unavailable_items', (select count(*) from wardrobe_items
                          where user_id = p_user_id and status <> 'available')
  ) end;
$$;

-- ---------- Enregistrement d'une tenue -----------------------------------
-- Une régénération le même jour remplace la tenue officielle (l'ancienne
-- reste en base avec is_current=false => sert à éviter de reproposer).

create or replace function save_outfit(p jsonb)
returns jsonb
language plpgsql
as $$
declare
  v_user   int  := (p->>'user_id')::int;
  v_date   date := (p->>'outfit_date')::date;
  v_source text := coalesce(p->>'source', 'schedule');
  v_dry    boolean := v_source = 'eval_dry';
  v_id     bigint;
begin
  if not v_dry then
    update outfit_history set is_current = false
     where user_id = v_user and outfit_date = v_date and is_current;
  end if;

  insert into outfit_history (
    user_id, outfit_date, source, is_current, style, occasion, city, weather,
    weather_band, items, item_ids, combo_key, title, advice, tips,
    generation_mode, model, llm_raw, latency_ms, checks, rule_score)
  values (
    v_user, v_date, v_source, not v_dry, p->>'style', p->>'occasion', p->>'city',
    p->'weather', p->>'weather_band', p->'items',
    array(select jsonb_array_elements_text(p->'item_ids')::int),
    p->>'combo_key', p->>'title', p->>'advice', p->'tips',
    coalesce(p->>'generation_mode','llm'), p->>'model', p->>'llm_raw',
    nullif(p->>'latency_ms','')::int, p->'checks', nullif(p->>'rule_score','')::numeric)
  returning id into v_id;

  return jsonb_build_object('outfit_id', v_id, 'outfit_date', v_date, 'saved', true);
end;
$$;

-- ---------- Feedback utilisateur (évaluation humaine + adaptation) -------

create or replace function rate_outfit(p_id bigint, p_rating int, p_comment text default null)
returns jsonb
language plpgsql
as $$
begin
  if p_rating is null or p_rating not between 1 and 5 then
    return jsonb_build_object('ok', false, 'error', 'La note doit être entre 1 et 5');
  end if;
  update outfit_history
     set user_rating = p_rating, user_comment = coalesce(p_comment, user_comment)
   where id = p_id;
  if not found then
    return jsonb_build_object('ok', false, 'error', 'Tenue introuvable');
  end if;
  return jsonb_build_object('ok', true, 'outfit_id', p_id, 'rating', p_rating);
end;
$$;

-- ---------- Vues d'évaluation --------------------------------------------

-- Taux de réussite de chaque contrôle automatique, par mode de génération
create or replace view v_eval_checks as
select h.generation_mode, c.key as check_name,
       count(*) as n,
       round(avg(case when c.value = 'true'::jsonb then 1 else 0 end) * 100, 1) as pass_rate_pct
from outfit_history h, jsonb_each(h.checks) c
where h.checks is not null
group by h.generation_mode, c.key
order by h.generation_mode, c.key;

-- Synthèse globale
create or replace view v_eval_summary as
select
  count(*)                                                        as total_runs,
  round(avg(case when generation_mode='llm' then 1 else 0 end)*100,1) as llm_success_pct,
  round(avg(rule_score)*100, 1)                                   as avg_rule_score_pct,
  round(avg(case when rule_score = 1 then 1 else 0 end)*100, 1)   as perfect_outfits_pct,
  round(avg(latency_ms))                                          as avg_latency_ms,
  count(user_rating)                                              as rated_outfits,
  round(avg(user_rating), 2)                                      as avg_user_rating
from outfit_history;

-- Répétitions : même combinaison haut+bas portée 2 fois en 14 jours
create or replace view v_repetitions as
select a.user_id, a.outfit_date, a.combo_key, b.outfit_date as previous_date
from outfit_history a
join outfit_history b
  on a.user_id = b.user_id and a.combo_key = b.combo_key
 and b.outfit_date < a.outfit_date and b.outfit_date >= a.outfit_date - 14
 and a.is_current and b.is_current;

-- ---------- Données pour la page web (mini UI servie par n8n) -------------

create or replace function app_page_data(p_user_id int)
returns jsonb
language sql stable
as $$
select jsonb_build_object(
  'users', (select coalesce(jsonb_agg(jsonb_build_object(
              'id', id, 'name', name, 'city', city, 'style', preferred_style,
              'cold_sensitivity', cold_sensitivity) order by id), '[]'::jsonb)
            from app_users where active),
  'recent', (select coalesce(jsonb_agg(r order by r->>'outfit_date' desc), '[]'::jsonb) from (
              select jsonb_build_object(
                'id', h.id, 'outfit_date', h.outfit_date, 'title', h.title,
                'style', h.style, 'city', h.city, 'band', h.weather_band,
                'mode', h.generation_mode, 'score', h.rule_score,
                'rating', h.user_rating,
                'items', (select jsonb_agg(w.name order by array_position(array['base_top','mid_layer','outerwear','bottom','shoes','accessory'], w.category))
                          from wardrobe_items w where w.id = any(h.item_ids))) as r
              from outfit_history h
              where h.user_id = p_user_id and h.is_current and h.source <> 'eval_dry'
              order by h.outfit_date desc limit 7) t),
  'summary', (select to_jsonb(s) from v_eval_summary s),
  'laundry', (select coalesce(jsonb_agg(jsonb_build_object('id', id, 'name', name)), '[]'::jsonb)
              from wardrobe_items where user_id = p_user_id and status = 'laundry')
);
$$;

-- Petite fonctionnalité : mettre une pièce au linge sale / la récupérer
create or replace function set_item_status(p_item_id int, p_status text)
returns jsonb
language sql
as $$
  update wardrobe_items set status = p_status where id = p_item_id
  returning jsonb_build_object('id', id, 'name', name, 'status', status);
$$;
