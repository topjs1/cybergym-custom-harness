# CyberGym Custom Harness

CyberGym의 독립적인 AgentBeats Purple Agent MVP이다. 한 DeepSeek agent가 제한된 코드 탐색 도구를 사용하고, PoC 후보를 공식 Green verifier에 반복 시험한 뒤 최종 raw input artifact 하나를 제출한다.

핵심 원칙은 작고 측정 가능하며 문제 간 상태가 없는 하네스다. AgentWhetters 소스는 복사하지 않았고, 라이선스가 명확한 A2A SDK와 공개 wire interface를 사용해 독립 구현했다.

## 현재 상태

- 구현: A2A app/executor, task manager, attachment extraction, code tools, DeepSeek adapter, safe PoC builder, optional Docker Python sandbox, verifier feedback queue, metrics
- 독립 환경: Python 3.13 `.venv`와 `uv.lock` 생성 완료
- 무료 테스트: 9개 PASS
- live TCP A2A feedback roundtrip: PASS
- Docker build와 hardened container smoke test: PASS
- 유료 DeepSeek 호출: 수행하지 않음
- 대용량 CyberGym/image download: 수행하지 않음

자세한 실제 결과와 제한은 [TEST_RESULTS.md](TEST_RESULTS.md)를 먼저 확인한다.

## 구조

```text
cybergym-custom-harness/
├── src/cybergym_harness/
│   ├── server.py          # A2A application / CLI
│   ├── executor.py        # initial/feedback request routing
│   ├── controller.py      # main single-agent feedback loop
│   ├── attachments.py     # safe A2A file and tar handling
│   ├── code_tools.py      # list/read/search/symbol/error tools
│   ├── static_triage.py   # deterministic file ranking / entry / crash hints
│   ├── mutation.py        # deterministic near-miss byte mutator
│   ├── feedback.py        # verifier feedback classification
│   ├── subagents/         # read-only recon and reviewer subagents
│   ├── dynamic/           # isolated build/run/fuzz/triage backends
│   ├── llm.py             # DeepSeek and Mock adapters
│   ├── sandbox.py         # byte DSL and optional Docker Python
│   └── metrics.py         # CSV/JSONL/token/cost/time/memory + role/phase tags
├── sidecar/               # dynamic-sandbox service (only socket holder)
├── tests/
├── tools/
├── amber-manifest.json5
├── scenario.toml
├── docker-compose.override.yml
└── results.csv
```

## 설치

Python 3.13과 `uv`를 권장한다.

```bash
cd cybergym-custom-harness
UV_CACHE_DIR="$PWD/.uv-cache" uv sync --locked --extra test
```

현재 `pyproject.toml`의 직접 의존성은 exact version이다. 최초 설치/lock 생성에는 일반 Python package download가 필요하다. CyberGym dataset이나 target image는 이 단계에서 받지 않는다.

## 무료 테스트

```bash
UV_CACHE_DIR="$PWD/.uv-cache" uv run --extra test pytest -q
```

socket이 허용된 환경에서는 실제 uvicorn subprocess와 streaming feedback도 시험한다.

```bash
UV_CACHE_DIR="$PWD/.uv-cache" uv run --extra test python tools/mock_green_roundtrip.py --tcp --keep-server-log
```

이 명령은 `HARNESS_LLM_MODE=mock`을 사용하므로 API key나 비용이 없다.

## 로컬 서버

Mock mode:

```bash
export HARNESS_LLM_MODE=mock
export HARNESS_RESULTS_CSV="$PWD/data/results.csv"
export HARNESS_EVENTS_JSONL="$PWD/data/events.jsonl"
export HARNESS_WORK_ROOT="$PWD/data/tasks"
UV_CACHE_DIR="$PWD/.uv-cache" uv run python -m cybergym_harness.server \
  --host 127.0.0.1 --port 9009
```

Agent Card:

```bash
curl http://127.0.0.1:9009/.well-known/agent-card.json
```

## DeepSeek 설정

실제 key를 저장소 파일이나 명령행에 쓰지 않는다.

```bash
read -s DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
export HARNESS_LLM_MODE=deepseek
export DEEPSEEK_MODEL=deepseek-flash
export DEEPSEEK_THINKING=enabled
export DEEPSEEK_MAX_OUTPUT_TOKENS=4096
export HARNESS_TOKEN_BUDGET=100000
export HARNESS_TASK_TIMEOUT_SECONDS=1800
export HARNESS_MAX_ATTEMPTS=30
```

실제 호출은 비용 승인을 받은 뒤에만 수행한다.

### A1a DeepSeek 파일럿 (실제 타깃 없이)

`tools/deepseek_pilot.py`는 실제 DeepSeek로 파이프라인 전체(thinking+tool calls, triage,
build_poc, submit_poc, 피드백 루프, 예산, metrics, 최종 artifact)를 **stub verifier**로
검증한다. target 이미지 다운로드나 실제 Green이 필요 없다.

```bash
cd cybergym-custom-harness
read -s DEEPSEEK_API_KEY && export DEEPSEEK_API_KEY
UV_CACHE_DIR="$PWD/.uv-cache" uv run python tools/deepseek_pilot.py \
  --tcp --stub nth=3 --model deepseek-flash --token-budget 100000 --max-attempts 30
unset DEEPSEEK_API_KEY
```

- `--stub never|always|nth=N`: N번째 제출을 크래시로 간주.
- `--task-dir DIR`: 실 repo-vul/description/error 사용(없으면 합성 챌린지).
- `--stub-output FILE`: 실제 ASan 로그를 feedback으로 주입.
- 기본 transport는 **TCP subprocess**다. `--inprocess`(ASGI)는 버퍼링 때문에 제출마다
  `HARNESS_FEEDBACK_TIMEOUT_SECONDS`만큼 지연되므로 파일럿에는 TCP를 권장한다.
- 산출: `data/pilot-<ts>/results.csv`·`events.jsonl` + 토큰/캐시/지연/추정비용 요약.
- 키는 shell로만 주입하며 파일/로그에 남기지 않는다.

## Docker build와 실행

```bash
docker build -t cybergym-custom-harness:local .
mkdir -p data
docker run --rm \
  --read-only --cap-drop ALL --security-opt no-new-privileges \
  --tmpfs /tmp:rw,nosuid,nodev,noexec,size=64m \
  -p 127.0.0.1:9009:9009 \
  -v "$PWD/data:/data" \
  -e HARNESS_LLM_MODE=mock \
  cybergym-custom-harness:local \
  --host 0.0.0.0 --port 9009 --card-url http://127.0.0.1:9009
```

image는 non-root UID 10001로 실행된다.

## AgentBeats 배포 준비

`.github/workflows/test-and-publish.yml`은 테스트, Amber manifest 검증,
Linux/AMD64 이미지 빌드와 smoke test를 수행한다. `main` 또는 `v*` tag를
push하면 `ghcr.io/topjs1/cybergym-custom-harness`에 이미지를 게시한다.

현재 `amber-manifest.json5`도 위 GHCR image를 가리키며 Amber CLI v0.4
검증을 통과한다. AgentBeats 등록 전에는 다음이 추가로 필요하다.

1. 이 디렉터리를 `topjs1/cybergym-custom-harness` GitHub 저장소로 게시한다.
2. GitHub Actions가 성공해 GHCR package가 생성됐는지 확인한다.
3. GHCR package를 public으로 공개한다.
4. raw manifest URL을 AgentBeats Purple Agent 등록 폼에 입력한다.

```text
https://raw.githubusercontent.com/topjs1/cybergym-custom-harness/main/amber-manifest.json5
```

## AgentBeats/CyberGym 연결 준비

1. Custom image와 공식 Green image를 local tag로 만든다.
2. `scenario.toml`의 task list와 level을 동결한다.
3. leaderboard의 compose generator로 `docker-compose.yml`을 만든다.
4. 이 저장소의 override를 함께 적용한다.
5. Green이 쓸 target image/dataset 다운로드는 크기를 확인하고 승인 후 수행한다.

예시:

```bash
python ../cybergym-poc/references/cybergym-leaderboard/generate_compose.py \
  --scenario scenario.toml
read -s DEEPSEEK_API_KEY
export DEEPSEEK_API_KEY
docker compose -f docker-compose.yml -f docker-compose.override.yml \
  up --abort-on-container-exit
unset DEEPSEEK_API_KEY
```

`docker-compose.override.yml`은 Green에 target 실행용 Docker socket을 주고, Purple에는 주지 않는다. Purple은 default `build_poc`만으로 동작한다. Green과 submission endpoint는 private network에만 둔다.

## Python PoC sandbox

기본 `HARNESS_PYTHON_BACKEND=disabled`가 안전한 운영값이다. 모델은 `build_poc` DSL로 bytes를 만든다.

별도 Docker daemon이 의도적으로 준비된 로컬 host 실행에서만 다음을 선택할 수 있다.

```bash
docker pull python:3.13.9-slim-bookworm  # 다운로드 승인 후 한 번만
export HARNESS_PYTHON_BACKEND=docker
```

Python sandbox는 network 없음, read-only root, non-root, capability 없음, resource cap이며 API key와 source tree를 받지 않는다. Docker daemon이나 image가 없으면 자동 fallback 실행을 하지 않고 오류를 반환한다.

## 동적분석 (Dynamic analysis)

기본값 `HARNESS_DYNAMIC_ANALYSIS=off`가 안전한 운영값이다. 켜면 모델이
`build_target` → `run_target`/`fuzz_target` → `triage_crash`로 취약 타겟을 직접
빌드·퍼징해 크래시 입력을 찾고, 그 바이트를 `submit_poc`로 공식 검증한다.

```bash
export HARNESS_DYNAMIC_ANALYSIS=sidecar
export HARNESS_DYNAMIC_URL=http://dynamic-sandbox:9110
export CYBERGYM_HOST_DATA="$PWD/data"   # compose가 sidecar에 전달
docker compose -f docker-compose.yml -f docker-compose.override.yml up --abort-on-container-exit
```

격리 원칙:

- Purple 컨테이너는 Docker socket이 없다. socket은 평가자(Green)와
  `dynamic-sandbox` sidecar에만 있다.
- sidecar는 API key와 외부 egress가 없고, private compose network에서만 통신한다.
- builder/타겟 컨테이너는 일회성이며 `--network=none`(빌드 예외는 opt-in),
  `--read-only`, `--cap-drop=ALL`, `no-new-privileges`, 비-root, 자원 제한을 강제한다.
- dynamic 도구에 넘기기 전에 `.git`, `patch.diff`, `tmp/poc` 같은 정답 누출
  아티팩트를 제거한다.
- 로컬 크래시는 신호일 뿐이다. 최종 성공은 항상 Green의 `submit_poc` 결과로 확정한다.
- 별도 builder/runner image 다운로드와 x86_64 runner가 필요하다. 설계는
  [DYNAMIC_ANALYSIS_DESIGN.md](DYNAMIC_ANALYSIS_DESIGN.md) 참조.

## 정적분석 · 서브에이전트 · 변이

- **정적 triage**는 항상 실행되며 토큰을 쓰지 않는다. 파일 랭킹, fuzz entry point,
  ASan 크래시 함수·위치, 입력 종류, magic bytes를 초기 컨텍스트에 넣는다.
- **RECON 서브에이전트**(`HARNESS_RECON_SUBAGENT=true`)는 시작 시 읽기 전용 도구로
  가설 JSON을 만들어 주 솔버에 넘긴다.
- **REVIEWER 서브에이전트**(`HARNESS_REVIEWER_SUBAGENT=true`)는 `HARNESS_STALL_THRESHOLD`
  회 연속 실패 시 대안 가설을 제안한다(`HARNESS_REVIEWER_MAX_CALLS`로 상한).
- **near-miss mutator**(`HARNESS_MUTATION_ATTEMPTS>0`)는 실패한 후보의 trigger 구간을
  무토큰으로 변이해 자동 재시도한다. 시도 예산을 소비하므로 값은 작게 시작한다.
- **K=2 앙상블**(`HARNESS_TRACKS=2`)은 정적(source-first) 트랙과 동적/포맷 트랙을 동시에
  돌려 상호보완적으로 탐색한다(공유 submit 채널·예산·중복 해시 풀, 첫 성공 시 종료).
- **긴/구조적 PoC**는 `build_poc`의 `pad`/`repeat_seq`/`u8..u64`/`crc32`/`patch` 연산으로
  조립한다(큰 리터럴 금지).
- **조기 종료 방지**(`HARNESS_RESUME_NUDGES`)로 제출 전 포기 시 재개를 유도한다.
- 서브에이전트 토큰은 metrics에 `role` 태그로 따로 합산되어 추후 경제성 비교에 쓰인다.

## 결과

root의 `results.csv`는 schema template이다. 실제 container는 `/data/results.csv`와 `/data/events.jsonl`에 쓴다.

```bash
python tools/summarize_results.py data/results.csv
```

`api_cost_usd`는 DeepSeek token usage와 실행 당시 price tier로 계산한 추정치다. 실제 billing 값은 `api_cost_actual_usd`에 별도 결합한다.

## 문서

- [RESEARCH.md](RESEARCH.md): 인터페이스와 구조 조사
- [DESIGN.md](DESIGN.md): 선택 구조와 근거
- [ARCHITECTURE.md](ARCHITECTURE.md): Mermaid 구조와 흐름
- [IMPLEMENTATION.md](IMPLEMENTATION.md): 모듈·코드 설명
- [ENVIRONMENT.md](ENVIRONMENT.md): 버전·secret·Docker 환경
- [TEST_RESULTS.md](TEST_RESULTS.md): 실제 실행 결과와 미검증 항목
- [COMPARISON_PLAN.md](COMPARISON_PLAN.md): AgentWhetters와의 정량 비교 계획
- [AGENTBEATS_REGISTRATION.md](AGENTBEATS_REGISTRATION.md): GHCR 게시와 Purple Agent 등록 체크리스트

## 첫 실제 평가 전에 남은 일

1. GitHub 저장소와 공개 Linux/AMD64 GHCR image를 게시하고 AgentBeats에 등록한다.
2. 승인된 CyberGym subset/target image를 준비한다.
3. 공식 Green과 한 task를 Mock LLM으로 연결해 network와 artifact를 확인한다.
4. AgentWhetters의 cached token을 외부에서 수집하는 공통 recorder를 마련한다.
5. Green의 실제 task ID와 Purple metrics row 결합 방법을 확정한다.
6. 유료 호출 상한과 승인을 받은 뒤 같은 DeepSeek 조건으로 1-task pilot을 실행한다.
