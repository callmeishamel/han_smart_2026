# Jetson Nano 실행 안내 (손준영 팀장님께)

안녕하세요, 이상민입니다. 제가 만든 데이터 파이프라인을 Jetson에서 같이 테스트해보고 싶어서
파일들을 보내드립니다. `ai_inference_sender.py`는 전혀 수정하지 않아도 됩니다.

> **현재 배포 안내**: 최신 Autopilot Jetson 배포 정본은 저장소 루트의
> `smart_factory_project/`입니다. `jetson용_smart_factory_project_1/` 뼈대에 파일을
> 다시 복사하지 말고 최신 폴더를 그대로 전달하세요. Jetson 상시 실행 파일과 PC 전용
> 호환 파일 구분은 [`../smart_factory_project/README.md`](../smart_factory_project/README.md)에
> 정리되어 있습니다. 아래의 DB 파이프라인 실행 단계는 Jetson 한 대에서 DB 적재까지
> 수행하는 구버전/단독 시연이 필요할 때만 사용합니다.

## 1. 압축 풀기
전달받은 `smart_factory_project.zip`을 Jetson의 홈 디렉토리에 풀어주세요.

```bash
cd ~
unzip smart_factory_project.zip
cd smart_factory_project
```

## 2. 필요한 패키지 설치
```bash
pip3 install psycopg2-binary
```

## 3. 접속 정보 준비 (최초 1회만)

```bash
cp set_env.example.sh set_env.sh
nano set_env.sh
```

열리면 아래 값들을 실제 값으로 바꿔주세요. 파일 안에 "반드시 채워야 하는 값"
구역으로 묶여 있습니다.

```bash
export SFP_DB_PASSWORD='이상민에게 받은 비밀번호'
export SFP_DB_HOST=이상민 PC의 IP주소      # 예: 203.0.113.50
export DASHBOARD_IP=이상민 PC의 IP주소     # 탐지 결과를 보낼 곳
export DAEUN_LAPTOP_IP=이다은 노트북 IP    # 미니맵 각도를 보낼 곳
```

저장하고 나가면 됩니다 (nano는 `Ctrl+O` → `Enter` → `Ctrl+X`).

> **IP를 잘못 넣어도 에러가 나지 않습니다.** UDP는 받는 쪽이 없어도 그냥
> 성공으로 처리되기 때문에, 화면에는 아무 문제 없어 보이는데 대시보드에만
> 데이터가 안 들어오는 상태가 됩니다. 실행 직후 터미널에 출력되는
> "UDP 전송 대상" 세 줄이 맞는지 꼭 확인해 주세요.

## 4. 실행 (앞으로는 이것만 하면 됩니다)

```bash
./run_pipeline.sh
```

권한 문제로 안 되면 한 번만:
```bash
chmod +x run_pipeline.sh
./run_pipeline.sh
```

아래처럼 뜨면 정상적으로 UDP 수신 대기 중인 상태입니다.
```
UDP 수신 대기 시작 (0.0.0.0:9998, 구역: A)
```
(`ai_inference_sender.py`는 감지 결과를 9999(ROS2용)와 9998(대시보드/DB용) 두 포트로
동시에 보냅니다. 이 파이프라인은 9998을 받습니다.)

## 5. 이제 다른 터미널에서 평소처럼 실행
```bash
python3 ai_inference_sender.py
```

## 6. 확인
파이프라인을 실행해둔 터미널에 아래처럼 로그가 찍히기 시작하면 정상 연동된 것입니다.
```
적재 완료 [A] 작업자 (위험도: 위험, 거리: 1.2m)
```

추론이 실제로 돌고 있는지는 브라우저나 `curl`로 확인할 수 있습니다.

```bash
curl http://localhost:8500/health
# {"ok": true, "frames": 9021, "frame_age_sec": 0.03, "camera_errors": 0, ...}
```

`ok`가 `false`이거나 `frame_age_sec`이 계속 커지면 추론이 멈춘 것입니다.
**영상 화면만으로는 이걸 알 수 없습니다** — 카메라가 빠져도 마지막 프레임이
계속 재전송되어 화면은 멀쩡해 보입니다.

## 문제가 생기면
- DB 연결 에러(`psycopg2.OperationalError` 등): 이상민 PC의 PostgreSQL이 켜져 있는지, 외부 접속이
  허용되어 있는지, 같은 와이파이(공유기)에 연결되어 있는지 확인이 필요합니다. 이상민에게 알려주세요.
- `UDP 수신 대기` 로그는 뜨는데 `적재 완료` 로그가 안 뜨면: 아래를 순서대로 보세요.
  1. `curl http://localhost:8500/health` — `ok: false`면 카메라/추론 문제입니다.
  2. `ai_inference_sender.py` 시작 로그의 **"UDP 전송 대상"** 세 줄. 대시보드 대상 IP가
     이상민 PC의 실제 IP인지 확인하세요. **틀려도 에러가 나지 않습니다.**
  3. 시작 로그에 `[주의]`로 시작하는 줄이 있는지. 컨테이너에서 루프백으로 보내고 있거나
     두 목적지가 같은 IP일 때 그 자리에서 알려줍니다.
- 카메라 케이블이 빠지면 이제 `⚠ [카메라] 프레임을 읽지 못했습니다` 로그가 2초마다
  뜹니다(예전에는 아무 로그 없이 조용했습니다).

감사합니다!
