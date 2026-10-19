-- AI complaint validation — application schema.
-- Database: automation_philip on db01-master.amzmanager.net (writes) — NOT the replica.
--   Ab: "Don't try to use it on db02-replica.amzmanager.net. Making changes on the
--   replica server will degrade replication."
--
-- Reads of review data come from db02-replica via the read-only `analytics` account
-- (this account cannot see the amzmanager schema at all — verified: SHOW DATABASES
-- returns only automation_philip). So the app holds TWO connections and they cannot
-- be confused: read-only against the replica, write-own-only against the master.
-- That is what makes Chadia's "don't disturb any process" true by construction.
--
-- MariaDB 10.3: no CHECK enforcement, no JSON column type.

SET NAMES utf8mb4;

-- One row per classification run. Provenance matters because a menu/prompt change
-- invalidates every stored label, and the noise floor here is ~8% row-flip between
-- identical runs — so a label is only meaningful alongside the config that made it.
CREATE TABLE IF NOT EXISTS runs (
  run_id         VARCHAR(40)  NOT NULL,
  model          VARCHAR(60)  NOT NULL,
  prompt_version VARCHAR(40)  NOT NULL,
  max_tokens     INT          NOT NULL DEFAULT 800,
  menu_size      INT          NULL,
  started_at     DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  finished_at    DATETIME     NULL,
  n_rows         INT          NOT NULL DEFAULT 0,
  n_errors       INT          NOT NULL DEFAULT 0,
  note           VARCHAR(255) NULL,
  PRIMARY KEY (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- The AI's output, one row per review. Kept separate from the legacy
-- cx_reviews_complaints_rel on purpose: the parallel-tab decision means nothing is
-- written into the legacy tables until Chadia has validated the output.
CREATE TABLE IF NOT EXISTS classifications (
  review_id      BIGINT       NOT NULL,
  run_id         VARCHAR(40)  NOT NULL,
  complaint      VARCHAR(120) NULL,
  category       VARCHAR(80)  NULL,
  second_complaint VARCHAR(120) NULL,   -- the runner-up; a genuine second code is the routing signal
  needs_review   TINYINT(1)   NOT NULL DEFAULT 0,
  is_new_code    TINYINT(1)   NOT NULL DEFAULT 0,
  review_date    DATE         NULL,     -- from cx_reviews.date_publishing
  sku            VARCHAR(60)  NULL,     -- from all_products.sku via cx_reviews.product_id
  asin           VARCHAR(20)  NULL,
  parent         VARCHAR(60)  NULL,     -- business_parent_products.short_name via all_products
  product        VARCHAR(120) NULL,     -- business_products.short_name via all_products
  title          VARCHAR(255) NULL,     -- all_products.title, for searching a product family
  review_text    TEXT         NULL,
  classified_at  DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (review_id),
  KEY idx_needs_review (needs_review),
  KEY idx_complaint_date (complaint, review_date),
  KEY idx_sku_date (sku, review_date),
  KEY idx_run (run_id),
  CONSTRAINT fk_class_run FOREIGN KEY (run_id) REFERENCES runs (run_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Chadia's spot-corrections. One row per change so the correction history is
-- preserved (an update would destroy the evidence of what the AI said).
-- reviewed_by is a uid from the legacy `users` table, or NULL for a system action.
-- reviewed_by_name is the authenticated username that made the call: the app knows
-- who is signed in, and the sheet path records the file's last editor. Without it
-- every correction was stamped 1089 regardless of who actually decided.
-- A later decision SUPERSEDES an earlier one: v_ai_complaints resolves the label
-- from MAX(id) per review, so revising means inserting a new row, never updating.
CREATE TABLE IF NOT EXISTS corrections (
  id            BIGINT      NOT NULL AUTO_INCREMENT,
  review_id     BIGINT      NOT NULL,
  ai_complaint  VARCHAR(120) NULL,
  new_complaint VARCHAR(120) NULL,      -- NULL = confirmed correct, no change
  decision      ENUM('accepted','corrected','rejected') NOT NULL,
  reviewed_by   INT         NULL,
  reviewed_by_name VARCHAR(64) NULL,
  note          TEXT        NULL,
  created_at    DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  KEY idx_review (review_id),
  KEY idx_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Chadia's approve/deny queue for complaint codes the AI produced that the module
-- does not have. 26 codes, 1,177 rows at the time of writing; 12 of them are labels
-- her own team already uses and the module never had.
CREATE TABLE IF NOT EXISTS proposed_codes (
  code          VARCHAR(120) NOT NULL,
  n_rows        INT          NOT NULL DEFAULT 0,
  example_review TEXT        NULL,
  status        ENUM('pending','approved','denied') NOT NULL DEFAULT 'pending',
  decided_by    INT          NULL,
  decided_at    DATETIME     NULL,
  note          TEXT         NULL,
  PRIMARY KEY (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Deny decisions that carry a replacement code, written by the review app.
-- code_denials is the DB half of the deny mapping that cxvocab.canonical() applies
-- to FUTURE classifications (sync_corrections.write_denials_json unions this table
-- into data/denied_codes.json). Without it a deny made in the app would only
-- relabel the rows already stored.
CREATE TABLE IF NOT EXISTS code_denials (
  code          VARCHAR(120) NOT NULL,
  replacement   VARCHAR(120) NOT NULL,
  note          TEXT         NULL,
  decided_by    INT          NULL,
  decided_at    DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- The weekly random check, drawn and answered in the app. One row per sampled
-- review, persisted so a re-draw cannot swap rows under the checker mid-week.
-- your_call NULL means "not called yet".
CREATE TABLE IF NOT EXISTS weekly_samples (
  sample_week   VARCHAR(10)  NOT NULL,   -- ISO week, e.g. 2026-W42
  review_id     BIGINT       NOT NULL,
  your_call     VARCHAR(120) NULL,
  note          VARCHAR(255) NULL,
  decided_by    INT          NULL,
  decided_at    DATETIME     NULL,
  drawn_at      DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (sample_week, review_id),
  KEY idx_call (sample_week, your_call)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- Her two-month weekly accuracy check, tapering after. One row per check so the
-- taper is a decision backed by a trend rather than a feeling.
CREATE TABLE IF NOT EXISTS quality_checks (
  id             BIGINT     NOT NULL AUTO_INCREMENT,
  week_start     DATE       NOT NULL,
  sample_n       INT        NOT NULL,
  agreement_pct  DECIMAL(5,2) NULL,
  checked_by     INT        NULL,
  sample_ref     VARCHAR(120) NULL,   -- e.g. the sheet holding the sampled rows
  note           TEXT       NULL,
  created_at     DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (id),
  UNIQUE KEY uniq_week (week_start)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

-- The reporting views are NOT defined here.
-- They used to be, and this copy silently clobbered apply_views.py's richer
-- definition whenever someone re-ran apply_schema.py: v_ai_complaints lost
-- final_complaint/title and v_ai_complaints_monthly (which reads them) became an
-- invalid view. apply_views.py is the single owner of both views, and cx_refresh.sh
-- re-creates them on every run. Kept deliberately empty.
