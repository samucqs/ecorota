create extension if not exists pgcrypto;
create table if not exists public.profiles (
 id uuid primary key references auth.users(id) on delete cascade,
 display_name text not null default 'Usuário EcoRota',
 role text not null default 'resident' check(role in ('resident','tourist','business','recycler','admin')),
 municipality text, created_at timestamptz not null default now()
);
create table if not exists public.collection_points (
 id uuid primary key default gen_random_uuid(), name text not null, materials text not null,
 municipality text not null, address text, latitude double precision not null check(latitude between -90 and 90),
 longitude double precision not null check(longitude between -180 and 180),
 verified boolean not null default false, created_by uuid references auth.users(id) on delete set null,
 created_at timestamptz not null default now()
);
create table if not exists public.recycling_offers (
 id uuid primary key default gen_random_uuid(), material text not null, quantity text not null,
 municipality text not null, contact text, notes text,
 status text not null default 'pending' check(status in ('pending','matched','closed','rejected')),
 created_by uuid references auth.users(id) on delete set null, created_at timestamptz not null default now()
);
create table if not exists public.eco_actions (
 id uuid primary key default gen_random_uuid(), user_id uuid not null references auth.users(id) on delete cascade,
 action_type text not null, description text not null, points_awarded integer not null default 0 check(points_awarded>=0),
 evidence_url text, status text not null default 'pending' check(status in ('pending','approved','rejected')),
 created_at timestamptz not null default now()
);
alter table public.profiles enable row level security;
alter table public.collection_points enable row level security;
alter table public.recycling_offers enable row level security;
alter table public.eco_actions enable row level security;
create policy "read own profile" on public.profiles for select to authenticated using(auth.uid()=id);
create policy "update own profile" on public.profiles for update to authenticated using(auth.uid()=id) with check(auth.uid()=id);
create policy "read own actions" on public.eco_actions for select to authenticated using(auth.uid()=user_id);
create policy "insert own actions" on public.eco_actions for insert to authenticated with check(auth.uid()=user_id);
create or replace function public.handle_new_user() returns trigger language plpgsql security definer set search_path=public as $$
begin
 insert into public.profiles(id,display_name,role) values(new.id,coalesce(new.raw_user_meta_data->>'display_name','Usuário EcoRota'),coalesce(new.raw_user_meta_data->>'role','resident'));
 return new;
end; $$;
drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created after insert on auth.users for each row execute procedure public.handle_new_user();
