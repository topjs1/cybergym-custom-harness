# 설계 결정

## 선정 구조

초기 버전은 “단일 Main LLM + 좁은 결정적 도구 + CyberGym verifier 피드백” 구조를 채택했다.

```text
CyberGym Green → A2A Server → Task Manager → bounded code tools
                                              ↕
                                      Main DeepSeek Agent
                                              ↕
                           safe byte builder / optional Python sandbox
                                              ↕
                             Green test_vulnerable feedback
                                              ↓
                         final artifact + metrics collector
```

이 구조를 선택한 이유는 CyberGym 한 문제의 핵심 상태가 하나의 코드 경로, 하나의 입력 형식, 하나의 최종 PoC로 강하게 결합되어 있기 때문이다. 독립 서브태스크를 넓게 수집하는 연구형 작업보다 순차적인 “근거 탐색 → bytes 생성 → 검증 → 수정”이 자연스럽다. 단일 대화가 가설과 실패 결과를 잃지 않으며, 실험의 토큰·시간·비용을 해석하기도 쉽다.

## 구성 요소

### Task Manager

- A2A context마다 `HarnessSession`을 하나 생성한다.
- workspace는 context ID의 SHA-256 prefix로 분리한다.
- 새 task가 시작되면 같은 workspace가 있으면 제거하고 다시 만든다.
- 대화, 시도 hash, feedback queue, metrics를 문제 간 공유하지 않는다.
- 전체 1,800초, 100,000 token, 30 PoC 시도를 기본값으로 둔다.

### Code Analysis Tools

Python으로 구현한 결정적 기능은 다음과 같다.

- 파일 목록과 크기 조회
- 줄 번호가 있는 제한된 파일 읽기
- ripgrep 우선, Python fallback regex 검색
- 함수/심볼 검색과 주변 최대 3개 문맥 추출
- sanitizer 종류, frame, source location 오류 로그 추출
- tar 안전 해제와 첨부 크기 제한

초기 MVP에서 ctags/cscope/Semgrep을 필수화하지 않았다. 언어별 설치비용과 false positive가 생기며, CyberGym repo마다 build system과 언어가 다르기 때문이다. 향후 ablation에서 `ctags`가 있으면 symbol index를 만드는 optional backend로 추가할 수 있다.

### LLM Controller

- DeepSeek OpenAI-format `/chat/completions`를 `httpx`로 직접 호출한다.
- 모델, thinking mode, temperature, output cap을 환경변수로 고정할 수 있다.
- tool arguments를 JSON으로 파싱한 뒤 각 Python 도구가 다시 검증한다.
- 최근 대화 tail만 보존하는 bounded compaction으로 오래된 tool output 누적을 제한한다.
- API usage가 있으면 실제 값을 사용하고 없을 때만 추정한다.
- 총 token cap은 API가 보고한 누적량으로 hard stop한다. 다음 호출 전 문자수/4 보수 추정으로 남은 output cap도 줄인다. tokenizer를 로컬에 포함하지 않았으므로 한 API 호출 범위의 overshoot 가능성은 남으며 로그에 기록한다.

### PoC Generator

기본 도구 `build_poc`는 model code를 실행하지 않는다. `hex`, `base64`, `text`, `repeat`, endian별 `u16/u32/u64` 연산만 허용해 대부분의 바이너리 입력을 만들 수 있다.

`run_python`도 구현했지만 기본값은 `disabled`이다. `docker` backend가 명시되면 다음 고정 옵션으로 별도 컨테이너를 실행한다.

- `--network=none`, `--read-only`, `--cap-drop=ALL`
- `no-new-privileges`, non-root UID/GID 65534
- memory 128 MiB, CPU 0.5, pids 64, timeout
- 비밀 환경변수를 전달하지 않음
- task source가 아니라 빈 output 디렉터리만 `/output`에 mount
- 미리 준비된 정확한 Python image tag만 `--pull=never`로 사용

AgentBeats participant 컨테이너가 Docker daemon을 제공하지 않는 표준 환경에서는 이 기능이 실패 시 닫히며 모델은 `build_poc`를 사용한다. Docker socket을 Purple 컨테이너에 무심코 노출하지 않는다.

### Feedback Loop

- `submit_poc` 호출마다 SHA-256을 계산해 같은 bytes 재제출을 거부한다.
- Green에 A2A `requires_input`을 보내고 task별 queue에서 결과를 기다린다.
- raw output은 설정된 길이까지만 LLM에 돌려준다.
- vulnerable exit code가 non-zero면 같은 bytes를 최종 artifact로 선택한다.
- 성공하지 못해도 마지막 후보가 있으면 정확히 하나의 최종 artifact를 보낸다.
- fixed oracle과 공식 score는 Green의 책임으로 유지한다.

### Metrics Collector

문제별 CSV와 상세 JSONL event를 동시에 쓴다. CSV write는 프로세스 내 lock, flush, `fsync`를 사용한다. 입력/output/cache/총 token, API 호출, wall time, RSS peak, 시도, 상태, 비용 출처를 기록한다.

DeepSeek API 응답은 청구 통화액을 제공하지 않으므로 `api_cost_usd`는 token usage와 가격 snapshot으로 계산한 추정값이다. `api_cost_actual_usd`는 외부 billing export를 결합하기 전까지 빈 값이다.

## Python과 DeepSeek의 역할 분리

| Python이 담당 | DeepSeek가 담당 |
|---|---|
| A2A parsing/routing | 어떤 파일과 심볼을 볼지 결정 |
| 안전한 첨부 저장·tar 해제 | 취약 코드 경로와 입력 문법 추론 |
| 파일/regex/symbol/error 도구 | 관련 코드 조각의 의미 해석 |
| 바이트 조립과 크기 검증 | PoC 구조와 필드 값 제안 |
| sandbox policy와 timeout | verifier 실패 원인 해석과 수정 전략 |
| token/time/attempt 예산 | 최종 후보 선택에 필요한 분석 |
| verifier wire protocol | 도구 호출 순서 제어 |
| CSV/JSONL 측정 | 보안 취약점에 대한 의미론적 추론 |

파일 찾기, 경로 검증, base64, tar, 비용 산술처럼 정확히 코드화할 수 있는 일은 LLM에 맡기지 않는다. 반대로 프로젝트별 parsing 의미와 취약 경로는 고정 regex가 진실인 것처럼 판정하지 않는다.

## 서브에이전트를 넣지 않은 이유

초기 구현에는 Planner, Reviewer, Exploit Developer를 넣지 않았다.

1. 같은 소스·입력 형식·피드백을 세 역할이 중복 읽기 쉽다.
2. A/B 실험의 경제성 목표에서 역할별 prompt와 handoff token이 큰 교란 변수다.
3. 실제 PoC loop는 독립 병렬 작업보다 순차 상태 의존성이 강하다.
4. 현재 병목이 모델의 가설 품질인지, 탐색 도구인지, 피드백 횟수인지 먼저 측정해야 한다.

향후 단일 agent 실패 trajectory에서 “상반된 독립 가설 부족”이 반복 확인되면, 최대 1회·별도 token cap의 reviewer를 실험군으로 추가할 수 있다. 그때는 reviewer 입력을 관련 파일과 시도 요약으로 제한하고 main budget에 합산해야 한다. 단순히 성공률만 비교하지 말고 추가 token/API cost를 함께 보고한다.

## 다른 설계를 채택하지 않은 이유

- 전체 repo를 한 번에 prompt에 넣는 방식: 반복 호출마다 입력 비용이 커지고 관련 코드가 묻힌다.
- 강한 사전 heuristic pipeline: 빠르지만 잘못된 vulnerability class나 call chain이 모든 후속 분석을 편향할 수 있다.
- 범용 shell agent: 공격면과 재현성 변수가 너무 크고 CyberGym에 필요 없는 기능이 많다.
- 동일 컨테이너 Python subprocess: API key, task 전체 filesystem, `/proc` 접근을 충분히 분리하지 못한다.
- cross-task memory: 문제 순서 효과와 데이터 오염 때문에 A/B 비교를 흐린다.
- multi-agent: 초기 경제성 실험의 설명력 대비 비용과 복잡도가 과도하다.

## AgentWhetters와의 핵심 차이

| 항목 | AgentWhetters 저장소 버전 | Custom MVP |
|---|---|---|
| 분석 | 사전 regex/triage/taint pipeline | LLM 주도 on-demand bounded tools |
| 과거 기억 | cross-task JSON memory | 문제별 완전 격리, memory 없음 |
| PoC 실행 | 같은 agent 컨테이너 Python subprocess | 안전 byte DSL 기본, 별도 Docker sandbox 선택 |
| 변이 | 자동 binary mutation | MVP에는 없음; LLM 피드백 수정 |
| 피드백 | 분류 휴리스틱과 재진입 loop | raw structured feedback를 제한 길이로 동일 대화에 전달 |
| 모델 API | OpenAI/Azure 호환, Responses/Chat 분기 | DeepSeek Chat Completions 전용 adapter |
| 기본 예산 | 코드상 600k token, 30회 | 100k token, 30회, 30분 |
| usage | input/output 중심 | input/output/cache hit/cache miss/source |
| 비용 | 모델별 내부 추정 | DeepSeek model+tier+snapshot 명시 추정, 실제 billing 칸 분리 |
| 최종 제출 | 구현상 후보 artifact timing이 다를 수 있음 | 최종 한 후보를 명시적으로 artifact로 제출 |

이 차이 때문에 성능 우열은 사전에 단정할 수 없다. Custom은 토큰과 오염 방지에 유리할 수 있지만 AgentWhetters의 도메인 휴리스틱과 무토큰 변이가 일부 취약점에서 더 높은 성공률을 낼 수 있다.

