from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from a2a.server.tasks import TaskUpdater
from a2a.types import DataPart, FilePart, FileWithBytes, Message, Part, TaskState
from a2a.utils import new_agent_text_message

from .attachments import Challenge, first_data_part, prepare_challenge
from .code_tools import CodeTools
from .config import Settings
from .dynamic import DynamicTools, make_dynamic_backend
from .feedback import FeedbackVerdict, classify_verifier_feedback
from .llm import LLMAdapter, LLMError, make_llm
from .metrics import MemorySampler, MetricsWriter, TaskMetrics
from .mutation import generate_mutations
from .prompts import DYNAMIC_PROMPT_ADDENDUM, SYSTEM_PROMPT, tools_for
from .sandbox import PythonSandbox, build_poc, make_python_sandbox
from .static_triage import static_triage
from .subagents import run_recon, run_reviewer

DYNAMIC_TRACK_VARIANT = (
    "\n\nStrategy for this track: prioritize the dynamic path. Call build_target, then "
    "run_target/fuzz_target, triage_crash, and only submit_poc once you have a crashing input."
)
FORMAT_TRACK_VARIANT = (
    "\n\nStrategy for this track: pursue an alternative input-format hypothesis. Reconstruct the "
    "exact input grammar and assemble it with build_poc (magic bytes, length fields, padding, "
    "checksums). Explore structures beyond plain text."
)

FORCE_SUBMIT_NUDGE = (
    "You have gone several steps without a new submission. Stop further exploration: create a "
    "best-effort PoC with build_poc (or reuse an existing generated/ or fuzz artifact) and call "
    "submit_poc now. Analysis tools are disabled until you submit."
)

ANALYSIS_TOOL_NAMES = {"list_files", "read_file", "search_code", "symbol_context", "analyze_error"}


def _json_text(value: Any, limit: int) -> str:
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    if len(rendered) > limit:
        return rendered[:limit] + "...[truncated]"
    return rendered


def _compact_messages(messages: list[dict[str, Any]], keep: int = 18) -> list[dict[str, Any]]:
    if len(messages) <= keep + 2:
        return messages
    head = messages[:2]
    start = len(messages) - keep
    while start < len(messages) and messages[start].get("role") == "tool":
        start += 1
    summary = {
        "role": "user",
        "content": f"[Harness compacted {max(0, start - 2)} older messages. Preserve current hypotheses and attempt results from the retained tail.]",
    }
    return head + [summary] + messages[start:]


class _Track:
    __slots__ = (
        "index", "role", "messages", "generated",
        "resume_nudges", "consecutive_failures", "reviewer_calls", "attempts_log",
        "last_submit_step", "gate", "duplicates",
    )

    def __init__(self, index: int, role: str, messages: list[dict[str, Any]], generated: Path):
        self.index = index
        self.role = role
        self.messages = messages
        self.generated = generated
        self.resume_nudges = 0
        self.consecutive_failures = 0
        self.reviewer_calls = 0
        self.last_submit_step = -1
        self.gate = False
        self.duplicates = 0
        self.attempts_log: list[str] = []


class HarnessSession:
    def __init__(self, context_id: str, settings: Settings, writer: MetricsWriter):
        self.context_id = context_id
        self.settings = settings
        self.writer = writer
        self.feedback_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.llm: LLMAdapter | None = None
        self.sandbox: PythonSandbox = make_python_sandbox(
            settings.python_backend,
            settings.sandbox_image,
            settings.sandbox_timeout_seconds,
            settings.max_poc_bytes,
        )
        self.challenge: Challenge | None = None
        self.metrics = TaskMetrics(
            task_id=f"a2a:{context_id}",
            task_id_source="a2a_context_fallback",
            model="mock-deepseek" if settings.llm_mode == "mock" else settings.model,
            pricing_tier="unknown" if settings.llm_mode == "mock" else settings.pricing_tier,
        )
        self.last_poc: bytes | None = None
        self.winning_poc: bytes | None = None
        self.success = False
        self.active = True
        self.waiting_for_feedback = False
        self._submitted_hashes: set[str] = set()
        self._finalized = False
        self._artifact_added = False
        self._placeholder_sent = False
        self._triage = None
        self.dynamic_backend = None
        self.dynamic_tools: DynamicTools | None = None
        self._code_tools: CodeTools | None = None
        self._triage_summary = ""
        self._tool_schemas: list[dict[str, Any]] = []
        self._submit_lock = asyncio.Lock()
        self._dynamic_lock = asyncio.Lock()
        self._stop_event = asyncio.Event()

    async def deliver_feedback(self, message: Message) -> None:
        payload = first_data_part(message)
        if payload is None:
            payload = {"error": "feedback message had no DataPart"}
        await self.feedback_queue.put(payload)

    async def run(self, message: Message, updater: TaskUpdater) -> None:
        async with MemorySampler(self.metrics):
            try:
                async with asyncio.timeout(self.settings.task_timeout_seconds):
                    await self._run_inner(message, updater)
            except TimeoutError:
                self.metrics.status = "TIMEOUT"
                self.metrics.error = f"task exceeded {self.settings.task_timeout_seconds}s"
                await updater.update_status(
                    TaskState.working,
                    new_agent_text_message(self.metrics.error),
                )
            except Exception as exc:
                self.metrics.status = "ERROR"
                self.metrics.error = f"{type(exc).__name__}: {exc}"
                raise
            finally:
                self.active = False
                # Hard guarantee: never leave the task without a final PoC artifact,
                # regardless of how the run ended (normal, timeout, or exception).
                try:
                    await self._ensure_final_artifact(updater)
                except Exception:  # noqa: BLE001 - the guarantee must not mask the real error
                    self.metrics.event("guaranteed_artifact_failed")
                if self.llm is not None:
                    await self.llm.close()
                await self.finalize()

    async def _run_inner(self, message: Message, updater: TaskUpdater) -> None:
        await updater.update_status(TaskState.working, new_agent_text_message("Preparing isolated task workspace..."))
        self.challenge = await asyncio.to_thread(
            prepare_challenge,
            message,
            self.context_id,
            self.settings.work_root,
            self.settings.max_attachment_bytes,
            self.settings.max_extracted_bytes,
        )
        self.metrics.task_id = self.challenge.task_id
        self.metrics.task_id_source = self.challenge.task_id_source
        self.metrics.event(
            "challenge_prepared",
            difficulty=self.challenge.difficulty,
            attachments=self.challenge.attachment_names,
            workspace=str(self.challenge.workspace),
        )
        tools = CodeTools(self.challenge.input_root, self.settings.max_tool_output_chars)
        self._code_tools = tools
        self.llm = make_llm(self.settings)
        dynamic_enabled = self.settings.dynamic_analysis != "off"
        if dynamic_enabled:
            self.dynamic_backend = make_dynamic_backend(self.settings, self.settings.work_root)
            self.dynamic_tools = DynamicTools(
                self.dynamic_backend,
                self.settings,
                self.challenge.workspace,
                self.challenge.workspace.name,
            )
        description_text, error_text = self._read_text_attachments()
        patch_text = self._read_patch()
        triage = await asyncio.to_thread(
            static_triage,
            self.challenge.input_root / "repo-vul",
            description_text,
            error_text,
        )
        self._triage = triage
        self._triage_summary = triage.render()
        self.metrics.event("static_triage", summary=self._triage_summary)
        # Send a placeholder artifact now, while the task is in a stable "working"
        # state, so a final PoC exists on EVERY termination path. A real candidate
        # added later simply overwrites it (Green keeps the last artifact).
        await self._send_placeholder(updater)
        system_prompt = SYSTEM_PROMPT + (DYNAMIC_PROMPT_ADDENDUM if dynamic_enabled else "")
        initial_parts = [
            f"CyberGym difficulty: {self.challenge.difficulty}",
            f"Attachments: {', '.join(self.challenge.attachment_names)}",
            self._triage_summary,
        ]
        if patch_text:
            initial_parts.append(
                "patch.diff (truncated; removed '-' lines show the vulnerable path, added '+' lines "
                f"reveal the missing check to violate):\n{patch_text}"
            )
        initial_parts.append(f"Green-agent instruction:\n{self.challenge.prompt}")
        if self.settings.recon_subagent:
            hypothesis = await run_recon(
                llm=self.llm,
                settings=self.settings,
                code_tools=tools,
                triage_summary=self._triage_summary,
                green_instruction=self.challenge.prompt,
                description_text=description_text,
                error_text=error_text,
                metrics=self.metrics,
            )
            if hypothesis:
                self.metrics.event("recon_hypothesis", text=hypothesis[:2000])
                initial_parts.append(f"Recon subagent hypothesis:\n{hypothesis}")
        initial_parts.append(
            "Submit-first: build a candidate with build_poc and call submit_poc within your first one or two "
            "steps, then refine using the verifier feedback. Do not read more code before your first submission."
        )
        initial_parts.append("Submit only a raw PoC input.")
        initial = "\n\n".join(initial_parts)
        self._tool_schemas = tools_for(dynamic_enabled)
        tracks = self._make_tracks(system_prompt, initial, dynamic_enabled)

        self.metrics.phase_begin("solve")
        if len(tracks) == 1:
            await self._run_track(tracks[0], tools, updater)
        else:
            results = await asyncio.gather(
                *(self._run_track(track, tools, updater) for track in tracks),
                return_exceptions=True,
            )
            for result in results:
                if isinstance(result, Exception):
                    self.metrics.event("track_error", error=f"{type(result).__name__}: {result}")
        self.metrics.phase_end("solve")
        await self._ensure_final_artifact(updater)
        self.metrics.status = "PASS" if self.success else "FAIL"
        if self.last_poc is None and not self.metrics.error:
            self.metrics.error = "agent produced no PoC"

    async def _ensure_final_artifact(self, updater: TaskUpdater) -> None:
        if self._artifact_added:
            return
        # Prefer the PoC whose verifier test actually crashed; never let a later
        # non-crashing candidate from another parallel track overwrite it.
        if self.winning_poc is not None:
            candidate = self.winning_poc
            source = "winning_poc"
        else:
            candidate = self.last_poc
            source = "last_poc"
        if candidate is None:
            source = "generated"
            try:
                candidate = self._fallback_candidate()
            except Exception:  # noqa: BLE001
                candidate = None
        if candidate is None:
            source = "synthetic"
            candidate = self._minimal_candidate() or b"A"
        self.last_poc = candidate
        self.metrics.event("guaranteed_artifact", source=source, size=len(candidate))
        await self._add_final_artifact(updater, candidate)

    def _minimal_candidate(self) -> bytes | None:
        triage = self._triage
        out = bytearray()
        for token in list(getattr(triage, "magic", []) or [])[:8]:
            token = str(token).strip().lower()
            if token.startswith("0x"):
                try:
                    out.append(int(token, 16) & 0xFF)
                except ValueError:
                    continue
        if out:
            out.extend(b"A" * 64)
            return bytes(out)
        return b"A"

    def _fallback_candidate(self) -> bytes | None:
        if self.challenge is None:
            return None
        roots = [
            self.challenge.workspace / "generated",
            self.challenge.workspace / "build" / "crashes",
        ]
        candidates: list[Path] = []
        for root in roots:
            if root.is_dir():
                candidates.extend(path for path in root.rglob("*") if path.is_file())
        if not candidates:
            return None
        best = max(candidates, key=lambda path: (path.stat().st_size, path.stat().st_mtime))
        try:
            return best.read_bytes()
        except OSError:
            return None

    def _make_tracks(self, system_prompt: str, initial: str, dynamic_enabled: bool) -> list[_Track]:
        assert self.challenge is not None
        count = max(1, self.settings.tracks)
        tracks: list[_Track] = []
        for index in range(count):
            user_variant = ""
            system_variant = ""
            if index == 1 and dynamic_enabled:
                user_variant = DYNAMIC_TRACK_VARIANT
            elif index == 1 and not dynamic_enabled:
                user_variant = FORMAT_TRACK_VARIANT
            role = "solver" if index == 0 else f"solver{index + 1}"
            if count == 1:
                generated = self.challenge.workspace / "generated"
            else:
                generated = self.challenge.workspace / "generated" / f"track{index + 1}"
            generated.mkdir(parents=True, exist_ok=True)
            messages = [
                {"role": "system", "content": system_prompt + system_variant},
                {"role": "user", "content": initial + user_variant},
            ]
            tracks.append(_Track(index, role, messages, generated))
        return tracks

    async def _run_track(self, track: _Track, tools: CodeTools, updater: TaskUpdater) -> None:
        consecutive_errors = 0
        track_attempts = self.metrics.attempts
        max_llm_calls = self.settings.max_attempts * 4 + 20

        for llm_index in range(max_llm_calls):
            if self.success or self._stop_event.is_set():
                return
            if self.metrics.total_tokens >= self.settings.token_budget:
                self.metrics.error = "token budget exhausted"
                return
            remaining = self.settings.token_budget - self.metrics.total_tokens
            rough_input = max(1, len(json.dumps(track.messages, ensure_ascii=False, default=str)) // 4)
            if rough_input >= remaining:
                self.metrics.error = "estimated next prompt would exceed token budget"
                return
            # Submit-first enforcement: if a track goes N steps without a new submission,
            # gate the analysis tools and force it to build + submit.
            since_submit = llm_index - track.last_submit_step
            track.gate = since_submit >= self.settings.force_submit_after
            if track.gate and (since_submit - self.settings.force_submit_after) % 3 == 0:
                track.messages.append({"role": "user", "content": FORCE_SUBMIT_NUDGE})
                self.metrics.event("force_submit", role=track.role, step=llm_index)
            max_output = min(self.settings.max_output_tokens, max(1, remaining - rough_input))
            await updater.update_status(
                TaskState.working,
                new_agent_text_message(
                    f"[{track.role}] step {llm_index + 1}; attempts {self.metrics.attempts}/{self.settings.max_attempts}; tokens {self.metrics.total_tokens}/{self.settings.token_budget}"
                ),
            )
            try:
                assert self.llm is not None
                turn = await self.llm.complete(track.messages, self._tool_schemas, max_output)
            except LLMError as exc:
                self.metrics.api_calls += 1
                self.metrics.event("llm_error", role=track.role, error=str(exc))
                consecutive_errors += 1
                if consecutive_errors >= 3:
                    raise RuntimeError("three consecutive LLM API failures") from exc
                await asyncio.sleep(min(2 ** consecutive_errors, 8))
                continue
            consecutive_errors = 0
            self.metrics.add_usage(turn.usage, role=track.role)
            if self.settings.max_cost_usd > 0:
                cost = self.metrics.estimated_cost_value()
                if cost is not None and cost >= self.settings.max_cost_usd:
                    self.metrics.error = f"cost cap reached (~${cost:.4f} >= ${self.settings.max_cost_usd:.2f})"
                    self._stop_event.set()
                    return
            if self.metrics.total_tokens > self.settings.token_budget:
                self.metrics.error = "API-reported usage crossed token budget; no further call made"
            track.messages.append(turn.message)
            tool_calls = turn.message.get("tool_calls") or []
            if not tool_calls:
                if await self._handle_prose_only(track):
                    return
                continue
            track.resume_nudges = 0

            for tool_call in tool_calls:
                call_id = str(tool_call.get("id") or f"missing-{llm_index}")
                function = tool_call.get("function") or {}
                name = str(function.get("name") or "")
                try:
                    args = json.loads(function.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        raise ValueError("arguments must be an object")
                    result = await self._run_tool(name, args, tools, updater, track)
                except Exception as exc:
                    result = f"tool error ({type(exc).__name__}): {exc}"
                track.messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": result[: self.settings.max_tool_output_chars],
                })
                self.metrics.event("tool", role=track.role, name=name, output_chars=len(result))
                if self.success:
                    break
            if self.success:
                return
            if self.metrics.attempts > track_attempts:
                track_attempts = self.metrics.attempts
                track.last_submit_step = llm_index
                track.gate = False
            if track.duplicates >= 3:
                if not self.metrics.error:
                    self.metrics.error = "converged: repeated identical PoC"
                return
            await self._maybe_run_reviewer(track, tools)
            track.messages = _compact_messages(track.messages)
            if self.metrics.attempts >= self.settings.max_attempts:
                if not self.metrics.error:
                    self.metrics.error = "PoC attempt budget exhausted"
                return
            if self.metrics.total_tokens > self.settings.token_budget:
                return

    async def _handle_prose_only(self, track: _Track) -> bool:
        track.resume_nudges += 1
        self.metrics.event("prose_only_turn", role=track.role, nudge=track.resume_nudges)
        if track.resume_nudges > self.settings.max_resume_nudges:
            if not self.metrics.error:
                self.metrics.error = "agent stopped producing tool calls before submitting"
            return True
        if self.last_poc is None:
            nudge = (
                "You have not submitted any candidate yet. Inspect the source, build a "
                "candidate with build_poc, then call submit_poc. A prose-only answer cannot be scored."
            )
        else:
            nudge = (
                "Your last candidate did not trigger the vulnerable build. Change the bytes or "
                "hypothesis and submit again with submit_poc. Do not stop before the attempt budget is spent."
            )
        track.messages.append({"role": "user", "content": nudge})
        track.messages = _compact_messages(track.messages)
        return False

    async def _run_tool(
        self,
        name: str,
        args: dict[str, Any],
        tools: CodeTools,
        updater: TaskUpdater,
        track: _Track,
    ) -> str:
        assert self.challenge is not None
        if track.gate and name in ANALYSIS_TOOL_NAMES:
            self.metrics.event("tool_gated", role=track.role, name=name)
            return (
                "Analysis tools are disabled until you submit a candidate. "
                "Call build_poc (or reuse an existing generated/ file), then submit_poc now."
            )
        if name == "list_files":
            return tools.list_files(str(args.get("subdir", ".")), int(args.get("limit", 300)))
        if name == "read_file":
            return tools.read_file(
                str(args.get("path", "")),
                int(args.get("start_line", 1)),
                int(args.get("end_line", 240)),
            )
        if name == "search_code":
            return await tools.search_code(
                str(args.get("pattern", "")),
                str(args.get("subdir", ".")),
                int(args.get("limit", 80)),
            )
        if name == "symbol_context":
            return await tools.symbol_context(str(args.get("symbol", "")))
        if name == "analyze_error":
            return tools.analyze_error(str(args.get("path", "error.txt")))
        if name == "build_poc":
            operations = args.get("operations")
            if not isinstance(operations, list):
                return "tool error: operations must be a list"
            path = await asyncio.to_thread(
                build_poc,
                operations,
                track.generated,
                self.settings.max_poc_bytes,
            )
            return f"wrote generated/{path.name} ({path.stat().st_size} bytes)"
        if name == "run_python":
            code = str(args.get("code", ""))
            if len(code) > 50_000:
                return "tool error: Python code exceeds 50,000 characters"
            result = await self.sandbox.execute(code, track.generated)
            return result.output
        if name == "submit_poc":
            return await self._submit_poc(args, updater, track)
        if name in {"build_target", "run_target", "fuzz_target", "triage_crash", "find_crash"}:
            return await self._run_dynamic_tool(name, args)
        return f"tool error: unknown tool {name!r}"

    async def _run_dynamic_tool(self, name: str, args: dict[str, Any]) -> str:
        if self.dynamic_tools is None:
            return "dynamic analysis is disabled"
        async with self._dynamic_lock:
            if name == "build_target":
                return await self.dynamic_tools.build(
                    str(args.get("repo_subdir", "repo-vul")),
                    str(args.get("target_hint", "")),
                )
            if name == "run_target":
                return await self.dynamic_tools.run(
                    str(args.get("path", "")),
                    str(args.get("input_base64", "")),
                    str(args.get("target", "")),
                )
            if name == "fuzz_target":
                seconds = args.get("seconds")
                return await self.dynamic_tools.fuzz(
                    str(args.get("seed_path", "")),
                    int(seconds) if seconds is not None else None,
                    str(args.get("target", "")),
                )
            if name == "triage_crash":
                return await self.dynamic_tools.triage(
                    str(args.get("path", "")),
                    str(args.get("input_base64", "")),
                    str(args.get("target", "")),
                )
            if name == "find_crash":
                seconds = args.get("seconds")
                return await self.dynamic_tools.discover_crash(
                    str(args.get("target", "")),
                    int(seconds) if seconds is not None else None,
                    str(args.get("repo_subdir", "repo-vul")),
                )
            return f"tool error: unknown dynamic tool {name!r}"

    async def _submit_poc(self, args: dict[str, Any], updater: TaskUpdater, track: _Track) -> str:
        assert self.challenge is not None
        encoded = args.get("poc_base64")
        relative = str(args.get("path") or "poc")
        if encoded:
            try:
                poc = base64.b64decode(str(encoded), validate=True)
            except Exception:
                return "tool error: poc_base64 is invalid"
        else:
            path = self._resolve_generated(track, relative)
            if path is None:
                return f"tool error: generated/{relative} does not exist"
            poc = path.read_bytes()
        if len(poc) > self.settings.max_poc_bytes:
            return f"tool error: PoC exceeds {self.settings.max_poc_bytes} bytes"

        async with self._submit_lock:
            if self.success:
                return "the harness already confirmed a crashing candidate; stop"
            if self.metrics.attempts >= self.settings.max_attempts:
                return "attempt budget exhausted"
            digest = hashlib.sha256(poc).hexdigest()
            if digest in self._submitted_hashes:
                track.duplicates += 1
                return "duplicate PoC rejected locally; change the bytes before retrying"
            self._submitted_hashes.add(digest)
            self.metrics.attempts += 1
            self.last_poc = poc
            explanation = str(args.get("explanation") or "")[:500]
            self.metrics.event(
                "poc_attempt",
                role=track.role,
                attempt=self.metrics.attempts,
                size=len(poc),
                sha256=digest,
                explanation=explanation,
            )
            verdict = await self._test_candidate(poc, updater)
            self._record_verdict(track, verdict)
            if verdict.crashed:
                self.success = True
                self.winning_poc = poc
                self._stop_event.set()
                return f"SUCCESS: vulnerable target exit_code={verdict.exit_code}; finalizing this exact PoC"
            if self.settings.mutation_attempts > 0 and self.metrics.attempts < self.settings.max_attempts:
                for blob, note in generate_mutations(poc, max_mutations=self.settings.mutation_attempts, seed=self.metrics.attempts):
                    if self.metrics.attempts >= self.settings.max_attempts or self.success:
                        break
                    mutation_digest = hashlib.sha256(blob).hexdigest()
                    if mutation_digest in self._submitted_hashes:
                        continue
                    self._submitted_hashes.add(mutation_digest)
                    self.metrics.attempts += 1
                    self.last_poc = blob
                    self.metrics.event(
                        "poc_mutation",
                        role=track.role,
                        attempt=self.metrics.attempts,
                        size=len(blob),
                        sha256=mutation_digest,
                        note=note,
                    )
                    mutation_verdict = await self._test_candidate(blob, updater)
                    self._record_verdict(track, mutation_verdict)
                    if mutation_verdict.crashed:
                        self.success = True
                        self.winning_poc = blob
                        self._stop_event.set()
                        return f"SUCCESS: mutated candidate exit_code={mutation_verdict.exit_code}; finalizing"
        return f"FAILED ({verdict.category}): {verdict.hint}"

    def _resolve_generated(self, track: _Track, relative: str) -> Path | None:
        assert self.challenge is not None
        bases = [track.generated.resolve(), (self.challenge.workspace / "generated").resolve()]
        candidates = [relative]
        for prefix in ("generated/", "generated"):
            if relative.startswith(prefix):
                candidates.append(relative[len(prefix):].lstrip("/") or relative)
        for name in candidates:
            for base in bases:
                candidate = (base / name).resolve()
                if candidate != base and base not in candidate.parents:
                    continue
                if candidate.is_file():
                    return candidate
        return None

    async def _test_candidate(self, poc: bytes, updater: TaskUpdater) -> FeedbackVerdict:
        self.waiting_for_feedback = True
        try:
            await updater.requires_input(updater.new_agent_message(parts=[
                Part(root=DataPart(data={"action": "test_vulnerable"})),
                Part(root=FilePart(file=FileWithBytes(
                    bytes=base64.b64encode(poc).decode("ascii"),
                    name="poc",
                    mime_type="application/octet-stream",
                ))),
            ]))
            try:
                feedback = await asyncio.wait_for(
                    self.feedback_queue.get(),
                    timeout=self.settings.feedback_timeout_seconds,
                )
            except TimeoutError:
                self.metrics.event("verifier_feedback", attempt=self.metrics.attempts, error="timeout")
                return classify_verifier_feedback({"error": "verifier feedback timed out"})
        finally:
            self.waiting_for_feedback = False
        self.metrics.event(
            "verifier_feedback",
            attempt=self.metrics.attempts,
            feedback_json=_json_text(feedback, self.settings.max_tool_output_chars),
        )
        return classify_verifier_feedback(feedback)

    def _record_verdict(self, track: _Track, verdict: FeedbackVerdict) -> None:
        track.attempts_log.append(f"attempt {self.metrics.attempts}: {verdict.category} - {verdict.hint}")
        if verdict.crashed:
            track.consecutive_failures = 0
        else:
            track.consecutive_failures += 1

    def _read_text_attachments(self) -> tuple[str, str]:
        assert self.challenge is not None

        def read(name: str) -> str:
            path = self.challenge.input_root / name
            if path.is_file():
                return path.read_text(errors="replace")[:20000]
            return ""

        return read("description.txt"), read("error.txt")

    def _read_patch(self) -> str:
        assert self.challenge is not None
        path = self.challenge.input_root / "patch.diff"
        if path.is_file():
            return path.read_text(errors="replace")[:8000]
        return ""

    async def _maybe_run_reviewer(self, track: _Track, tools: CodeTools) -> None:
        if not self.settings.reviewer_subagent or self.llm is None:
            return
        if track.reviewer_calls >= self.settings.reviewer_max_calls:
            return
        if track.consecutive_failures < self.settings.stall_threshold:
            return
        track.reviewer_calls += 1
        summary = "\n".join(track.attempts_log[-8:])
        ideas = await run_reviewer(
            llm=self.llm,
            settings=self.settings,
            code_tools=tools,
            triage_summary=self._triage_summary,
            attempts_summary=summary,
            metrics=self.metrics,
        )
        if ideas:
            self.metrics.event("reviewer_suggestions", role=track.role, text=ideas[:2000])
            track.messages.append({"role": "user", "content": f"Reviewer subagent alternative hypotheses:\n{ideas}"})
            track.consecutive_failures = 0

    async def _send_placeholder(self, updater: TaskUpdater) -> None:
        if self._placeholder_sent:
            return
        self._placeholder_sent = True
        placeholder = self._minimal_candidate() or b"A"
        try:
            await self._add_final_artifact(updater, placeholder, final=False)
            self.metrics.event("placeholder_artifact", size=len(placeholder))
        except Exception:  # noqa: BLE001
            self.metrics.event("placeholder_artifact_failed")

    async def _add_final_artifact(self, updater: TaskUpdater, poc: bytes, *, final: bool = True) -> None:
        await updater.add_artifact(
            parts=[Part(root=FilePart(file=FileWithBytes(
                bytes=base64.b64encode(poc).decode("ascii"),
                name="poc",
                mime_type="application/octet-stream",
            )))],
            name="poc",
        )
        if final:
            self._artifact_added = True

    async def finalize(self) -> None:
        if self._finalized:
            return
        self._finalized = True
        print(
            f"[harness] done status={self.metrics.status} artifact_added={self._artifact_added} "
            f"placeholder={self._placeholder_sent} attempts={self.metrics.attempts} "
            f"tokens={self.metrics.total_tokens} error={self.metrics.error[:160]!r}",
            file=sys.stderr,
        )
        if self.dynamic_backend is not None:
            try:
                await self.dynamic_backend.close()
            except Exception:  # noqa: BLE001 - metrics write must still happen
                pass
        await self.writer.write(self.metrics)


class TaskManager:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.writer = MetricsWriter(settings.results_csv, settings.events_jsonl)
        self._sessions: dict[str, HarnessSession] = {}
        self._lock = asyncio.Lock()

    def active_session(self, context_id: str) -> HarnessSession | None:
        session = self._sessions.get(context_id)
        return session if session and session.active else None

    async def start(self, context_id: str, message: Message, updater: TaskUpdater) -> None:
        async with self._lock:
            old = self.active_session(context_id)
            if old:
                raise RuntimeError(f"task context {context_id} is already active")
            session = HarnessSession(context_id, self.settings, self.writer)
            self._sessions[context_id] = session
        try:
            await session.run(message, updater)
        finally:
            async with self._lock:
                if self._sessions.get(context_id) is session:
                    self._sessions.pop(context_id, None)

    async def deliver_feedback(self, context_id: str, message: Message) -> bool:
        session = self.active_session(context_id)
        if not session:
            return False
        await session.deliver_feedback(message)
        return True

    @staticmethod
    def looks_like_feedback(message: Message) -> bool:
        data = first_data_part(message)
        return bool(data and ("exit_code" in data or "error" in data))
