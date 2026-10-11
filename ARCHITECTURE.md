# 아키텍처와 워크플로우

## 시스템 경계

```mermaid
flowchart LR
    AB[AgentBeats Client] -->|EvalRequest| G[CyberGym Green Agent]
    G -->|A2A Message<br/>TextPart + FileWithBytes| S[A2A Server]
    S --> E[Executor]
    E --> TM[Task Manager<br/>context별 session]
    TM --> AX[Safe Attachment Extractor]
    AX --> WS[(Per-task Workspace)]
    TM --> LC[Main DeepSeek Controller]
    LC --> FT[File / Search / Symbol / Error Tools]
    FT --> WS
    LC --> BP[Structured Byte Builder]
    LC -. optional .-> PS[Isolated Python Container]
    BP --> P[(PoC bytes)]
    PS --> P
    LC -->|requires_input<br/>test_vulnerable + FilePart| G
    G -->|DataPart<br/>exit_code + output| TM
    TM --> LC
    LC -->|one final FilePart artifact| G
    G -->|vulnerable + fixed oracle| R[Official task_results / score]
    TM --> MC[Metrics Collector]
    MC --> CSV[(results.csv)]
    MC --> JSONL[(events.jsonl)]
```

Green의 target 실행과 공식 dual-oracle 판정은 하네스 경계 밖이다. Custom은 그 기능을 복제하지 않는다.

## 한 task의 상태 전이

```mermaid
stateDiagram-v2
    [*] --> RECEIVED
    RECEIVED --> PREPARING: validate + decode + safe extract
    PREPARING --> ANALYZING: initialize isolated context
    ANALYZING --> GENERATING: tool calls / hypothesis
    GENERATING --> WAITING_FEEDBACK: submit_poc
    WAITING_FEEDBACK --> ANALYZING: exit_code = 0 or error
    WAITING_FEEDBACK --> FINALIZING: exit_code != 0
    ANALYZING --> FINALIZING: budget/attempt exhausted and candidate exists
    ANALYZING --> FAIL: no candidate
    PREPARING --> ERROR: invalid attachment/archive
    ANALYZING --> TIMEOUT: 1800s deadline
    FINALIZING --> PASS: local vulnerable check succeeded
    FINALIZING --> FAIL: no local success
    PASS --> [*]
    FAIL --> [*]
    ERROR --> [*]
    TIMEOUT --> [*]
```

`PASS`는 Purple이 관측한 vulnerable test 결과이다. 최종 공식 `reproduced` 여부는 이후 Green의 fixed 비교로 확정된다.

## A2A sequence

```mermaid
sequenceDiagram
    participant G as CyberGym Green
    participant A as Custom A2A Server
    participant T as Task Manager
    participant L as DeepSeek
    participant V as Green Verifier

    G->>A: initial Message(TextPart, README, repo-vul, ...)
    A->>T: new context/session
    T->>T: safe extract + budgets + metrics start
    loop bounded analysis and attempts
        T->>L: prompt + retained tool history
        L-->>T: tool_calls
        T->>T: list/read/search/build bytes
        T->>A: requires_input(DataPart action + PoC FilePart)
        A-->>G: TaskStatusUpdateEvent
        G->>V: run PoC on vulnerable image
        V-->>G: exit_code/output
        G->>A: follow-up Message(DataPart result, same context)
        A->>T: feedback queue
        T->>L: bounded verifier result
    end
    T->>A: add_artifact(final PoC FilePart)
    A-->>G: TaskArtifactUpdateEvent
    G->>V: run final PoC on vulnerable and fixed images
    V-->>G: official result
```

## 신뢰 경계

```mermaid
flowchart TB
    U[Untrusted inputs<br/>A2A files, tar names, LLM JSON, generated code]
    U --> C1[Size/type/path validation]
    C1 --> C2[Per-task workspace]
    U --> C3[Tool argument validation]
    C3 --> B[Non-executable byte DSL]
    C3 -. enabled only when configured .-> D[Docker sandbox<br/>no network, no secrets, non-root]
    B --> O[Bounded PoC]
    D --> O
    O --> G[Green verifier]
```

`run_python`의 Docker backend는 source나 API key를 mount/pass하지 않는다. Docker daemon이 없거나 image가 미리 준비되지 않았으면 실행하지 않고 오류를 반환한다.

## 데이터 보존

- task source와 generated PoC: `HARNESS_WORK_ROOT/<context-hash>/`
- 비교 CSV: `HARNESS_RESULTS_CSV`
- 상세 event: `HARNESS_EVENTS_JSONL`
- in-memory A2A history: 첨부 원문을 보존하지 않는 custom task store
- cross-task model memory: 없음

