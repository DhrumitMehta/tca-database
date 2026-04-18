-- Run this SQL in your Supabase SQL Editor to create the ball_by_ball table.

create table if not exists ball_by_ball (
  id              bigserial primary key,
  match_id        integer       not null,
  event_name      text,
  inning_number   integer,
  batting_team    text,
  bowling_team    text,
  over            integer,
  delivery        text,
  batter          text,
  bowler          text,
  total_runs      integer       default 0,
  batter_runs     integer       default 0,
  extras_runs     integer       default 0,
  extras_type     text,
  wickets         integer       default 0,
  bowler_wicket   integer       default 0,
  player_out      text,
  wicket_type     text,
  legal_delivery  integer       default 0,
  dots            integer       default 0,  -- was "0s"
  ones            integer       default 0,  -- was "1s"
  twos            integer       default 0,  -- was "2s"
  threes          integer       default 0,  -- was "3s"
  fours           integer       default 0,  -- was "4s"
  sixes           integer       default 0,  -- was "6s"
  wides           integer       default 0,
  scraped_at      timestamptz   default now()
);

-- Unique constraint used by the upsert (on_conflict)
create unique index if not exists ball_by_ball_unique_ball
  on ball_by_ball (match_id, inning_number, over, delivery, batter);

-- Useful indexes for query performance
create index if not exists idx_bbb_match_id    on ball_by_ball (match_id);
create index if not exists idx_bbb_event       on ball_by_ball (event_name);
create index if not exists idx_bbb_batter      on ball_by_ball (batter);
create index if not exists idx_bbb_bowler      on ball_by_ball (bowler);
