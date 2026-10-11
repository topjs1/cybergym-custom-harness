# AgentBeats 등록 체크리스트

확인일: 2026-10-12 (Asia/Seoul)

## 어떤 에이전트를 등록하는가

- 원본 `AgentWhetters_CyberGym_Purple`은 이미 AgentBeats에 등록돼 있다.
- 이 저장소의 `CyberGym Custom Harness`는 별도 구현이므로, 공개 CyberGym 평가와 leaderboard 제출에 사용하려면 별도 Purple Agent로 등록해야 한다.
- 로컬 개발과 manual local compose 검증에는 등록이 필요 없다.

## 등록 전에 필요한 것

1. 이 디렉터리를 공개 GitHub 저장소 `topjs1/cybergym-custom-harness`로 게시한다.
2. `main` push로 `.github/workflows/test-and-publish.yml`을 실행한다.
3. GitHub Actions가 테스트, Amber check, Linux/AMD64 build와 container roundtrip을 통과했는지 확인한다.
4. `ghcr.io/topjs1/cybergym-custom-harness:latest` package를 public으로 공개한다.
5. 다음 raw manifest URL이 로그인 없이 열리는지 확인한다.

```text
https://raw.githubusercontent.com/topjs1/cybergym-custom-harness/main/amber-manifest.json5
```

## 등록 폼

`https://agentbeats.dev/register-agent`는 GitHub OAuth 로그인이 필요하다. Purple Agent를 선택하고 아래 정보를 사용한다.

- Display name: `CyberGym Custom Harness`
- Category: `Cybersecurity`
- Repository: `https://github.com/topjs1/cybergym-custom-harness`
- Amber Manifest URL: 위 raw GitHub URL
- Model: 실제 평가에 사용할 DeepSeek model
- 설명: 독립적이고 budget 제한이 있는 CyberGym PoC 생성 Purple Agent

등록이 끝나면 AgentBeats 페이지의 `Copy agent ID`로 ID를 보관한다. Quick Submit이나 GitHub Actions 기반 leaderboard 제출에는 이 ID가 필요하다.

## Secret

DeepSeek key는 저장소나 manifest에 직접 넣지 않는다. Quick Submit에서는 `deepseek_api_key` secret field로 전달한다. AgentBeats 문서상 Quick Submit secret은 browser에서 암호화되고, 승인된 workflow가 OIDC로 받아 사용하며 60분 후 만료된다.
