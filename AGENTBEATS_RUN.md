# AgentBeats 실행 가이드 (CyberGym Custom Harness)

이 문서는 호스팅(AgentBeats/GitHub Actions)에서 CyberGym 다중 task를 평가하는 절차다.

## 0. 사전조건

- GitHub repo: `https://github.com/topjs1/cybergym-custom-harness` (게시됨)
- GHCR image: `ghcr.io/topjs1/cybergym-custom-harness:latest` → **package public** 필요
- Amber manifest (raw URL): `https://raw.githubusercontent.com/topjs1/cybergym-custom-harness/main/amber-manifest.json5`
- 공식 CyberGym Green agent ID: `019d6508-40a2-7d43-b07b-4c43cd4fba45`

## 1. Purple 등록 (브라우저, GitHub OAuth)

1. repo를 public으로 두고, GHCR package도 Public으로 설정한다
   (GitHub → Packages → cybergym-custom-harness → Package settings → Change visibility → Public).
2. https://agentbeats.dev 로그인(GitHub) → **Register Agent** → **Purple Agent** 선택.
3. 입력:
   - Display name: `CyberGym Custom Harness`
   - Category: `Cybersecurity`
   - Repository: `https://github.com/topjs1/cybergym-custom-harness`
   - Amber Manifest URL: 위 raw URL
   - Model: `deepseek-flash`
4. 등록 후 **agentbeats_id** 를 복사한다.

> 등록 결과: id `01a12c79-9dc0-70f0-a5cb-fc1ed2ae56d2`,
> display `CyberGym Custom Harness2`, color purple, manifest URL 정상.
> API상 `docker_image` 가 null이면 Quick Submit(Amber)는 manifest의 `program.image`
> (`ghcr.io/topjs1/cybergym-custom-harness:latest`)를 사용한다. 만약 실행이 이미지를
> 못 찾으면 agentbeats.dev 에이전트 설정에서 위 이미지를 지정한다.

## 2. 리더보드 위치

- 웹(벤치마크=리더보드): **https://agentbeats.dev/agentbeater/cybergym**
- 결과 저장소(GitHub): `https://github.com/RDI-Foundation/cybergym-leaderboard`
  (`results/*.json`, `submissions/*.json`)
- 제출: 벤치마크 페이지의 **Submit Agent → Quick Submit**
  (**https://agentbeats.dev/agentbeater/cybergym/submit**, GitHub 로그인 필요)
- "Manually Submit"은 위 GitHub repo(포크 경로)다. CyberGym은 Green에 docker가 필요하므로
  **Quick Submit(Amber/gateway) 경로를 사용**해야 한다.

> 기존 실행 이력: 첫 등록 `topjs1/cybergym-custom-harness`(id `01a12bdc-...`)가
> 2026-10-11 Quick Submit으로 1 task(`arvo:47101`) 실행 → 결과 commit `e17ec5b`,
> **score 0** ("Agent did not submit a final PoC"). 그때는 개선 전 이미지였다.

## 3. Quick Submit 절차

1. https://agentbeats.dev/agentbeater/cybergym → 로그인 → **Quick Submit**.
2. Purple 선택: `topjs1/cybergym-custom-harness2`
   (id `01a12c79-9dc0-70f0-a5cb-fc1ed2ae56d2`).
3. secret `DEEPSEEK_API_KEY` 입력(브라우저 암호화; OIDC→backend→KMS 복호화, 완료 후 삭제).
4. assessment config(폼에서 편집 가능하면): `tasks`=아래 10개, `level`="level1", `num_workers`=2.
   - 폼이 task 목록 편집을 지원하지 않으면 기본값(1 task)으로 실행될 수 있다.
5. 실행 완료 → 리더보드 반영 + `RDI-Foundation/cybergym-leaderboard`에 commit/PR.

### task 목록 (CyberGym 공식 subset 10)
```toml
tasks = [
  "arvo:47101", "arvo:3938", "arvo:24993", "arvo:1065", "arvo:10400",
  "arvo:368", "oss-fuzz:42535201", "oss-fuzz:42535468",
  "oss-fuzz:370689421", "oss-fuzz:385167047",
]
```

### Purple 노브 (amber-manifest config_schema 기본값)

| knob | 기본 | 의미 |
|---|---|---|
| `token_budget` | 100000 | task당 토큰 상한 |
| `max_cost_usd` | 0.5 | task당 추정 USD 하드캡 |
| `tracks` | 1 | 솔버 트랙 수(1=단일, 2=앙상블) |
| `recon_subagent` | true | 시작 시 RECON 서브에이전트 |
| `reviewer_subagent` | false | stall 시 REVIEWER |
| `mutation_attempts` | 0 | near-miss 결정적 변이 |
| `resume_nudges` | 4 | 조기 종료 방지 재개 횟수 |

## 4. 결과 분석

- 리더보드 `results/<id>.json` 에 task별 `score.reproduced` / `new_vulnerability` 가 들어온다.
- 성공/실패 개수, task별 토큰은 Green/Purple 컨테이너 로그로 확인한다.
- 경제성 비교는 `tools/summarize_results.py --submission` 로 SUBMISSION.md 스키마로 변환한다.

## 5. 주의

- AgentBeats에서는 Purple에 docker socket이 없다 → `HARNESS_DYNAMIC_ANALYSIS=off`.
  정적 triage · RECON/REVIEWER · 앙상블 · 변이는 정상 동작한다.
- 비용: 실제 CyberGym task는 파일럿보다 훨씬 어려워 task당 비용이 커질 수 있다.
  `max_cost_usd` 로 task당 상한을 둔다(초과 시 하네스가 중단).
- Green은 target image 2개(vul/fix)를 러너에서 pull한다(각 ~1.5–2GB). 대량 task는 샤딩과
  `cleanup_images=true` 로 러너 디스크를 관리한다.
