-- =============================================================================
-- 파일명   : sql/01_knowledge_base.sql
-- 목적     : 대응안 생성에 사용하는 산업안전 지식베이스 테이블 생성
-- 실행     : psql -U postgres -d <DB명> -f sql/01_knowledge_base.sql
--            (install.py 실행 시 자동으로 처리됨)
-- 선행조건 : PostgreSQL 13 이상
--
-- 개요
--   기존 테이블(patrol_zones, patrol_logs, response_plans, detection_events)에
--   영향을 주지 않고 지식베이스 테이블 두 개를 추가한다.
--   대응안 생성 에이전트는 이 두 테이블에 SELECT만 수행하며,
--   생성 결과 저장은 기존 경로(오케스트레이터 -> response_plans)를 사용한다.
--
--   pgvector 확장이 없는 환경에서는 embedding 컬럼 없이 생성된다.
--   이 경우 위험유형 기준 조회로 동작하며 통합에는 지장이 없다.
-- =============================================================================


-- -----------------------------------------------------------------------------
-- 1. 벡터 확장 (선택)
--
--    설치되어 있지 않으면 이 구문에서 오류가 발생하나, 이후 구문은
--    정상 실행된다. install.py는 이 경우를 감지해 embedding 컬럼을
--    제외하고 테이블을 생성한다.
-- -----------------------------------------------------------------------------
CREATE EXTENSION IF NOT EXISTS vector;


-- -----------------------------------------------------------------------------
-- 2. safety_documents — 문서 원문
--
--    생성된 대응안이 어느 문서에서 비롯되었는지 역추적하기 위한 테이블이다.
--
--    컬럼
--      doc_id      문서 식별자
--      title       문서 제목. 대응안의 참조 문서 표기에 사용된다
--      source      배포 출처
--      doc_type    문서 유형 (법령 / 지침 / 절차서)
--      full_text   원문 전체. 재청킹 시에만 필요하며 비어 있어도 무방하다
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS safety_documents (
    doc_id      SERIAL          PRIMARY KEY,
    title       VARCHAR(200)    NOT NULL,
    source      VARCHAR(300),
    doc_type    VARCHAR(50),
    full_text   TEXT,
    created_at  TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE  safety_documents        IS '산업안전 매뉴얼 원문 및 출처';
COMMENT ON COLUMN safety_documents.title  IS '대응안의 참조 문서 표기에 사용';


-- -----------------------------------------------------------------------------
-- 3. safety_chunks — 절차 단위 청크
--
--    문서를 절차·항목 단위로 분할한 결과를 보관한다.
--    고정 글자 수로 분할하면 절차 중간에서 맥락이 끊겨 검색 품질이 떨어지므로
--    조항 또는 절차 단위를 유지한다.
--
--    컬럼
--      chunk_id         청크 식별자
--      doc_id           원문 문서 참조
--      risk_type        위험유형. 에이전트의 RISK_TYPE_MAP 값과 일치해야 한다
--                       허용 값 : PPE, fire, chemical, machinery,
--                                 access, loto, general
--      section_label    조항 번호 또는 절차 라벨
--      content_snippet  청크 본문. 대응안의 근거 문장으로 인용된다
--      embedding        임베딩 벡터 (pgvector 사용 시)
--
--    주의
--      embedding 차원은 임베딩 모델 출력 차원과 일치해야 한다.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS safety_chunks (
    chunk_id        SERIAL          PRIMARY KEY,
    doc_id          INTEGER         REFERENCES safety_documents(doc_id),
    risk_type       VARCHAR(30)     NOT NULL,
    section_label   VARCHAR(100),
    content_snippet TEXT            NOT NULL,
    token_count     INTEGER,
    embedding       vector(1024),
    created_at      TIMESTAMP       DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE  safety_chunks               IS '절차 단위 청크 및 임베딩';
COMMENT ON COLUMN safety_chunks.risk_type     IS '위험유형. 감지 객체명에서 매핑됨';
COMMENT ON COLUMN safety_chunks.section_label IS '조항 번호. 참조 문서 표기에 사용';


-- -----------------------------------------------------------------------------
-- 4. 인덱스
--
--    4.1 위험유형 인덱스
--        기본 조회 경로다. 임베딩 없이도 이 인덱스만으로 동작한다.
--
--    4.2 벡터 인덱스
--        유사도 검색용. pgvector가 설치된 환경에서만 생성된다.
--        lists 값은 행 수의 제곱근 수준을 권장한다.
-- -----------------------------------------------------------------------------
CREATE INDEX IF NOT EXISTS idx_safety_chunks_risk_type
    ON safety_chunks (risk_type);

CREATE INDEX IF NOT EXISTS idx_safety_chunks_embedding
    ON safety_chunks USING ivfflat (embedding vector_cosine_ops)
    WITH (lists = 10);


-- -----------------------------------------------------------------------------
-- 5. 생성 결과 확인
-- -----------------------------------------------------------------------------
SELECT table_name
FROM   information_schema.tables
WHERE  table_schema = 'public'
  AND  table_name IN ('safety_documents', 'safety_chunks')
ORDER  BY table_name;


-- =============================================================================
-- 파일 끝
-- =============================================================================
