# maps — 지도 생성 스크립트 & 지도 파일

```
maps/
├── generate_map.py         # .world 파일에서 지도를 생성하는 스크립트
├── digital_twin_map.pgm    # digital_twin_1.world 에서 생성 (digital_twin.launch.py 용)
├── digital_twin_map.yaml
├── smart_factory_map.pgm   # smart_factory.world 에서 생성 (simulation.launch.py 용)
├── smart_factory_map.yaml
├── factory_map.pgm         # 실물 로봇 SLAM 산출물 (시뮬레이션에는 쓰지 않음)
└── factory_map.yaml
```

| 지도 | 크기 | origin | 쓰는 곳 |
|---|---|---|---|
| `digital_twin_map` | 268×188px = 13.4m × 9.4m | `(-1.700, -1.200)` | `digital_twin.launch.py` |
| `smart_factory_map` | 429×428px = 21.5m × 21.4m | `(-10.700, -10.700)` | `simulation.launch.py` |
| `factory_map` | 100×106px = 5.0m × 5.3m | `(-0.659, -1.410)` | 실물 로봇용 (아래 참고) |

해상도는 모두 `0.05` m/px입니다.

---

## generate_map.py — 월드 파일에서 지도를 직접 생성

**핵심: 지도를 손으로 그리지 않습니다.** `.world`(SDF) 파일을 파싱해서 그 안의
collision 도형을 그대로 래스터화합니다. 그래서 지도와 Gazebo 월드가 어긋날 수
없습니다.

```bash
python3 generate_map.py            # 두 지도 모두 생성
python3 generate_map.py --list     # 무엇을 생성하는지만 확인
```

**의존성이 없습니다.** 예전에는 Pillow(PIL)가 필요했지만, PGM(P5)은 헤더 + 바이트
배열이라 표준 라이브러리만으로 직접 씁니다.

### 동작

1. **SDF 파싱** — `<model>` → `<link>` → `<collision>` 3단계의 `<pose>`를 합성합니다.
   월드마다 pose가 모델에 붙기도 하고 링크에 붙기도 해서 둘 다 처리해야 합니다.
2. **높이 필터** — 라이다 평면(`--lidar-z`, 기본 `0.18m`)을 지나는 도형만 넣습니다.
   그 높이를 지나지 않는 물체는 실제 스캔에도 안 잡히므로 지도에 있으면 안 됩니다.
3. **래스터화** — 회전한 박스는 네 모서리를 직접 돌려서 정확한 범위를 잡고,
   원기둥은 거리 판정으로 채웁니다.
4. **미탐사 판정** — 로봇 시작점(`--seed`)에서 flood fill 해서, **도달할 수 없는
   자유칸은 미탐사(205)로 바꿉니다.** 벽 바깥 공간이 여기 해당하며, 실제 SLAM
   지도와 같은 모양이 됩니다.
5. **저장** — `<이름>.pgm` + `<이름>.yaml`. `origin`은 자동 계산된 실제 범위에
   맞춰 기록되므로 좌표계가 항상 맞습니다.

### 생성 대상 추가하기

스크립트 상단 `WORLDS` 목록에 항목을 넣거나, 인자로 직접 지정합니다.

```bash
python3 generate_map.py --world ../worlds/my_world.world \
                        --name my_map --seed 0.5,3.5
```

| 인자 | 기본값 | 의미 |
|---|---|---|
| `--world` | — | 월드 파일 경로 (생략하면 `WORLDS` 전부) |
| `--name` | — | 출력 파일 이름(확장자 제외) |
| `--seed` | `0,0` | 로봇 시작 좌표 `x,y`. **launch의 `spawn_entity` 좌표와 같아야** 미탐사 판정이 맞습니다 |
| `--res` | `0.05` | 해상도 m/px |
| `--margin` | `0.6` | 바깥 벽 너머로 남길 여백(m) |
| `--lidar-z` | `0.18` | 라이다 높이(m) |
| `--out` | 이 폴더 | 출력 폴더 |

### 출력값 규약

`map_server`가 읽는 값과 같습니다.

| 값 | 의미 |
|---|---|
| `254` | 자유공간 |
| `0` | 장애물 |
| `205` | 미탐사 (시작점에서 도달 불가) |

YAML은 `mode: trinary`, `occupied_thresh: 0.65`, `free_thresh: 0.196`으로 씁니다.
`free_thresh: 0.25`이면 미탐사 픽셀 205(점유도 약 0.196)가 자유 공간으로
재해석되어 맵 전체 직사각형이 순찰 영역으로 잡힐 수 있습니다.

---

## factory_map — 실물 로봇 SLAM 산출물

커밋되어 있는 `factory_map.pgm`/`.yaml`은 **시뮬레이션용이 아닙니다.**
`mode: trinary`와 소수점 `origin`은 `map_saver_cli`의 서명이고, 내용을 열어보면
실제로 이렇습니다.

```
100 x 106 px = 5.0m x 5.3m
미탐사(205)  8,799셀  83.0%
자유(254)    1,711셀  16.1%   → 자유공간 4.3m²
장애물(0)       90셀   0.85%  → 벽 정보 0.2m²
```

**벽 정보가 90셀뿐인, 중단된 SLAM 세션의 잔해입니다.** 이 지도로는 AMCL이
likelihood field를 만들 근거가 없어 수렴하지 않습니다. 예전에는 이 파일이
`simulation.launch.py`의 Nav2에 전달되고 있었고, 20×20m 월드와 정합될 수 없어
자율주행이 전혀 동작하지 않는 원인이었습니다.

`generate_map.py`는 이 파일을 **건드리지 않습니다.** 실물 로봇 기록으로 남겨두고,
새 지도는 다른 이름으로 만듭니다.

실물 공장에서 새 지도를 만들려면:

```bash
ros2 launch smart_factory_sim real_cartographer.launch.py
# 로봇을 몰아서 지도를 그린 뒤
ros2 run nav2_map_server map_saver_cli -f factory_map --free 0.196
```

---

## clean_map.py — 의자 다리 같은 "치우면 없어질 물건" 지우기

2D LiDAR 는 바닥에서 약 0.18m **한 높이만** 봅니다. 하필 그 높이에 의자 다리 ·
책상 다리 · 파렛트 모서리가 걸립니다. 매핑 중에 서 있던 가구는 지도에 점유 셀로
박히고, **나중에 치워도 지도에는 그대로 남습니다.**

저장된 지도를 세 곳이 함께 믿기 때문에 이게 골치아픕니다.

| 어디 | 증상 |
|---|---|
| Nav2 전역 코스트맵(static layer) | 유령 주변이 부풀려져 경로가 막힘 |
| AMCL | 없는 물체를 기준으로 정합하려 해서 위치가 흔들림 |
| `patrol_planner` | 여유 공간이 줄어 순찰 지점이 밀리거나 구역이 빠짐 |

특히 **Nav2 는 static layer 를 레이트레이싱으로 지우지 않습니다.** 로봇이 그
자리를 지나가며 빈 공간을 봐도 전역 코스트맵의 유령은 남습니다.

### 왜 SLAM 중에 저절로 안 지워지나

Cartographer 의 서브맵은 `num_range_data`(기본 90 스캔)를 채우면 **동결됩니다.**
의자가 서브맵 N 에 박히면, 나중에 그 자리를 다시 지나가며 빈 공간을 관측해도
그건 새 서브맵에 들어갈 뿐 옛 서브맵은 바뀌지 않습니다.

### 판단 기준 — 모르는 것은 지우지 않는다

점유 셀 덩어리를 연결 성분으로 묶고 **세 가지를 모두** 만족할 때만 지웁니다.

1. **작다** — `--max-cells`(기본 12) 이하
2. **짧다** — `--max-extent`(기본 0.20m) 이하
3. **떠 있다** — 덩어리 둘레가 **전부** 자유공간

3번이 핵심입니다. 둘레에 미탐사가 조금이라도 닿아 있으면 그 물체 뒤를 본 적이
없다는 뜻이고, 그러면 가구인지 벽의 일부인지 판단할 근거가 없습니다.

| 상황 | 결과 |
|---|---|
| 통로 한가운데 의자 | 로봇이 주위를 돌았으므로 둘레가 전부 자유공간 → **지움** |
| 벽에 붙은 의자 | 둘레에 벽이 섞임 → 남김 (옆이 벽이라 지워도 이득이 없음) |
| 기둥 · 파렛트(0.3m 이상) | 크기 초과 → 남김 |
| 안쪽 칸막이벽 | 얇아도 길다 → 남김 |

### 쓰는 법

```bash
# 무엇이 지워질지 먼저 본다 (파일을 쓰지 않습니다)
python3 clean_map.py factory_map --dry-run

# 정리본을 만든다. 원본은 절대 덮어쓰지 않습니다.
python3 clean_map.py factory_map
```

`factory_map_clean.pgm/.yaml` 과 함께 **검토용 그림**
`factory_map_clean_review.png` 이 나옵니다. 지운 자리가 빨갛게 칠해져 있으니
**눈으로 확인한 뒤에** 쓰세요.

```bash
ros2 launch smart_factory_sim real_navigation.launch.py     map:=$HOME/factory_map_clean.yaml
```

> **가장 싸고 확실한 예방은 매핑 전에 바닥을 치우는 것입니다.** 이 도구는
> 그러지 못했을 때의 사후 수습이고, Cartographer 쪽 예방 설정은
> `config/real_turtlebot3_lds_2d.lua` 아래쪽 주석을 참고하세요.

검증: `데이터 플랫폼 및 대시보드(이상민)/self_test/test_map_cleaner.py`

---

## 지도를 바꾼 뒤에는 반드시 다시 빌드하세요

`setup.py`가 `package_files('maps')`로 이 폴더를 `share/smart_factory_sim/maps`에
설치합니다. launch 파일과 `web_dashboard.py`는 `get_package_share_directory`로
**설치된 복사본**을 먼저 찾으므로, 소스 폴더만 바꿔서는 반영되지 않습니다.

```bash
python3 maps/generate_map.py
colcon build --packages-select smart_factory_sim
source install/setup.bash
```

`__pycache__`와 `.pyc`는 설치본에 포함되지 않습니다(`setup.py`에서 제외).

---

## web_dashboard 와의 관계

`smart_factory_sim/web_dashboard.py`의 `/map_image`, `/map_info` 엔드포인트도 이
폴더를 읽습니다. 어떤 지도를 볼지는 `map_name` 파라미터로 정하고, launch 파일이
Nav2에 넘긴 지도와 같은 값을 넣어줍니다.

- `digital_twin.launch.py` → `map_name=digital_twin_map`
- `simulation.launch.py` → `map_name=smart_factory_map`

예전에는 `factory_map`이 하드코딩되어 있어, Nav2는 A 지도로 주행하는데 대시보드는
B 지도를 그리는 상태였습니다. 또 파일을 못 찾으면 실제와 무관한
`origin=[-6.0, -3.0]`을 조용히 반환했는데, 지금은 응답에 `"fallback": true`가
함께 들어와 구별할 수 있습니다.
