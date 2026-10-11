# AgentWhetters 대 Custom 비교 계획

## 연구 질문

1. 같은 DeepSeek와 예산에서 Custom의 on-demand 도구 구조가 AgentWhetters의 사전 휴리스틱 구조보다 token과 비용을 줄이는가?
2. token 감소가 PoC final-submission 성공률을 희생하는가?
3. 성공 1건당 비용과 wall time은 어느 하네스가 낮은가?
4. 취약점 유형·repo 크기·실패 횟수에 따라 차이가 어떻게 달라지는가?

## 통제 변수

| 변수 | 고정값/방법 |
|---|---|
| task | 같은 명시적 ID list, 같은 순서 |
| 난이도 | level1 |
| model | `deepseek-flash`; 응답의 실제 model/version도 event에 보존 권장 |
| API endpoint | `https://api.deepseek.com` |
| thinking | enabled. AgentWhetters가 해당 필드를 생략해 받는 공식 기본값과 Custom을 맞춤 |
| temperature | AgentWhetters는 0.0을 보냄; thinking mode에서는 공식 문서상 효과 없음. Custom도 별도 sampling 차이를 만들지 않음 |
| max output | 4,096 tokens/call |
| task token budget | 100,000 input+output reported tokens |
| task wall time | 1,800초, 두 하네스 모두 외부 runner에서도 강제 |
| verifier attempts | 최대 30 |
| Green | 같은 image digest/commit |
| target images/data | 같은 local cache와 digest |
| worker | 1; task ID 상관관계와 resource contention 제거 |
| CPU/memory | 같은 `--cpus`, `--memory`, pids limit |
| network | 같은 Docker network/firewall/DeepSeek allowlist |
| 시작 상태 | 문제마다 새 agent container와 빈 writable volume |

### 동일하게 만들 수 없는 차이

- AgentWhetters의 코드상 MAX_ATTEMPTS=30과 Custom의 환경변수 30은 동일하게 맞출 수 있다.
- AgentWhetters에는 내부 전체 wall timeout이 없으므로 외부 container/job timeout으로 맞춰야 한다.
- AgentWhetters는 cross-task memory를 쓰므로 문제마다 새 빈 `MEMORY_DIR`와 새 container를 사용해야 한다. 그렇지 않으면 Custom의 stateless 조건과 비교할 수 없다.
- 두 image가 포함하는 분석 binary가 다를 수 있다. image digest와 설치 tool 목록을 보고서에 기록한다.
- AgentWhetters의 현재 token tracker는 cached input을 완전히 보존하지 않으며 task별 CSV write가 없다. 디렉터리를 변경하지 않는 조건을 지키려면 두 하네스 앞에 동일한 local OpenAI-compatible usage recorder를 두거나, provider billing/response log를 외부에서 결합해야 한다. 이 recorder가 준비되기 전 cached token 비교는 실행하지 않는다.

## task set

1. 환경 검증용 공식 subset 중 1개를 유료 호출 없이 mock으로 먼저 통과시킨다.
2. 승인 후 5~10개 level1 pilot을 paired run으로 실행한다.
3. pilot에서 timeout, context, target image 문제를 해결한 뒤 사전에 고정한 전체 표본을 실행한다.
4. task 목록, 순서, 제외 기준을 실행 전에 checksum과 함께 동결한다.

성공하기 쉬운 문제만 사후 선택하지 않는다. 실패한 환경 문제와 모델 실패를 구분하되 둘 다 원본 row로 남긴다.

## 반복과 순서

모델 비결정성을 고려하면 가능하면 task/harness 조합당 3회 반복한다. 비용 제약이 있으면 1회 paired pilot 후 전체 1회, 중요한 차이만 추가 반복한다. 순서 효과를 줄이기 위해 AB/BA를 seeded block randomization하되, 각 문제는 항상 깨끗한 container로 시작한다.

## 공통 CSV schema

`results.csv`의 필수 열은 요청된 13개이고, provenance를 위해 추가 열을 둔다.

```text
harness,task_id,model,input_tokens,output_tokens,cached_tokens,total_tokens,
api_calls,time_seconds,api_cost_usd,peak_memory_mb,attempts,status,
task_id_source,input_cache_miss_tokens,usage_source,api_cost_actual_usd,
api_cost_source,pricing_tier,pricing_snapshot,error
```

상태 규칙:

- `PASS`: 공식 Green final score에서 `reproduced=1`을 원칙으로 사용한다.
- `FAIL`: 실행 완료, final reproduced 0.
- `TIMEOUT`: 외부 또는 내부 1,800초 cap.
- `ERROR`: infrastructure/API/protocol 문제로 평가 미완료.

Custom이 자체 기록한 vulnerable-test PASS는 Green 결과 결합 전 임시 값이다. 최종 분석 데이터셋에서는 Green의 권위 있는 score로 덮어쓴다.

## 비용 처리

DeepSeek 응답의 cache hit/miss/input/output token을 우선 사용한다. 비용은 해당 요청 시점의 model과 peak/off-peak 가격을 곱해 추정한다. API 응답에 청구 통화액이 없으므로 다음을 분리한다.

- `api_cost_usd`: token 기반 추정
- `api_cost_source=estimated_from_token_usage`
- `api_cost_actual_usd`: billing export와 task attribution이 가능할 때만 채움

price가 바뀌면 실행일 가격으로 각 run을 계산하고 snapshot을 보존한다. 과거 run을 새 가격으로 조용히 다시 계산하지 않는다.

## 분석 지표

- final-submission 성공률 = PASS / 전체 평가 가능 task
- 평균/중앙 input, output, cached, total token
- 평균/중앙 wall time과 API call 수
- 총 API 비용과 문제당 평균 비용
- 성공 1건당 비용 = 전체 비용 / PASS 수
- attempts 분포
- status별 비율과 infrastructure error rate

paired task 결과에는 McNemar test 또는 paired bootstrap confidence interval을 사용하고, token/time/cost는 median 차이와 bootstrap CI를 함께 낸다. 표본이 작을 때 p-value만으로 우열을 선언하지 않는다.

## 실행 전 체크리스트

- [ ] socket/port가 허용된 host에서 Custom live A2A roundtrip PASS
- [ ] Custom Docker build 및 non-root startup PASS
- [ ] 공식 Green image와 target 1개가 사전 다운로드되어 있음
- [ ] Green이 private Docker network gateway에만 bind됨
- [ ] 두 하네스 image digest 기록
- [ ] DeepSeek model/parameter/price tier 기록
- [ ] AgentWhetters memory가 매 task 초기화됨
- [ ] 동일 외부 1,800초 timeout과 resource limit 적용
- [ ] task ID 상관관계 방법 확정
- [ ] AgentWhetters cached token 수집 경로 확정
- [ ] 유료 호출 승인과 최대 비용 상한 확정

## 집계

두 CSV가 준비되면 다음으로 기본 지표를 낸다.

```bash
python tools/summarize_results.py agentwhetters-results.csv custom-results.csv
```

이 도구는 success rate, 평균 token, 평균 time, 총 비용, 성공 1건당 비용을 하네스별로 출력한다.

