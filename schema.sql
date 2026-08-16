-- LLM API 가격 모니터 - 표 생성 SQL (3판, 2026-08-15)
-- 대상: MySQL 8.0.46
--
-- 3판 = 2026-08-14 "수집 기준 다시 세우기" 확정에 따라 DB 를 갈아엎고 새로 만든다
-- (수집기준_결정항목.md 개요 절 · 검증 반영 보강 절 · 전환 기조 절).
--   계승: DB 이름 api_price · 계정 4개 · 접속 설정 파일 · 살아남는 컬럼 이름 · 뷰 이름 4개
--   새로: 스키마(이 파일) · 데이터(빈 채로 시작, 소급 없음) · 파이프라인 코드
-- 2판(2026-07-27)에서 바뀐 것
--   (1) price_series.multiplier 신설 - 회사가 금액을 안 적고 "표준의 몇 배"로만 공지하는
--       요금(xAI Batch 20% off · Priority 2x 등)을 계산해 넣지 않고 배수 그대로 둔다.
--       채워진 줄에서는 price_point.value 가 금액이 아니라 배수(0.8 · 2.0)다. 고유 키에 넣는다
--   (2) price_series.category 신설 - 페이지 절 이름 원문(Flagship models 등). 알림 필터의
--       근거. 고유 키에는 안 넣는다(gpt-5.5-cyber 가 여드레 사이 절을 옮긴 실측 - 옮겨도
--       같은 계열로 두고 마지막 본 절로 갱신한다)
--   (3) variant 폭 60 -> 120 (Google 영상 변형 표기가 길다. 실측 최대 64)
--   (4) region 기본값 '' -> 'global' (지역 할증은 자리 모델 '(all models)' 의 배수 줄로 들어오고
--       region='regional'. 값은 전부 영어)
--   (5) context_min_tokens · context_max_tokens 삭제 - 채움 규칙이 없던 죽은 칸. 구간은
--       context_label(short/long/low/medium/high) 이 담고 원문은 price_point.note 에 남는다
--   (6) effective_to 는 남기되 새 어댑터는 안 채운다(변동·시기 원문은 note 에 영어 그대로,
--       2026-08-15 사용자). effective_from 은 예고 단가 판정(db.applies_on)에 쓴다
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
CREATE TABLE IF NOT EXISTS price_series (
  series_id     INT UNSIGNED NOT NULL AUTO_INCREMENT,
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
  ef_key        DATE GENERATED ALWAYS AS (IFNULL(effective_from, '1000-01-01')) STORED,
  et_key        DATE GENERATED ALWAYS AS (IFNULL(effective_to,   '9999-12-31')) STORED,
  first_seen    DATE NOT NULL,
  last_seen     DATE NOT NULL,
  source_url    VARCHAR(500) NOT NULL DEFAULT '',
  PRIMARY KEY (series_id),
  UNIQUE KEY uq_series (model_id, item, unit, tier, context_label,
                        modality, variant, cache_ttl, region, multiplier, ef_key, et_key),
  KEY ix_series_model (model_id),
  CONSTRAINT fk_series_model FOREIGN KEY (model_id)
    REFERENCES model (model_id)
) ENGINE=InnoDB;

-- 4. 수집 실행 1회 (하루 1줄)
CREATE TABLE IF NOT EXISTS collection_run (
  run_id        INT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_date      DATE NOT NULL,                     -- 한국시간 기준 날짜 라벨
  source        VARCHAR(20) NOT NULL DEFAULT 'live', -- live / archive_backfill
  started_at    DATETIME NOT NULL,                 -- 세계표준시
  finished_at   DATETIME NULL,
  git_commit    CHAR(40) NOT NULL DEFAULT '',
  snapshot_path VARCHAR(300) NOT NULL DEFAULT '',
  models_total  SMALLINT UNSIGNED NOT NULL DEFAULT 0,   -- 실제로 넣은 줄에서 센 모델 수
  points_total  SMALLINT UNSIGNED NOT NULL DEFAULT 0,   -- 실제로 넣은 관측 줄 수(하루 약 1,300)
  review_required TINYINT(1) NOT NULL DEFAULT 0,
  review_reasons  VARCHAR(500) NOT NULL DEFAULT '',
  PRIMARY KEY (run_id),
  UNIQUE KEY uq_run (run_date, source)
) ENGINE=InnoDB;

-- 5. 제공사별 수집 결과 (실행 1회당 6줄)
CREATE TABLE IF NOT EXISTS provider_run (
  run_id       INT UNSIGNED NOT NULL,
  provider_id  TINYINT UNSIGNED NOT NULL,
  ok           TINYINT(1) NOT NULL,
  model_count  SMALLINT UNSIGNED NOT NULL DEFAULT 0,   -- 그 회사에서 넣은 모델 수
  row_count    SMALLINT UNSIGNED NOT NULL DEFAULT 0,   -- 그 회사에서 넣은 관측 줄 수 (3판 신설)
  attempts     TINYINT UNSIGNED NOT NULL DEFAULT 1,
  warns        TEXT NULL,
  error        TEXT NULL,
  PRIMARY KEY (run_id, provider_id),
  CONSTRAINT fk_prun_run FOREIGN KEY (run_id)
    REFERENCES collection_run (run_id) ON DELETE CASCADE,
  CONSTRAINT fk_prun_provider FOREIGN KEY (provider_id)
    REFERENCES provider (provider_id)
) ENGINE=InnoDB;

-- 6. 가격 관측. 계열 하나가 그날 얼마였는가.
CREATE TABLE IF NOT EXISTS price_point (
  series_id     INT UNSIGNED NOT NULL,
  run_id        INT UNSIGNED NOT NULL,
  observed_date DATE NOT NULL,
  value         DECIMAL(18,10) NOT NULL,           -- 금액. multiplier 계열에서는 배수
  note          VARCHAR(300) NOT NULL DEFAULT '',  -- 그날 페이지의 원문 조각(영어 그대로. 시기·변동 문구 포함)
  PRIMARY KEY (series_id, run_id),                 -- 같은 계열이 한 실행에 두 번 못 들어옴
  KEY ix_point_run (run_id),                       -- 그날 전체 조회 + 외래 키에 필요
  CONSTRAINT fk_point_run FOREIGN KEY (run_id)
    REFERENCES collection_run (run_id) ON DELETE CASCADE,
  CONSTRAINT fk_point_series FOREIGN KEY (series_id)
    REFERENCES price_series (series_id)
) ENGINE=InnoDB;

-- 7. 변동 기록. 관측에서 계산할 수 있지만 매일 이미 계산하는 값이라 같이 둔다.
CREATE TABLE IF NOT EXISTS price_change (
  change_id     BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  run_id        INT UNSIGNED NOT NULL,             -- 이번 수집
  prev_run_id   INT UNSIGNED NULL,                 -- 비교 대상 수집
  series_id     INT UNSIGNED NOT NULL,
  kind          VARCHAR(10) NOT NULL,              -- changed / added / removed
  old_value     DECIMAL(18,10) NULL,
  new_value     DECIMAL(18,10) NULL,
  pct           DECIMAL(12,4) NULL,                -- 변동률(%)
  is_spike      TINYINT(1) NOT NULL DEFAULT 0,     -- 변동 폭이 큼(2배 이상)
  PRIMARY KEY (change_id),
  UNIQUE KEY uq_change (run_id, series_id),
  KEY ix_change_series (series_id, run_id),
  CONSTRAINT fk_chg_run FOREIGN KEY (run_id)
    REFERENCES collection_run (run_id) ON DELETE CASCADE,
  CONSTRAINT fk_chg_series FOREIGN KEY (series_id)
    REFERENCES price_series (series_id)
) ENGINE=InnoDB;
