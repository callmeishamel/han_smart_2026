# smart_factory_project (과거 Jetson zip 뼈대 — 사용 중단)

> **새 배포에는 이 폴더를 사용하지 마세요.** 최신 Autopilot Jetson 배포 정본은 저장소
> 루트의 [`../../../smart_factory_project/`](../../../smart_factory_project/)입니다.
> 최신 폴더에는 비전·브리지·TTS 코드와 환경변수 템플릿이 이미 정리되어 있습니다.

과거 Jetson 실물 장비에 zip으로 전달하기 위해 사용하던 **호환용 뼈대**입니다. 여기 있는
파일 자체가 실행되는 원본 코드는 아니고, Jetson으로 넘길 때 필요한 실행
스크립트/설정 템플릿만 모아둔 것입니다. 실제 실행 안내는
[`../../손준영팀장님_실행안내(new).md`](../../손준영팀장님_실행안내(new).md)에
있으니 이 문서는 "이 폴더가 지금 무슨 상태인지"를 정리하는 용도로 읽어주세요.

## 현재 들어있는 파일

```
smart_factory_project/
├── run_pipeline.sh          # set_env.sh를 로드하고 pipeline/jetson_patrol_pipeline.py --zone A 실행
└── set_env.example.sh       # DB 접속 정보(SFP_DB_*) 템플릿. 복사해서 set_env.sh로 채워 씀
```

`run_pipeline.sh`는 `pipeline/jetson_patrol_pipeline.py`를 호출하는데, **이 폴더에는
`pipeline/`도 `common/`도 들어있지 않습니다.** 원래는 있었지만
["데이터 플랫폼 및 대시보드(이상민)/"](../../../데이터%20플랫폼%20및%20대시보드(이상민)/)
폴더의 원본과 완전히 동일한 중복 파일이라 저장소 정리 과정에서 제거되었습니다.

## 과거 배포(zip 전달) 준비 단계

아래 내용은 이전 배포 절차를 이해해야 할 때만 참고하세요. 새 배포에서는 수동 복사로
이 폴더를 채우지 말고 루트 `smart_factory_project/`를 그대로 사용합니다.

```bash
cp -r "데이터 플랫폼 및 대시보드(이상민)/common" "젯슨 연결용 프로그램(손준영)/jetson용_smart_factory_project_1/smart_factory_project/"
cp -r "데이터 플랫폼 및 대시보드(이상민)/pipeline" "젯슨 연결용 프로그램(손준영)/jetson용_smart_factory_project_1/smart_factory_project/"
cp "젯슨 연결용 프로그램(손준영)/ai_inference_sender.py" \
   "젯슨 연결용 프로그램(손준영)/stereo_calibrate.py" \
   "젯슨 연결용 프로그램(손준영)/vision_inference_node.py" \
   "젯슨 연결용 프로그램(손준영)/requirements.txt" \
   "젯슨 연결용 프로그램(손준영)/requirements-vision.txt" \
   "젯슨 연결용 프로그램(손준영)/jetson용_smart_factory_project_1/smart_factory_project/"
```

RAG 대응안 파이프라인을 사용할 때만 아래 두 폴더도 추가합니다. 기본
`run_pipeline.sh`에는 필요하지 않습니다.

```bash
cp -r "데이터 플랫폼 및 대시보드(이상민)/integration" "젯슨 연결용 프로그램(손준영)/jetson용_smart_factory_project_1/smart_factory_project/"
cp -r "데이터 플랫폼 및 대시보드(이상민)/rag" "젯슨 연결용 프로그램(손준영)/jetson용_smart_factory_project_1/smart_factory_project/"
```

현장 음성 경보(`--tts`)까지 쓸 계획이라면 TTS 수신기도 함께 넣어야 합니다.
Jetson에서 도는 쪽은 `Rx_pipeline.py` 하나입니다 (`Rag_to_Jetson.py`는 PC 쪽 코드).

```bash
mkdir -p "젯슨 연결용 프로그램(손준영)/jetson용_smart_factory_project_1/smart_factory_project/tts"
cp "TTS Engine and pipeline/Rx_pipeline.py" \
   "젯슨 연결용 프로그램(손준영)/jetson용_smart_factory_project_1/smart_factory_project/tts/"
```

Jetson 쪽에는 `piper` CLI, `alsa-utils`(aplay), 그리고 음성 모델
(`ko_KR-kss-medium.onnx`)이 따로 있어야 합니다. 모델은 용량(63MB) 때문에 저장소에
포함하지 않으니 젯슨에서 `tts/setup_tts.sh`로 받으세요 — 모델 내려받기, USB 스피커
선택, 실제 소리 재생까지 한 번에 점검합니다. 자세한 내용은
[`../../../TTS Engine and pipeline/README.md`](../../../TTS%20Engine%20and%20pipeline/README.md).

그다음 이 폴더 전체(`smart_factory_project/`)를 `smart_factory_project.zip`으로
압축해서 Jetson에 전달하면, 받는 쪽에서는
[`../../손준영팀장님_실행안내(new).md`](../../손준영팀장님_실행안내(new).md)의
1~4단계(압축 해제 → `pip3 install psycopg2-binary` → `set_env.sh` 준비 →
`./run_pipeline.sh`)만 따라가면 됩니다.

이 복사 단계는 루트 [`../../../README.md`](../../../README.md) 2.7절에도 같은
내용으로 안내되어 있습니다.

## 추론 실행 환경 주의

위 준비 명령은 `ai_inference_sender.py`와 관련 파일도 zip에 포함하므로 실행 안내의
5단계를 같은 폴더에서 그대로 수행할 수 있습니다. 다만 NanoOWL과 torch는 일반 pip
패키지가 아니라 Jetson L4T에 맞는 NVIDIA 이미지/빌드가 필요합니다. 파일이 포함되는
것과 추론 런타임이 준비되는 것은 별개이므로, 기존 NanoOWL 컨테이너 안에서 이 폴더를
마운트해 실행하거나 해당 런타임을 먼저 준비해야 합니다.

스테레오 보정 파일 `stereo_calibration.npz`는 장비별 산출물이므로 저장소에 포함하지
않습니다. 실제 카메라로 `stereo_calibrate.py`를 실행해 같은 폴더에 생성한 후 추론을
시작하세요. 파일이 없으면 보정 없이 실행되어 거리 정확도가 낮아질 수 있습니다.

## 관련 문서

- [`../../README.md`](../../README.md) — 손준영 파트 전체 개요 및 코드 설명.
- [`../../손준영팀장님_실행안내(new).md`](../../손준영팀장님_실행안내(new).md) — 이 폴더를 Jetson에서 실행하는 단계별 안내.
- [`../../../README.md`](../../../README.md) — 전체 파이프라인 통합 실행 순서 (2.7절 참고).
