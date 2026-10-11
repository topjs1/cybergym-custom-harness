# 세션 진행 로그 — CyberGym Custom Harness

작성일: 2026-10-12. 이 문서는 이번 세션에서 수행한 작업의 발표/피드백용 기록이다.

## 목표

CyberGym 평가용 Purple Agent 하네스를 **점수(재현 성공률) 우선**으로 개선하고,
그 과정의 토큰·비용·시간을 저장해 추후 경제성 비교에 쓰는 것.

## 한 줄 요약

A2A 기반 Purple 하네스에 **격리된 동적분석(sidecar)** + **정적 triage** +
**결정적 변이** + **RECON/REVIEWER 서브에이전트** + **K=2 앙상블** + **긴 PoC 대응**을
구현했고, **테스트 48개 통과**. 아직 실제 DeepSeek/Docker/타깃에는 닿지 않음(파일럿 준비 완료).

## 산출물 요약

| 영역 | 파일 | 상태 |
|---|---|---|
| 동적분석 격리 | `src/cybergym_harness/dynamic/{policy,runner,backend,tools,analysis}.py` | 구현 |
| 동적 sidecar | `sidecar/{app.py,Dockerfile}`, `docker-compose.override.yml` | 구현 |
| 정적 triage | `src/cybergym_harness/static_triage.py` | 구현 |
| 변이 | `src/cybergym_harness/mutation.py` | 구현 |
| 피드백 분류 | `src/cybergym_harness/feedback.py` | 구현 |
| 서브에이전트 | `src/cybergym_harness/subagents/{runner,recon,reviewer}.py` | 구현 |
| 앙상블/컨트롤러 | `src/cybergym_harness/controller.py` (트랙 구조) | 구현 |
| PoC DSL | `src/cybergym_harness/sandbox.py` (pad/repeat_seq/u8..u64/crc32/patch) | 구현 |
| 메트릭 | `src/cybergym_harness/metrics.py` (role/phase, hard cost cap) | 구현 |
| 파일럿 도구 | `tools/deepseek_pilot.py` | 구현 |
| 설계문서 | `DYNAMIC_ANALYSIS_DESIGN.md`, `README.md`, `.env.example` | 갱신 |

## 구현 단계

- **Phase 0** 메트릭 role/phase 태깅 + USD 하드캡
- **Phase 1** 동적분석 코어(policy/runner/backend/tools) + sidecar + compose
- **Phase 2** 정적 triage(파일랭킹·entry·ASan 파싱·도메인·magic·디렉터리 트리)
- **Phase 3** near-miss 결정적 변이
- **Phase 4** RECON 서브에이전트(시작 1회)
- **Phase 5** REVIEWER 서브에이전트(stall 시)
- **P1** K=2 앙상블(정적 트랙 + 동적/포맷 트랙, 공유 submit 채널)
- **P2** 긴 PoC 대응(build_poc DSL 확장)
- **P3** 조기 종료 방지(resume nudge)
- **P4** 컨텍스트 효율(디렉터리 트리 사전 주입)

## 격리 원칙 (핵심)

- Purple 컨테이너에는 **Docker socket 없음**. socket은 Green과 `dynamic-sandbox` sidecar에만.
- sidecar: API key 없음, egress 없음, private compose network만.
- builder/target 컨테이너: `--network=none`(빌드 예외 opt-in), `--read-only`,
  `--cap-drop=ALL`, `no-new-privileges`, 비-root, 자원 제한.
- 퍼징 전 누출 strip: `.git`, `patch.diff`, `tmp/poc`.
- 로컬 크래시 ≠ 공식 성공. 최종 판정은 항상 Green.

## 공개 자료 조사 근거

- CyberGym ICLR 2026 (arXiv:2506.02548): 최고 조합 ~18–22%, 모델 앙상블 union 27.2%,
  실패 모드 조기 종료 ~30%·긴 평문 PoC ~20%, **PoC >100바이트가 65.7%인데 성공률 ~10%**.
- 공식 예시 에이전트(`sunblaze-ucb/cybergym-agent-examples`): OpenHands/Codex/EnIGMA/Cybench.
- SUBMISSION.md: 비용/토큰 리포트 스키마.
- 격리는 환경 누출만 막고 **LLM 사전학습 지식은 못 막음**(논문 contamination 분석 p>0.1).

## 테스트

`uv run pytest -q` → **48 passed** (기본 루프, 동적 격리/정책, 변이, 피드백, 서브에이전트,
앙상블, PoC DSL, 비용캡, 파일럿 TCP 왕복).

## A1a 실제 DeepSeek 검증 결과 (2026-10-12)

stub verifier + 합성 챌린지로 실제 DeepSeek(`deepseek-flash`, thinking enabled) 구동.

| 시나리오 | 결과 | attempts | api_calls | tokens | est_usd | time |
|---|---|---|---|---|---|---|
| stub=always | PASS | 1 | 5 | 11,407 | $0.0007 | 6.4s |
| stub=nth=2 (피드백 루프) | PASS | 2 | 8 | 38,138 | $0.0061 | 45.7s |
| tracks=2 앙상블 (nth=2) | PASS | 2 | 9 | 19,705 | $0.0013 | 5.9s |

- **확인된 것**: DeepSeek `thinking + tools` 호환, tool-calling, `build_poc`/`submit_poc`,
  verifier 피드백 루프, 실패 후 **모델이 스스로 재분석**(analyze_error/search_code)하고
  69바이트 구조적 PoC로 2차 성공, 앙상블 트랙 role 분리(`solver`/`solver2`), 캐시 히트 동작.
- **발견/수정한 버그**: 모델이 `submit_poc`에 `path="generated/poc"`처럼 접두어를 붙이면
  `generated/generated/poc`로 조회해 실패 → 경로 접두어 정규화로 수정(회귀 테스트 추가).
- 누적 비용 ~$0.009 (하드캡 $0.5 내). 산출물: `data/pilot-<ts>/{results.csv,events.jsonl}`.

## 동적분석(Docker) 실검증 결과 (2026-10-12)

`tools/dynamic_smoke.py`로 실제 `DockerDynamicBackend`를 합성 C 유사 타깃(`ubuntu:22.04`,
로컬 보유 이미지, 다운로드 0)으로 검증. LLM/CyberGym 데이터 불필요.

- 결과: **PASS** — `build_target` 성공, `run_target`(benign exit 0 / MAGIC exit 139), `triage`,
  누출 strip(`.git`, `patch.diff` 제거) 확인.
- **발견·수정한 버그**: `--mount`에 `,rw`(비정상)·`,ro`(비정상) 사용 → 실제 docker에서
  `invalid field 'rw'`. FakeRunner 테스트로는 못 잡던 문제. rw는 플래그 제거, ro는
  `,readonly`로 수정(`dynamic/backend.py`, `sandbox.py`). 회귀 테스트 추가.

## 현재 상태 / 블로커

- 구현·단위·통합(mock) 검증 완료. **실제 DeepSeek 구동 검증 완료(A1a).**
- **미검증**: 실제 Docker 빌드/퍼징(sidecar), 실제 CyberGym `reproduced`(A1b),
  native CyberGym 어댑터.
- A1b에는 이미지 다운로드/x86_64 runner 승인 필요.

## 파일럿 실행법 (A1a, 실제 타깃 없이)

```bash
cd cybergym-custom-harness
read -s DEEPSEEK_API_KEY && export DEEPSEEK_API_KEY
UV_CACHE_DIR="$PWD/.uv-cache" uv run python tools/deepseek_pilot.py \
  --tcp --stub nth=3 --model deepseek-flash --token-budget 100000 --max-attempts 30 --cost-cap-usd 1
unset DEEPSEEK_API_KEY
```

결과: `data/pilot-<ts>/results.csv`, `events.jsonl` + 콘솔 요약.

## 다음 단계

1. A1b 실제 타깃 1문제(이미지/데이터 다운로드 승인 필요) — 실제 `reproduced` 검증
2. fuzz_target 실검증(clang/OSS-Fuzz 이미지 다운로드 승인 필요)
3. native CyberGym 어댑터(로컬 `submit.sh`/실행파일)
4. P5 도메인/시맨틱 도구

완료: P6 SUBMISSION 스키마 요약(`tools/summarize_results.py --submission`).

## Phase 0 배포 완료 (2026-10-12)

- 32개 변경분 커밋 `6d76413` → `origin/main` push.
- CI(`Test and Publish Agent`) 전 단계 성공: 테스트 → amber check → linux/amd64 빌드 →
  스모크 → **GHCR 재푸시**.
- GHCR `ghcr.io/topjs1/cybergym-custom-harness:latest` **public** 확인(anonymous pull 200).
- `amber-manifest.json5`에 조정 노브 추가: `max_cost_usd=0.5`, `tracks=1`,
  `recon_subagent=true`, `reviewer_subagent=false`, `mutation_attempts=0`, `resume_nudges=4`,
  `HARNESS_DYNAMIC_ANALYSIS=off`(AgentBeats는 Purple에 docker 없음).
- 실행 가이드 `AGENTBEATS_RUN.md` 추가(등록 절차 + 리더보드 scenario.toml + Quick Submit).

### 다음: AgentBeats 등록·실행 (사용자 브라우저 작업)
1. agentbeats.dev에 Purple 등록 → `agentbeats_id` 확보.
2. `RDI-Foundation/cybergym-leaderboard` fork → `scenario.toml`에 green id + 우리 id +
   `tasks`(subset 10) + `level1`+`num_workers=2`+`cleanup_images=true`.
3. Quick Submit(`num_shards` 예: 5) → Actions → 결과 집계.

## 백업/복원

- 코드 백업: 상위 디렉터리의 `cybergym-custom-harness-backup-<ts>.tar.gz`
- git 이력: 이 디렉터리는 git 저장소(현재 변경분은 미커밋). 커밋하려면 별도 요청.
- 대화 로그: opencode `/export`(ctrl+x x)로 마크다운 추출.
