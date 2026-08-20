-- LLM API 가격 모니터 - 표 생성 SQL (4판, 2026-08-17)
-- 대상: MySQL 8.0.46
--
-- 4판 = 표·컬럼 이름을 사람이 읽는 이름으로 바꾸고, 실행 번호(run_id)를 없앤 판.
-- 3판(2026-08-15)에서 바뀐 것만 적는다. 저장하는 값과 축은 그대로다.
--   (1) 표 이름 4개: price_condition -> price_condition · daily_price -> daily_price ·
--       crawling_run -> crawling_run · crawling_run_provider -> crawling_run_provider
--       (provider · model · price_change 는 그대로)
--   (2) run_id 삭제. 수집 실행을 가리키는 열쇠가 날짜(run_date)가 된다.
--       daily_price 는 이미 observed_date 가 있어 칸이 통째로 없어지고,
--       price_change 는 run_date · prev_run_date 로 바뀐다.
--       ★source 는 열쇠에서 빠지고 기록용 칸으로만 남는다(하루 1실행 전제).
--       과거 소급(archive_backfill)은 2026-08-14 에 폐기했다. 되살리면 이 전제를 다시 본다
--   (3) 읽기 어렵던 컬럼 이름: value -> price · ok -> success · attempts -> tries ·
--       warns -> warnings · kind -> change_type · pct -> change_pct ·
--       is_spike -> is_big_change · ef_key/et_key -> from_key/to_key ·
--       review_required/review_reasons -> check_needed/check_reasons ·
--       models_total -> model_count · points_total/row_count -> price_count
--   (4) 축 이름은 그대로 둔다(item·unit·tier·context_label·modality·variant·cache_ttl·
--       region·multiplier·category). 값과 함께 사용자가 정한 이름이고 명세·어댑터 여섯 곳이
--       이 이름으로 쓰여 있다(수집기준_결정항목.md §item·unit 표준 목록·§context_label 값 규칙)
--
-- 실행(관리자): sudo mysql < schema.sql   (DROP 은 여기 없다 - 사용자가 별도 문장으로)

CREATE DATABASE IF NOT EXISTS api_price
  CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
USE api_price;

-- 1. 제공사 (6줄)
CREATE TABLE IF NOT EXISTS provider (
  provider_id  TINYINT UNSIGNED NOT NULL AUTO_INCREMENT,
  name         VARCHAR(40)  NOT NULL,              -- 'Anthropic'
  pricing_url  VARCHAR(500) NOT NULL DEFAULT '',   -- 파싱 원본 주소(마크다운 회사는 .md 주소)
  PRIMARY KEY (provider_id),
  UNIQUE KEY uq_provider_name (name)
) ENGINE=InnoDB;

-- 2. 모델. 가격이 아니라 모델 자체의 사실만
CREATE TABLE IF NOT EXISTS model (
  model_id     INT UNSIGNED NOT NULL AUTO_INCREMENT,
  provider_id  TINYINT UNSIGNED NOT NULL,
  name         VARCHAR(150) NOT NULL,              -- 적용 시기 문구를 뗀 이름
  note         VARCHAR(300) NOT NULL DEFAULT '',   -- 다른 칸에서 못 읽는 사실만(공지 끊김·이름 변경)
  first_seen   DATE NOT NULL,
  last_seen    DATE NOT NULL,
  PRIMARY KEY (model_id),
  UNIQUE KEY uq_model (provider_id, name),
  CONSTRAINT fk_model_provider FOREIGN KEY (provider_id)
    REFERENCES provider (provider_id)
) ENGINE=InnoDB;

-- 3. 가격 계열. "이 모델의 이 조건에서 이 항목" 하나가 한 줄.
--    값은 여기 없다. 축이 새로 나올 때만 줄이 늘어난다.
--    실물(2026-08-15 하루치 1,285줄): item 16종 · unit 19종 · tier 7종(standard/batch/flex/
--    priority/fast/peak/off_peak) · context 6종 · modality 5종 · variant 40종 · category 60종
CREATE TABLE IF NOT EXISTS price_condition (
  condition_id  INT UNSIGNED NOT NULL AUTO_INCREMENT,
  model_id      INT UNSIGNED NOT NULL,
  item          VARCHAR(30) NOT NULL,              -- input/output/cache_read/cache_write/generation/tool_call/...
                                                   --   표준 목록 = 수집기준_결정항목.md §item·unit 표준
  unit          VARCHAR(60) NOT NULL,              -- per_1M_tokens/per_image/per_second/per_1K_call/...
                                                   --   배수 줄(multiplier 채워짐)은 빈칸
  tier          VARCHAR(40) NOT NULL DEFAULT 'standard',  -- 회사가 쓴 말 그대로(standard/batch/flex/priority/fast/peak/off_peak)
  context_label VARCHAR(40) NOT NULL DEFAULT '',   -- short/long(20만 토큰 경계) · low/medium/high(Perplexity 검색 문맥). 빈칸 = 구분 없음
  modality      VARCHAR(20) NOT NULL DEFAULT '',   -- text/audio/image/video. 빈칸 = 자료형 구분 없는 공통 단가
  variant       VARCHAR(120) NOT NULL DEFAULT '',  -- 같은 모델 안의 변형. 720p/1080p/4K/1K/2K/fast/ultra 등
  cache_ttl     VARCHAR(20) NOT NULL DEFAULT '',   -- 5m/1h (캐시 보관 기간. Anthropic 캐시 쓰기)
  region        VARCHAR(20) NOT NULL DEFAULT 'global',  -- global(기본) / regional(지역 지정 할증)
  multiplier    VARCHAR(20) NOT NULL DEFAULT '',   -- 채워지면 값이 금액이 아니라 "여기 적힌 등급 단가의 배수". 예: 'standard'
  category      VARCHAR(80) NOT NULL DEFAULT '',   -- 페이지 절 이름 원문. 'Flagship models'·'Text API Pricing'·'Model pricing'·Google 은 모델 절 이름
  currency      CHAR(3) NOT NULL DEFAULT 'USD',
  effective_from DATE NULL,                        -- 이 날부터 적용(예고 단가. 그 전에는 적재 안 함)
  effective_to   DATE NULL,                        -- 이 날까지 적용(2026-08-15 부터는 채우지 않음. 원문은 note)
  -- 고유 키에 NULL이 들어가면 중복이 안 걸리므로 대체값을 쓴다
  from_key      DATE GENERATED ALWAYS AS (IFNULL(effective_from, '1000-01-01')) STORED,
  to_key        DATE GENERATED ALWAYS AS (IFNULL(effective_to,   '9999-12-31')) STORED,
  first_seen    DATE NOT NULL,
  last_seen     DATE NOT NULL,
  source_url    VARCHAR(500) NOT NULL DEFAULT '',
  PRIMARY KEY (condition_id),
  UNIQUE KEY uq_condition (model_id, item, unit, tier, context_label,
                        modality, variant, cache_ttl, region, multiplier, from_key, to_key),
  KEY ix_condition_model (model_id),
  CONSTRAINT fk_condition_model FOREIGN KEY (model_id)
    REFERENCES model (model_id)
) ENGINE=InnoDB;

-- 4. 수집 실행 1회 (하루 1줄)
CREATE TABLE IF NOT EXISTS crawling_run (
  run_date      DATE NOT NULL,                     -- 한국시간 기준 날짜. 이 표의 열쇠
  source        VARCHAR(20) NOT NULL DEFAULT 'live', -- 기록용. 하루 1실행 전제라 열쇠에 안 넣는다
  started_at    DATETIME NOT NULL,                 -- 세계표준시
  finished_at   DATETIME NULL,
  git_commit    CHAR(40) NOT NULL DEFAULT '',
  snapshot_path VARCHAR(300) NOT NULL DEFAULT '',
  model_count   SMALLINT UNSIGNED NOT NULL DEFAULT 0,   -- 실제로 넣은 줄에서 센 모델 수
  price_count   SMALLINT UNSIGNED NOT NULL DEFAULT 0,   -- 실제로 넣은 단가 건수(하루 약 1,300)
  check_needed  TINYINT(1) NOT NULL DEFAULT 0,          -- 사람 확인 필요 여부 (1 = 필요)
  check_reasons VARCHAR(500) NOT NULL DEFAULT '',
  PRIMARY KEY (run_date)
) ENGINE=InnoDB;

-- 5. 제공사별 수집 결과 (실행 1회당 6줄)
CREATE TABLE IF NOT EXISTS crawling_run_provider (
  run_date     DATE NOT NULL,
  provider_id  TINYINT UNSIGNED NOT NULL,
  success      TINYINT(1) NOT NULL,
  model_count  SMALLINT UNSIGNED NOT NULL DEFAULT 0,   -- 그 회사에서 넣은 모델 수
  price_count  SMALLINT UNSIGNED NOT NULL DEFAULT 0,   -- 그 회사에서 넣은 단가 건수
  tries        TINYINT UNSIGNED NOT NULL DEFAULT 1,
  warnings     TEXT NULL,
  error        TEXT NULL,
  PRIMARY KEY (run_date, provider_id),
  CONSTRAINT fk_crp_run FOREIGN KEY (run_date)
    REFERENCES crawling_run (run_date) ON DELETE CASCADE,
  CONSTRAINT fk_crp_provider FOREIGN KEY (provider_id)
    REFERENCES provider (provider_id)
) ENGINE=InnoDB;

-- 6. 가격 관측. 가격 계열 하나의 그날 단가.
CREATE TABLE IF NOT EXISTS daily_price (
  condition_id  INT UNSIGNED NOT NULL,             -- 어느 조건 조합(price_condition)
  observed_date DATE NOT NULL,                     -- 관측한 날. crawling_run.run_date 와 같다
  price         DECIMAL(18,10) NOT NULL,           -- 금액. multiplier 계열에서는 배수
  note          VARCHAR(300) NOT NULL DEFAULT '',  -- 그날 페이지의 원문 조각(영어 그대로. 시기·변동 문구 포함)
  PRIMARY KEY (condition_id, observed_date),       -- 같은 조건이 하루에 두 번 못 들어옴
  KEY ix_price_date (observed_date),               -- 그날 전체 조회 + 외래 키에 필요
  CONSTRAINT fk_price_run FOREIGN KEY (observed_date)
    REFERENCES crawling_run (run_date) ON DELETE CASCADE,
  CONSTRAINT fk_price_condition FOREIGN KEY (condition_id)
    REFERENCES price_condition (condition_id)
) ENGINE=InnoDB;

-- 7. 변동 기록. 관측에서 계산할 수 있지만 매일 이미 계산하는 값이라 같이 둔다.
CREATE TABLE IF NOT EXISTS price_change (
  change_id      BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_date       DATE NOT NULL,                    -- 이번 수집
  prev_run_date  DATE NULL,                        -- 비교 대상 수집(회사마다 다를 수 있다)
  condition_id   INT UNSIGNED NOT NULL,
  change_type    VARCHAR(10) NOT NULL,             -- changed / added / removed
  old_price      DECIMAL(18,10) NULL,
  new_price      DECIMAL(18,10) NULL,
  change_pct     DECIMAL(12,4) NULL,               -- 변동률(%)
  is_big_change  TINYINT(1) NOT NULL DEFAULT 0,    -- 2배 이상 움직임
  PRIMARY KEY (change_id),
  UNIQUE KEY uq_change (run_date, condition_id),
  KEY ix_change_condition (condition_id, run_date),
  CONSTRAINT fk_chg_run FOREIGN KEY (run_date)
    REFERENCES crawling_run (run_date) ON DELETE CASCADE,
  CONSTRAINT fk_chg_condition FOREIGN KEY (condition_id)
    REFERENCES price_condition (condition_id)
) ENGINE=InnoDB;
