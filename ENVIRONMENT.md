# 실행환경과 의존성

## 확인한 개발 환경

- 호스트 Python: 3.12.12
- 테스트 Python: 3.13.15
- uv: 0.12.10
- Docker client: 29.6.1
- Docker client/daemon: 29.6.1, 현재 세션에서 접근 가능
- OS: macOS 계열 호스트, 최종 비교 권장은 동일 Linux runner

## 고정한 직접 의존성

| 패키지 | 버전 | 선택 이유 |
|---|---:|---|
| `a2a-sdk[http-server]` | 0.3.26 | 로컬 공식 참고 구현과 호환되는 A2A server/client API |
| `httpx` | 0.28.1 | DeepSeek 비동기 HTTP 및 테스트 transport |
| `pydantic` | 2.13.3 | A2A dependency와 현재 검증 환경에 맞는 model validation |
| `uvicorn` | 0.46.0 | A2A Starlette application 실행 |
| `pytest` | 9.0.3 | 테스트 |
| `pytest-asyncio` | 1.3.0 | async A2A/controller 테스트 |
| `hatchling` | 1.27.0 | package build backend |

`pyproject.toml`은 위 직접 의존성을 모두 `==`로 고정하며 `uv.lock`에 전체 transitive dependency가 잠겨 있다. 독립 `.venv`는 다음 명령으로 생성했다.

```bash
cd cybergym-custom-harness
UV_CACHE_DIR="$PWD/.uv-cache" uv sync --locked --extra test
```

실험 중 버전 drift를 막으려면 lock 생성 이후 두 하네스의 image digest와 `pip/uv` freeze를 run manifest에 기록한다.

## 환경변수

| 변수 | 기본값 | 의미 |
|---|---|---|
| `DEEPSEEK_API_KEY` | 없음 | 실제 모드 필수 secret |
| `DEEPSEEK_BASE_URL` | `https://api.deepseek.com` | OpenAI-format endpoint |
| `DEEPSEEK_MODEL` | `deepseek-flash` | 비교할 정확한 model alias |
| `DEEPSEEK_THINKING` | `enabled` | `enabled/disabled`; AgentWhetters가 생략해 받는 공식 기본값과 정렬 |
| `DEEPSEEK_TEMPERATURE` | `0.0` | non-thinking sampling |
| `DEEPSEEK_MAX_OUTPUT_TOKENS` | `4096` | 호출당 output cap |
| `HARNESS_TASK_TIMEOUT_SECONDS` | `1800` | 문제 전체 wall timeout |
| `HARNESS_TOKEN_BUDGET` | `100000` | API-reported input+output 누적 cap |
| `HARNESS_MAX_ATTEMPTS` | `30` | verifier PoC 시도 cap |
| `HARNESS_LLM_TIMEOUT_SECONDS` | `180` | API 요청 timeout |
| `HARNESS_FEEDBACK_TIMEOUT_SECONDS` | `60` | Green feedback timeout |
| `HARNESS_WORK_ROOT` | `/tmp/.../tasks` | task workspace root |
| `HARNESS_RESULTS_CSV` | `/tmp/.../results.csv` | summary CSV |
| `HARNESS_EVENTS_JSONL` | `/tmp/.../events.jsonl` | 상세 event log |
| `HARNESS_PYTHON_BACKEND` | `disabled` | `disabled/docker`; `mock`은 테스트 전용 |
| `HARNESS_SANDBOX_IMAGE` | `python:3.13.9-slim-bookworm` | 미리 받은 sandbox image |
| `DEEPSEEK_PRICING_TIER` | `off_peak` | 비용 추정 tier |

## secret 주입

실제 값을 파일에 쓰거나 CLI argument로 전달하지 않는다.

```bash
read -s DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
docker compose --env-file .env up --abort-on-container-exit
unset DEEPSEEK_API_KEY
```

`.env`는 gitignore 대상이다. AgentBeats Quick Submit에서는 browser-side encrypted secret 전달과 만료 기능을 사용하고, local manual run은 shell 또는 Docker Compose environment interpolation을 사용한다.

## 컨테이너 보안

Purple image는 UID/GID 10001로 실행한다. `docker-compose.override.yml`은 다음을 추가한다.

- read-only root filesystem
- all Linux capabilities drop
- no-new-privileges
- 제한된 `/tmp` tmpfs
- 결과용 `./data:/data`만 mount

Green은 target image 실행을 위해 Docker API가 필요하므로 local override에서 `/var/run/docker.sock`을 mount한다. 이 endpoint와 Green server는 외부 인터넷에 절대 공개하지 않는다. [CyberGym README](https://github.com/sunblaze-ucb/cybergym)는 Docker network gateway에만 bind하고 firewall/allowlist를 쓰도록 경고한다.

Purple에는 Docker socket을 mount하지 않는다. 따라서 AgentBeats 표준 컨테이너 실행에서는 `build_poc`가 기본 생성기다. 별도 Python sandbox가 필요하면 Green/agent network와 분리된 전용 sandbox service나 승인된 local daemon 구성을 먼저 검토한다.

로컬 `cybergym-custom-harness:local` 이미지는 Linux/ARM64로 빌드됐으며 약 91MB이다. 실제 기동 검증에서는 UID/GID 10001, read-only root, capability 전체 제거, `no-new-privileges`, 제한된 `/tmp`와 `/data` tmpfs를 적용했다. GitHub Actions는 AgentBeats 실행 환경에 맞춰 Linux/AMD64 이미지를 빌드하도록 구성했다.

## 대용량 자산

이 작업에서는 CyberGym dataset, target images, Python sandbox image를 다운로드하지 않았다. CyberGym 전체 dataset은 공식 문서상 약 240GB이고 server full data는 훨씬 크므로 사전 승인 없이 실행하지 않는다. 첫 실제 문제는 subset 또는 이미 존재하는 cache/image로 시작한다.
