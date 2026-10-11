# 테스트 결과

실행일: 2026-10-12. 유료 API 호출과 대용량 다운로드는 수행하지 않았다.

## 결과 요약

| 항목 | 결과 | 실제 확인 범위 |
|---|---|---|
| Python syntax/import | PASS | `compileall`로 source/tests/tools import·문법 확인 |
| 단위/통합 테스트 | PASS | 9 tests |
| 첨부와 tar 보안 | PASS | 정상 추출, level 판별, `../../` traversal 거부 |
| 코드 검색 도구 | PASS | list/read/rg 또는 fallback/symbol, root escape 거부 |
| structured PoC builder | PASS | hex, integer endian packing, repeat bytes |
| Python sandbox 구성 | PARTIAL | Docker command hardening과 secret 비전달은 fake process로 검증; 실제 container는 미실행 |
| Mock LLM | PASS | 실제 HTTP 없이 고정 usage/tool call 2회 처리 |
| Mock verifier feedback loop | PASS | 1차 exit 0 → 2차 exit 1 → 정확한 최종 PoC artifact |
| metrics CSV/JSONL | PASS | token/cache/call/time/cost source/attempt/status 저장 |
| A2A application | PASS | 실제 A2A SDK client와 ASGI app으로 Agent Card, challenge attachment, artifact event 확인 |
| 실제 listener 시작 | PASS | loopback TCP uvicorn subprocess 기동과 Agent Card HTTP 200 |
| live A2A streaming feedback | PASS | 1차 exit 0, 2차 exit 1, 최종 artifact를 실제 TCP에서 왕복 |
| Docker build | PASS | lock 기반 `cybergym-custom-harness:local` Linux/ARM64 image |
| hardened Docker roundtrip | PASS | UID 10001, read-only, cap drop, no-new-privileges 상태에서 A2A/metrics 확인 |
| Amber manifest | PASS | `amber-cli:v0.4 check` |
| 실제 CyberGym Green | PARTIAL | 공식 Green source 환경과 Agent Card 기동 PASS; target image 평가는 미실행 |
| 실제 DeepSeek | NOT RUN | 사용자 승인 전 유료 호출 금지 |

## 실행 명령과 출력

### 문법과 테스트

```bash
.venv/bin/python -m compileall -q src tests tools
.venv/bin/python -m pytest -q
```

결과:

```text
.........                                                                [100%]
9 passed in 1.24s
```

테스트는 이 저장소의 `uv.lock`으로 생성한 독립 Python 3.13 `.venv`에서 실행했다.

### A2A server application

`test_a2a_application.py`는 실제 `A2ACardResolver`, `ClientFactory`, streaming client를 `httpx.ASGITransport`에 연결했다. 다음을 확인했다.

- `/.well-known/agent-card.json`을 해석할 수 있음
- Agent Card 이름과 capability가 노출됨
- A2A `Message`의 FileWithBytes challenge가 Task Manager에 도달함
- workspace가 만들어지고 task_id가 기록됨
- 최종 `TaskArtifactUpdateEvent`가 발생함

ASGI transport는 application이 반환할 때까지 response stream을 buffer하므로, 같은 stream 도중 Green이 follow-up message를 보내는 live feedback 재진입은 이 방법으로 검증할 수 없었다. 그 부분은 `test_controller.py`에서 session queue와 두 번의 verifier 응답으로 검증했다.

### 실제 socket listener와 feedback

다음 명령으로 실제 loopback TCP listener와 streaming feedback을 검증했다.

```bash
uv run python tools/mock_green_roundtrip.py --tcp --keep-server-log
```

결과는 `PASS a2a_roundtrip attempts=2 poc_bytes=14`였다. 도구가 server subprocess를 시작하고, Green Agent와 같은 event pattern으로 첫 PoC에 exit 0, 두 번째 PoC에 exit 1을 회신한 뒤 최종 artifact와 CSV PASS row를 검사했다.

### Docker build와 hardened roundtrip

최초 Dockerfile의 `0.12.10-python3.13-bookworm-slim` tag가 registry에 존재하지 않는 것을 발견했다. 유효한 multi-platform base digest로 고정하고 `uv.lock`을 image에 포함해 `uv sync --locked --no-dev`를 강제했다.

```bash
docker build -t cybergym-custom-harness:local .
```

빌드 후 read-only root, all capabilities drop, `no-new-privileges`, 제한된 tmpfs를 적용해 컨테이너를 기동했다. 외부 mock Green roundtrip에서 2번의 PoC 시험과 14-byte 최종 artifact가 확인됐고 `/data/results.csv`, `/data/events.jsonl`도 생성됐다.

## 기능별 테스트 연결

| 요구 기능 | 테스트 |
|---|---|
| 모의 문제 수신 | `test_a2a_application.py`, `test_attachments.py` |
| 코드 검색 | `test_code_tools.py` |
| Mock LLM | `test_controller.py` |
| Mock verifier | `test_controller.py` |
| 피드백 loop | `test_controller.py` |
| metric log | `test_controller.py` |
| DeepSeek usage/cache parsing | `test_llm.py` |
| sandbox security options | `test_sandbox.py` |
| archive/path defense | `test_attachments.py`, `test_code_tools.py` |

## 발견 후 수정한 문제

초기 in-process A2A 실험에서는 ASGI transport가 streaming body를 buffer하여 verifier follow-up이 session timeout 이후 도착했다. 이를 실제 네트워크 성공으로 오판하지 않았고, 실제 TCP subprocess와 Docker 컨테이너를 대상으로 별도 왕복 테스트를 추가했다.

초기 Docker build에서는 존재하지 않는 uv image tag를 사용했고, hardened smoke test에서는 `/data` tmpfs가 root 소유여서 UID 10001이 metrics를 기록하지 못했다. base image digest를 고정하고 `/data` tmpfs에 `uid=10001,gid=10001`을 지정해 수정했다.

## 미검증 위험

- 실제 DeepSeek thinking-mode tool call에서 여러 tool call과 `reasoning_content`가 장시간 반복될 때의 호환성
- 실제 Green의 10초 target timeout과 Purple의 60초 feedback timeout 상호작용
- 큰 repo tar에서 메모리 peak와 추출 시간
- Docker Python sandbox의 host별 bind mount UID 동작
- 공식 Green의 final `task_results`와 Purple metrics row 자동 결합
- GitHub Actions Linux/AMD64 publish와 공개 GHCR image의 AgentBeats 실행
