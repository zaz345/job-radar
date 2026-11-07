-- job-radar schema v1
-- 单一归一张表：所有平台都落到这里，adapter 只负责映射。

CREATE TABLE IF NOT EXISTS jobs (
  id             TEXT PRIMARY KEY,   -- sha1(platform:platform_job_id)[:16]
  platform       TEXT NOT NULL,
  platform_id    TEXT NOT NULL,      -- 平台自己的 job id，去重与回溯用
  title          TEXT NOT NULL,
  company        TEXT,
  url            TEXT NOT NULL,
  description    TEXT,
  description_source TEXT,          -- full / snippet（邮件里的摘要是截断的）
  posted_at      TEXT,               -- ISO8601 UTC，未知则 NULL
  budget_type    TEXT,               -- fixed / hourly / unknown
  budget_min     REAL,
  budget_max     REAL,
  currency       TEXT DEFAULT 'USD',
  skills         TEXT,               -- JSON array
  client_country TEXT,
  proposals      INTEGER,            -- 竞争数（Upwork 有，job board 没有）
  required_connects INTEGER,
  client_spent   REAL,
  client_hires   INTEGER,
  client_rating  REAL,               -- Upwork 客户评分（邮件里带）
  payment_verified INTEGER,          -- 1/0，付款方式已验证
  raw            TEXT,               -- 原始 JSON，便于回溯
  first_seen     TEXT NOT NULL,      -- 首次入库时间（UTC ISO）
  last_seen      TEXT NOT NULL,      -- 最近一次仍被 API 返回的时间
  score          INTEGER,
  score_hits     TEXT,               -- JSON：命中的规则名
  status         TEXT DEFAULT 'new', -- new / shortlisted / skipped / applied
  notified_at    TEXT                -- 推送时间（UTC ISO）。非空=已推过，绝不再推第二次
);

CREATE INDEX IF NOT EXISTS idx_jobs_platform  ON jobs(platform);
CREATE INDEX IF NOT EXISTS idx_jobs_posted    ON jobs(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_status    ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score     ON jobs(score DESC);

-- 每次抓取的体检记录。解析崩掉时必须在这里留 error，不许静默返回空。
CREATE TABLE IF NOT EXISTS runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  platform    TEXT NOT NULL,
  fetched     INTEGER DEFAULT 0,
  inserted    INTEGER DEFAULT 0,
  updated     INTEGER DEFAULT 0,
  error       TEXT
);

-- 规则配置镜像（rules.yaml 是唯一真相，这里只是运行快照）
CREATE TABLE IF NOT EXISTS rules_snapshot (
  taken_at  TEXT NOT NULL,
  content   TEXT NOT NULL
);

-- 人工标记：用于后续校准权重
CREATE TABLE IF NOT EXISTS feedback (
  job_id     TEXT NOT NULL,
  label      TEXT NOT NULL,          -- shortlisted / skipped / applied
  note       TEXT,
  created_at TEXT NOT NULL,
  PRIMARY KEY (job_id, label, created_at)
);
