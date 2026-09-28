# 먼저 읽으십시오

RAG 대응안 생성 파트의 브랜치 반영용 패키지입니다.

## 받는 사람에게 전달할 것

브랜치에 올린 뒤, 설치하는 사람에게는 **`docs/시작하기.md` 한 장만**
가리키면 됩니다. 한 페이지짜리이고 아래 순서로 되어 있습니다.

```
1. set_env.sh 작성      DB 접속 정보
2. ./setup_linux.sh     Python 패키지·Ollama·gemma2:2b·pgvector 자동 설치
3. setup_knowledge_base.py   지식베이스 적재 (여기까지가 필수)
4. (선택) 벡터 검색·LLM
5. 실행
```

`setup_linux.sh` 는 확인 후 빠진 것만 설치하며 여러 번 실행해도
안전합니다. `--check` 로 먼저 상태만 볼 수 있습니다.

`설치가이드_리눅스.md` 는 스크립트가 막혔을 때 찾아보는 참조용입니다.
처음부터 읽힐 문서가 아닙니다.

## 요약

모든 파일이 완성본입니다. 저장소의 같은 위치에 복사하면 끝납니다.

`pipeline/patrol_pipeline_rag.py` 도 들어 있습니다. 전달받은 저장소
파일과 대조한 결과 TTS 송신·대응안 스로틀·억제 간격이 들어 있지 않아,
덮어써도 잃을 것이 없음을 확인했습니다. 그래도 복사 전에 `diff` 로 한 번
보십시오(병합가이드 4절).

손으로 고칠 것은 둘뿐입니다.

- `set_env.example.sh` — 기존 DB 설정이 지워지지 않게 블록만 덧붙임 (3절)
- 지식베이스 DB 를 나눠 쓸 때만 `setup_knowledge_base.py` ·
  `embed_chunks.py` 를 한 줄씩 (5절)

## 폴더 구조

저장소 구조와 같습니다. ZIP 을 저장소에 올리지 말고, 아래 파일을 각자의
위치에 반영하십시오.

```
setup_linux.sh                 신규 (설치 도우미 스크립트)
pipeline/
  patrol_pipeline_rag.py       수정 (UDP 환경변수, --use-llm, --async-llm)
integration/
  response_agent.py            수정 (LLM_TIMEOUT, 지식베이스 DB 분리)
  response_plan_worker.py      신규 (비동기 대응안 워커)
rag/
  kb_config.py                 신규 (지식베이스 DB 접속 설정)
  check_pgvector.py            수정 (문서 참조, 대상 DB 표시)
  check_llm.py                 수정 (문서 참조, LLM_TIMEOUT)
  requirements.txt             수정 (주석)
  README.md                    수정 (비동기 절 추가)
self_test/
  test_response_plan_worker.py 신규 (DB·LLM 없이 실행되는 시험)
  test_response_agent_llm.py   신규 (LLM 응답 방어 시험)
docs/
  시작하기.md                    신규 (받는 사람용 한 장짜리)
  apply_merge.py                신규 (파이프라인 변경 자동 적용 스크립트)
  변경요약.md                   재작성
  설치가이드_리눅스.md           갱신 (빠른시작, DB 분리, UDP, 비동기)
  병합가이드.md                  신규 (브랜치 절차 + 파이프라인 패치)
  set_env_llm_block.sh          신규 (set_env.example.sh 에 덧붙일 블록)
```

저장소의 `patrol_pipeline_rag.py` 가 그동안 바뀌어 복사할 수 없다면,
`docs/apply_merge.py` 가 **추가만 하는** 방식으로 반영합니다. 기존 코드를
지우지 않으므로 나중에 들어간 기능이 사라지지 않습니다.

```bash
python3 docs/apply_merge.py --repo <저장소>/rag_upload --dry-run
```

저장소에 이미 있는 `rag/setup_knowledge_base.py` 와 `rag/embed_chunks.py`
는 DB 를 나눠 쓸 때만 한 줄씩 손보면 됩니다(병합가이드 5절).

## 순서

1. `docs/병합가이드.md` — 브랜치 생성부터 커밋 전 확인까지
2. `docs/변경요약.md` — 무엇이 왜 바뀌었는지
3. `docs/설치가이드_리눅스.md` — 각 PC 에서 무엇을 설치하는지

## 브랜치에 올리지 않는 것

`set_env.sh`(DB 비밀번호), Ollama 설치 파일, `gemma2:2b` 모델, BGE-M3 캐시,
그리고 이 ZIP 자체입니다. 소스 파일로 커밋해야 이력이 남습니다.
