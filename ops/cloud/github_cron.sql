-- Fallback scheduler: when GitHub's own `schedule:` does not fire (2026-11-23: no scheduled run at all on this repo),
-- our Supabase project (pg_cron + pg_net) starts the two workflows through GitHub's workflow_dispatch API.
-- A dispatched run is event `workflow_dispatch`, so the jobs run whatever CLOUD_SCHEDULE_ENABLED /
-- ANALYSIS_SCHEDULE_ENABLED say. Both workflows have a concurrency group, so a run started while another is going
-- waits instead of overlapping.
--
-- Run in the investment-evidence-system Supabase project (txhxkbkzwcuujjmvhraq) → SQL Editor, in three parts.
--
-- 0) Once, outside SQL: create a GitHub fine-grained token
--    GitHub → Settings → Developer settings → Fine-grained tokens → Generate new token
--      Repository access: Only select repositories → jihaein/investment-evidence-system
--      Permissions: Actions = Read and write  (nothing else)
--      Expiration: after the project ends (e.g. 2027-01-31)
--
-- 1) Store the token in Supabase Vault (Dashboard → Project Settings → Vault → Add new secret,
--    name: github_dispatch_token). Do not paste it into this file or into chat.

-- 2) Extensions and the dispatch function
create extension if not exists pg_cron;
create extension if not exists pg_net;

create or replace function public.dispatch_github_workflow(workflow text)
returns bigint
language plpgsql
security definer
set search_path = public, extensions
as $$
declare
  token text;
  request_id bigint;
begin
  select decrypted_secret into token from vault.decrypted_secrets where name = 'github_dispatch_token';
  if token is null then
    raise exception 'vault secret github_dispatch_token is missing';
  end if;
  select net.http_post(
    url := format('https://api.github.com/repos/jihaein/investment-evidence-system/actions/workflows/%s/dispatches', workflow),
    body := jsonb_build_object('ref', 'main'),
    headers := jsonb_build_object(
      'Authorization', 'Bearer ' || token,
      'Accept', 'application/vnd.github+json',
      'X-GitHub-Api-Version', '2022-11-28',
      'User-Agent', 'ies-supabase-cron',
      'Content-Type', 'application/json'),
    timeout_milliseconds := 10000
  ) into request_id;
  return request_id;
end $$;
revoke all on function public.dispatch_github_workflow(text) from public, anon, authenticated;

-- Try it once by hand, then check GitHub → Actions for a new "Manually run" daily-refresh / analysis-worker run:
--   select public.dispatch_github_workflow('analysis-worker.yml');
--   select status_code, content::text from net._http_response order by created desc limit 1;   -- 204 = accepted

-- 3) The schedules (pg_cron runs in UTC; same times as the workflow files)
select cron.schedule('ies-daily-refresh-1930', '30 10 * * 1-5', $$select public.dispatch_github_workflow('daily-refresh.yml')$$);   -- 19:30 KST Mon-Fri
select cron.schedule('ies-daily-refresh-2230', '30 13 * * 1-5', $$select public.dispatch_github_workflow('daily-refresh.yml')$$);   -- 22:30 KST retry
select cron.schedule('ies-daily-refresh-0830', '30 23 * * 0-4', $$select public.dispatch_github_workflow('daily-refresh.yml')$$);   -- 08:30 KST Mon-Fri
select cron.schedule('ies-analysis-worker',    '7,37 * * * *',  $$select public.dispatch_github_workflow('analysis-worker.yml')$$); -- every 30 minutes

-- Check:   select jobname, schedule, active from cron.job;
--          select jobname, status, start_time from cron.job_run_details d join cron.job j using (jobid) order by start_time desc limit 10;
-- Stop:    select cron.unschedule('ies-analysis-worker');   (and the three daily ones)
-- If GitHub's own schedule starts working later, remove either these jobs or the workflows' `schedule:` block,
-- not both, so each run is started once.
