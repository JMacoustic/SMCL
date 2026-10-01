# SMCL — Stir-Mixer Control Library

Arduino로 임펠러(교반기) 모터의 회전수(RPM)를 닫힌 루프로 제어하고, 목표 속도까지의
가속(램프)을 PC에서 지정하며, 측정값을 CSV로 기록하는 도구 모음입니다.

이 문서는 **처음 받는 분이 바로 쓸 수 있도록** 쓴 안내서입니다.
순서대로 따라가면 됩니다.

---

## 1. 구성

```
SMCL/
├─ impeller_gui/                 ← PC용 제어·기록 프로그램 (Windows, Python)
│   ├─ setup_venv.bat            ← 최초 1회 설치 (python.org Python)
│   ├─ setup_conda.bat           ← 최초 1회 설치 (Anaconda/Miniconda)
│   ├─ run_gui.bat               ← 실행 (.venv 또는 conda 환경 자동 선택)
│   ├─ run_tests.bat             ← 자체 점검(선택)
│   └─ README.md                 ← 프로그램 상세 설명(영문)
├─ src/arduino_codes/impeller/   ← Arduino 펌웨어 (impeller.ino)
├─ src/arduino_codes/encoder/    ← 엔코더 보정용 스케치 (보정할 때만)
├─ src/arduino_codes/pwm/        ← 개루프 PWM 시험용 스케치 (보통 불필요)
├─ src/, configs/                ← 카메라·로봇팔 연동 등 다른 실험용 코드 (임펠러 제어에는 불필요)
```

임펠러 제어와 기록만 필요하면 **`impeller_gui/`와 `src/arduino_codes/impeller/`** 두 폴더만 보면 됩니다.

## 2. 준비물

| 항목 | 내용 |
|---|---|
| PC | Windows 10/11, USB 포트 1개 |
| 보드 | Arduino Nano (CH340 USB 칩). 포트 목록에 `USB-SERIAL CH340`으로 표시됨 |
| 배선 | 엔코더 A → D2, 엔코더 B → D3, 모터 드라이버 PWM 입력 ← D9 |
| Python | 3.9 ~ 3.12. <https://www.python.org/downloads/> 에서 설치할 때 **"Add python.exe to PATH"** 체크 |
| (펌웨어를 새로 올릴 때만) | Arduino IDE 또는 `arduino-cli` |

보드에 펌웨어가 이미 올라가 있다면 3절은 건너뛰어도 됩니다.

## 3. 펌웨어 올리기 (필요한 경우만)

펌웨어 소스는 `src/arduino_codes/impeller/impeller.ino` 하나입니다.

**Arduino IDE**
1. `impeller.ino`를 엽니다.
2. 보드: *Arduino Nano*, 프로세서: *ATmega328P*, 포트: CH340이 잡힌 COM 포트.
3. 업로드. 업로드가 `programmer is not responding`으로 실패하면 프로세서를 *ATmega328P (Old Bootloader)*로 바꿔 다시 시도합니다.

**arduino-cli** (포트가 COM12인 경우)

```bat
arduino-cli core install arduino:avr
arduino-cli compile --fqbn arduino:avr:nano src/arduino_codes/impeller
arduino-cli upload  --fqbn arduino:avr:nano --port COM12 src/arduino_codes/impeller
```

펌웨어 안에서 알아두면 좋은 설정값 (`impeller.ino` 상단):

| 상수 | 값 | 의미 |
|---|---|---|
| `MAX_TARGET_RPM` | 700 | 받아들이는 최대 목표 RPM |
| `RPM_OVERSPEED_STOP_RPM` | 750 | 이 속도를 넘으면 스스로 정지 |
| `SERIAL_COMMAND_TIMEOUT_MS` | 2500 | 모터가 도는 동안 2.5초간 PC 명령이 없으면 안전 정지 |
| `RPM_CALIB_*` | 보정표 | 엔코더 RPM → 실제 RPM 변환. 임펠러·엔코더를 바꾸면 `encoder/` 스케치와 `calibration.py`로 다시 측정 |
| `USE_FIXED_TARGET_OUTPUT_TABLE` | `false` | `true`면 PID를 끄고 고정 출력표만 사용(보정 실험용). 평소에는 반드시 `false` |

## 4. PC 프로그램 설치 (최초 1회)

이 저장소를 PC 아무 곳에나 내려받은 뒤(ZIP 다운로드 또는 `git clone`), 아래 둘 중 **하나**를 고릅니다. 인터넷이 필요합니다.

**A. python.org Python을 쓰는 경우**
1. <https://www.python.org/downloads/> 에서 Python 3.9~3.12를 설치합니다. **"Add python.exe to PATH"**를 꼭 체크합니다.
2. `impeller_gui\setup_venv.bat`을 더블클릭합니다. `impeller_gui\.venv\` 안에 전용 가상환경을 만들고 `PySide6`, `pyserial`을 설치합니다. PC의 다른 Python 환경은 건드리지 않습니다.

**B. Anaconda / Miniconda를 쓰는 경우**
1. 시작 메뉴에서 **Anaconda Prompt**(또는 Miniconda Prompt)를 엽니다.
2. 그 창에서 `impeller_gui` 폴더로 이동해 `setup_conda.bat`을 실행합니다.
   ```bat
   cd 저장소경로\impeller_gui
   setup_conda.bat
   ```
   `environment.yml`대로 `smcl`이라는 conda 환경(Python 3.12 + PySide6 + pyserial)이 만들어집니다.
   이미 `smcl` 환경이 있으면 패키지만 갱신합니다.

어느 쪽이든 "Setup complete"가 뜨면 끝입니다. `run_gui.bat`은 `.venv`가 있으면 그것을, 없으면 conda의 `smcl` 환경을 자동으로 찾아 씁니다. conda 환경을 쓸 때 `run_gui.bat`을 더블클릭해도 되지만, conda가 PATH에 없다고 나오면 Anaconda Prompt에서 실행하세요.

## 5. 실행과 사용 순서

`impeller_gui\run_gui.bat`을 더블클릭합니다. 창은 **위에서 아래로 쓰는 순서**로 배치되어 있습니다.

### ① Connection (맨 위)
1. Arduino를 USB로 꽂고 **Refresh ports**.
2. `USB-SERIAL CH340` 포트를 고르고 **Connect**.
3. 표시등이 **초록색 "Ready"**가 되면 준비 완료입니다. 포트를 열면 보드가 재부팅되므로 1~2초 걸립니다.
4. 보드가 없을 때는 **Simulator**를 골라 프로그램 사용법을 연습할 수 있습니다.

### ② 1. Ramp settings — 어떻게 올릴지
| 항목 | 설명 |
|---|---|
| Target speed | 도달할 목표 RPM |
| Max allowed speed | 프로그램이 보낼 수 있는 상한(기본 700). 펌웨어가 거부하면 자동으로 낮아짐 |
| Start speed | 램프 시작 RPM. **from current target**을 켜두면 보드가 지금 유지 중인 목표(정지 상태면 0)에서 시작 |
| Ramp mode | **Linear**: 일정 기울기로 상승. **Stepped**: 일정 RPM씩 계단식으로 상승 |
| Ramp speed | Linear일 때 **Rate**(RPM/s) 또는 **Duration**(총 소요 초) 중 하나로 지정. 나머지 값은 옆에 계산되어 표시 |
| Step size / dwell | Stepped일 때 한 계단의 크기와 유지 시간 |
| Hold at target | 목표 도달 후 유지할 시간. **until Stop is pressed**를 켜면 STOP을 누를 때까지 유지 |
| Stop the motor when the hold time ends | 유지 시간이 끝나면 자동 정지. 끄면 계속 회전 |
| Setpoint update interval | 램프 중 중간 목표를 보드에 보내는 간격. 기본 0.5 s 권장 |

아래 **Plan** 줄에 설정이 문장으로 요약되므로 실행 전에 한 번 읽어보면 됩니다.

### ③ 2. CSV logging — 어디에 기록할지
- **Folder**, **File prefix**를 정합니다. 파일명은 `접두어_YYYYMMDD_HHMMSS.csv`이고 기존 파일을 덮어쓰지 않습니다.
- 기본 설정(두 체크박스 켜짐)에서는 **Start ramp를 누를 때마다 새 CSV가 열리고, 실행이 끝나면 닫힙니다.**
- 실행과 무관하게 기록하려면 **Start logging / Stop logging**을 직접 누릅니다 (예: 정지 후 감속 과정을 담고 싶을 때).
- CSV 옆에 같은 이름의 `_meta.json`이 생기며 그때의 램프 설정이 들어 있습니다.

### ④ 3. Run
- **▶ Start ramp**: 계획대로 실행. 실행 중에는 설정이 잠깁니다.
- **■ STOP motor**: 언제든 즉시 정지(목표 0 전송). 실행 중이 아니어도 동작합니다.
- **Manual target / Set now**: 램프 없이 목표를 바로 보냅니다.

### ⑤ 오른쪽 화면
- 큰 숫자: 측정 RPM, 프로그램이 보낸 목표, 보드가 알려주는 목표, PID 출력, 단계(Ramping / Holding / Finished / Stopped), 경과·남은 시간.
- 그래프: 최근 2분간 목표(파랑)와 측정(빨강).
- Controller messages: 보낸 명령(`>>`), 보드 응답, 오류(`!!`). *Show every status line*을 켜면 0.5초마다 오는 원본 상태 줄도 보입니다.

창을 닫으면 정지 명령을 보내고 CSV를 닫은 뒤 설정을 저장합니다. 다음 실행 때 마지막 설정이 그대로 복원됩니다.

## 6. CSV 열 설명

보드가 상태를 보낼 때마다 한 행이 기록됩니다 (초당 2~3행).

| 열 | 설명 |
|---|---|
| `timestamp` | PC 시각 (ISO 8601, ms) |
| `unix_time_s` | 같은 시각의 Unix 초 |
| `log_elapsed_s` | 이 파일을 연 뒤 경과 초 |
| `run_elapsed_s` | Start ramp를 누른 뒤 경과 초 (실행 전이면 빈칸) |
| `phase` | `idle` / `ramping` / `holding` / `finished` / `stopped` |
| `commanded_rpm` | 프로그램이 마지막으로 보낸 목표 |
| `controller_target_rpm` | 보드가 현재 쓰는 목표 |
| `measured_rpm` | 보정·평활된 측정 RPM |
| `raw_rpm` | 보정만 하고 평활하지 않은 RPM |
| `output_percent` | PID 최종 출력 (0~100 %) |
| `strength_percent` | 실제 적용된 PWM 세기 |
| `kp`, `ki`, `kd` | 제어 이득 |
| `controller_setpoint_rpm` | 보드 내부에서 추종 중인 setpoint |
| `encoder_rpm` | 보정 전 엔코더 RPM |
| `feedforward_percent` | 피드포워드 출력 |
| `d_rpm_s` | RPM 변화율 |
| `hold` | 보드가 출력 고정(수렴 완료) 상태면 1 |
| `open_loop` | 개루프(`O` 명령) 모드면 1 |

## 7. 자주 겪는 문제

| 증상 | 원인과 해결 |
|---|---|
| `setup_venv.bat`에서 `No runtime that matches 3` 또는 `No Python 3.9 or newer was found` | Python 런처만 있고 실제 Python이 없는 상태. python.org에서 Python을 설치하거나(4절 A), conda가 있으면 `setup_conda.bat`을 쓰세요(4절 B) |
| `run_gui.bat`에서 `conda was not found on PATH` | Anaconda Prompt를 열어 그 창에서 `run_gui.bat`을 실행하세요 |
| 포트 목록에 보드가 없다 | USB 케이블(데이터 지원 여부), CH340 드라이버 확인. 꽂은 뒤 **Refresh ports** |
| Connect는 되는데 Ready가 안 된다 | 다른 프로그램(Arduino 시리얼 모니터 등)이 포트를 잡고 있거나, 보드에 `impeller.ino`가 아닌 다른 스케치가 올라가 있음 |
| 메시지 창에 `ERR command timeout; stopped motor` | 보드의 2.5초 안전 정지. 프로그램은 회전 중 1초마다 자동으로 신호를 보내므로 정상 사용 중에는 나오지 않음. PC가 멈췄거나 케이블이 흔들린 경우 |
| 메시지 창에 `ERR overspeed; stopped motor` | 측정 RPM이 750을 넘어 보드가 정지. 보정표가 맞는지 확인 |
| 목표를 바꿔도 측정 RPM이 그대로다 | 펌웨어의 `USE_FIXED_TARGET_OUTPUT_TABLE`이 `true`로 올라가 있음. `false`로 바꿔 다시 업로드 |
| "Connection lost" 창이 뜬다 | USB가 빠짐. 보드는 마지막 목표를 유지하므로 다시 꽂고 Connect → **STOP motor** |
| 램프 중 속도가 목표보다 조금 뒤처진다 | 정상. 보드의 PID가 따라오는 데 1~2초 걸리며, 유지 구간에서 수렴함 |

## 8. 자체 점검

`impeller_gui\run_tests.bat`을 실행하면 램프 계산, 통신 프로토콜 해석, 시뮬레이터, CSV 기록에 대한 단위 테스트가 돌아갑니다. 모두 `OK`면 프로그램 설치가 정상입니다.

## 9. 문의

Juhyeon Moon — juhyeun2001@snu.ac.kr
