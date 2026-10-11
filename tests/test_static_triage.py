from __future__ import annotations

from pathlib import Path

from cybergym_harness.static_triage import static_triage


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo-vul"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "parser.c").write_text(
        "void parse(const char *p, int n) { char buf[8]; memcpy(buf, p, n); }\n"
    )
    (repo / "fuzz").mkdir()
    (repo / "fuzz" / "target.cc").write_text(
        "int LLVMFuzzerTestOneInput(const unsigned char *data, unsigned long size) { parse((char*)data, size); return 0; }\n"
    )
    (repo / "README").write_text("not source")
    return repo


def test_triage_ranks_relevant_files_and_detects_entry(tmp_path: Path):
    repo = _repo(tmp_path)
    result = static_triage(
        repo,
        description_text="heap-buffer-overflow in parse on binary image input",
        error_text="ERROR: AddressSanitizer: heap-buffer-overflow\n#0 0x1 in parse /src/parser.c:1\n",
    )
    assert result.fuzz_target == "fuzz/target.cc"
    assert result.sanitizer == "heap-buffer-overflow"
    assert result.crash_function is not None and "parse" in result.crash_function
    ranked = dict(result.ranked_files)
    assert "src/parser.c" in ranked
    rendered = result.render()
    assert "heap-buffer-overflow" in rendered
    assert "fuzz/target.cc" in rendered


def test_triage_handles_missing_repo(tmp_path: Path):
    result = static_triage(tmp_path / "nope", description_text="binary file")
    assert result.ranked_files == []
    assert result.input_kind == "binary_file"


def test_triage_emits_build_files_and_bounded_tree(tmp_path: Path):
    repo = _repo(tmp_path)
    (repo / "CMakeLists.txt").write_text("project(x)\n")
    (repo / "src" / "deep").mkdir()
    (repo / "src" / "deep" / "nested.c").write_text("int deep(void){return 0;}\n")
    result = static_triage(repo, description_text="heap overflow")
    assert "CMakeLists.txt" in result.build_files
    assert result.tree
    joined = "\n".join(result.tree)
    assert "src/" in joined
    assert len(result.tree) <= 40
    rendered = result.render()
    assert "build system files" in rendered
    assert "directory tree" in rendered
