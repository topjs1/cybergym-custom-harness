# 구현 설명

## 모듈

| 모듈 | 책임 |
|---|---|
| `config.py` | 모든 model, budget, timeout, path, sandbox 설정의 환경변수 파싱과 검증 |
| `server.py` | Agent Card, A2A Starlette app, no-attachment-history task store, uvicorn CLI |
| `executor.py` | 초기 task와 같은 context의 verifier feedback 재진입 라우팅 |
| `controller.py` | task session, LLM/tool loop, PoC 시도, feedback queue, final artifact, 상태 |
| `attachments.py` | A2A part 해석, base64 검증, 작업 디렉터리 초기화, 안전한 tar 해제 |
| `code_tools.py` | 파일 목록, bounded read, ripgrep/fallback search, symbol context, error parsing |
| `llm.py` | DeepSeek API adapter, usage/cache parsing, deterministic Mock adapter |
| `prompts.py` | 독립 system prompt와 JSON tool schema |
| `sandbox.py` | safe byte DSL, disabled/mock backend, hardened Docker Python backend |
| `metrics.py` | 실제/추정 usage 구분, 가격 snapshot, RSS sampling, CSV/JSONL write |

## Task Manager와 상태 격리

`TaskManager`는 `context_id → HarnessSession` map을 유지한다. 초기 메시지는 새 session에서 처리하고, Green의 follow-up `DataPart`는 `Message.context_id`로 같은 session의 queue에 전달한다. session 종료 시 map에서 제거한다.

workspace 이름에는 외부 context 문자열을 직접 쓰지 않고 SHA-256 앞 24자를 사용한다. 기존 같은 경로가 있으면 새 task 시작 시 제거해 과거 소스·PoC가 다음 실행에 들어오지 않는다. model conversation, 제출 hash, token counter도 session 객체에만 존재한다.

## 첨부 처리

허용되는 A2A file 형태는 `FilePart(FileWithBytes)`이다. 다음을 거부한다.

- `/`, `..`, 다중 path component가 있는 attachment name
- 잘못된 base64
- attachment 또는 압축 해제 총량 제한 초과
- tar absolute path, `..`, symlink, hard link, device, 특수 entry
- destination 밖으로 resolve되는 path

`repo-vul.tar.gz`와 `repo-fix.tar.gz`는 별도 디렉터리에 해제한다. 텍스트 evidence는 input root에 복사한다.

## 코드 도구 동작

`list_files`는 `.git`, build, vendor 등의 큰 디렉터리를 건너뛴다. `read_file`은 한 번에 최대 400줄을 반환한다. `search_code`는 shell을 거치지 않고 `rg` argument array로 실행하고, 없으면 Python regex fallback을 쓴다. `symbol_context`는 최대 3개 위치 주변 코드만 반환한다. 모든 path는 task input root 안인지 `resolve()` 후 확인한다.

## LLM loop

1. system prompt와 challenge metadata만 초기 대화에 넣는다. tar 전체는 넣지 않는다.
2. DeepSeek가 필요한 파일 도구를 선택한다.
3. assistant tool-call message와 각 tool result를 순서대로 대화에 기록한다.
4. 오래된 대화가 늘면 system/initial message와 최근 tail을 보존한다.
5. API usage를 metrics에 더하고 다음 호출 전에 남은 budget을 계산한다.
6. `submit_poc`가 성공하면 즉시 loop를 종료한다.
7. 중복 PoC, attempt cap, token cap, wall timeout에서 중단한다.

API 오류는 최대 3회 연속까지 짧은 backoff 후 재시도한다. 실패한 HTTP 요청도 `api_calls`에 포함한다. API key는 header로만 전달하며 로그나 exception message에 넣지 않는다.

## PoC 생성과 실행

`build_poc`는 append-only operation 배열을 bytes로 변환한다. 정수 packing은 endian을 명시하고 size별 unsigned format을 사용한다. 결과가 `HARNESS_MAX_POC_BYTES`를 넘으면 즉시 거부한다.

Docker Python backend는 shell interpolation 없이 `create_subprocess_exec()` argument array로 Docker CLI를 부른다. 실행 코드는 명령의 마지막 단일 argument이며 host shell이 해석하지 않는다. output bind mount 외에는 host path를 제공하지 않는다. 실제 PoC는 `/output/poc`에 있어야 한다.

## Metrics

필수 열 외에 다음 provenance 열을 추가했다.

- `task_id_source`: Green 제공 ID인지 A2A fallback인지
- `input_cache_miss_tokens`: 비용 계산용 실제 cache miss
- `usage_source`: `api_actual`, `estimated`, `mixed`, `none`
- `api_cost_actual_usd`: billing export 결합용, 현재는 빈 값
- `api_cost_source`: 추정 방식 또는 unavailable
- `pricing_tier`, `pricing_snapshot`
- `error`

RSS peak는 Linux에서 `/proc/self/status`의 현재 RSS를 250ms 간격으로 sampling한다. macOS test에서는 `resource.getrusage` high-water mark fallback이므로 엄밀한 task 증분 값이 아니다. 최종 실험은 동일 Linux runner/container에서 비교해야 한다.

## 테스트 코드

- `test_attachments.py`: 정상 난이도/압축 해제와 tar traversal 거부
- `test_code_tools.py`: list/read/search/symbol과 workspace escape 거부
- `test_sandbox.py`: byte builder 및 Docker hardening argument 검증
- `test_controller.py`: Mock LLM의 2회 제출, mock verifier feedback, PASS artifact, usage/cost CSV
- `test_a2a_application.py`: 실제 A2A SDK client + ASGI app으로 Agent Card, 첨부 challenge 수신, artifact event
- `tools/mock_green_roundtrip.py`: 실제 TCP/Unix socket에서 Green 방식의 streaming feedback 왕복을 재현하는 무료 도구

## 현재 제한

- 공식 Green 초기 메시지의 실제 task ID 누락은 하네스 내부에서 해결할 수 없다.
- 공식 score/fixed 결과는 Purple에 되돌아오지 않으므로 Green result와 후처리 결합이 필요하다.
- 실제 uvicorn TCP listener와 Docker 컨테이너에서 live streaming feedback roundtrip을 검증했다. 공식 Green의 vulnerable/fixed target 실행은 아직 수행하지 않았다.
- 실행 환경에서 Docker daemon 접근이 금지되어 image build와 실제 Python sandbox container는 미검증이다.
- 직접 의존성은 정확한 버전으로 고정했지만 전이 의존성 전체 `uv.lock`은 현재 환경의 외부 cache 접근 제한 때문에 생성하지 못했다.
