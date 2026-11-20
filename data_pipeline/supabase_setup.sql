-- Supabase SQL Editor 에서 실행: 읽기 전용 RLS 정책과 조회용 인덱스
-- 쓰기 정책은 두지 않음 (적재는 파이프라인에서만)

-- 1) 읽기 전용 정책
alter table prices  enable row level security;
alter table stocks  enable row level security;
alter table signals enable row level security;

drop policy if exists read_prices  on prices;
drop policy if exists read_stocks  on stocks;
drop policy if exists read_signals on signals;

create policy read_prices  on prices  for select using (true);
create policy read_stocks  on stocks  for select using (true);
create policy read_signals on signals for select using (true);

-- 2) 조회용 인덱스
create index if not exists ix_signals_lookup on signals (stock_code, baseline_date, eval_date);
create index if not exists ix_prices_stock   on prices  (stock_code, bas_dd);

-- 3) 정책 확인
select tablename, policyname from pg_policies
where schemaname = 'public' order by tablename, policyname;
