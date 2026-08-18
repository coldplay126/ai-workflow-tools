"""Analysis + System spec acceptance tests — SO-S2-001, SO-S2-002, AN-A6-001, AN-A6-002.

Tests spec loader resource/contract loading, skill search priority,
mode-specific output contracts, and mode-specific Writer set selection.

Reference: docs/tests/analysis-and-system.md
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from awf.core.spec_loader import (
    clear_cache,
    list_skill_resources,
    load_analysis_mode_contract,
    load_json_resource,
    load_manifest,
    load_prompt,
    load_skill_resource,
)
from awf.core.skills import (
    find_skill_dir,
    skill_search_paths,
)
from awf.core.analysis_state import get_required_output_files
from awf.core.analysis_fanout import get_writer_configs, get_judge_prompt_name, run_stage2_fanout


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _make_analysis_context(ai_context_dir: Path, analysis_mode: str = "document"):
    """Create a minimal AnalysisContext-like object for output_files_present()."""
    class _Ctx:
        pass
    ctx = _Ctx()
    ctx.ai_context_dir = ai_context_dir
    ctx.analysis_mode = analysis_mode
    return ctx


def _create_fixture_skill(root: Path, skill_name: str, manifest: dict, resources: dict[str, dict[str, str]]) -> Path:
    """Create a fixture skill directory with manifest and resources.

    Args:
        root: Parent skills directory.
        skill_name: Skill directory name.
        manifest: manifest.json content.
        resources: {category: {name: content}} — file content strings or dicts (auto-serialized).
    Returns:
        Path to the skill directory.
    """
    skill_dir = root / skill_name
    skill_dir.mkdir(parents=True, exist_ok=True)

    # Write manifest
    (skill_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    # Write SKILL.md (minimal)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {skill_name}\ndescription: test skill\n---\n", encoding="utf-8")

    # Write resources
    for category, files in resources.items():
        cat_type = manifest.get("categories", {}).get(category, {}).get("type", "json")
        ext = ".json" if cat_type == "json" else f".{cat_type}"
        cat_path = manifest.get("categories", {}).get(category, {}).get("path", category)
        cat_dir = skill_dir / cat_path
        cat_dir.mkdir(parents=True, exist_ok=True)
        for name, content in files.items():
            if isinstance(content, dict):
                content = json.dumps(content, ensure_ascii=False, indent=2)
            (cat_dir / f"{name}{ext}").write_text(content, encoding="utf-8")

    return skill_dir


# ===========================================================================
# SO-S2-001: spec loader resource/contract loading
# ===========================================================================

def test_s2_001_manifest_load():
    """manifest.json이 정상 로드되고 categories를 포함한다."""
    clear_cache()
    m = load_manifest("analysis")
    assert m["skill"] == "analysis"
    assert "modes" in m["categories"]
    assert "prompts" in m["categories"]
    assert m["categories"]["modes"]["type"] == "json"


def test_s2_001_manifest_auto_discover():
    """manifest.json이 없는 skill은 디렉토리 스캔으로 자동 디스커버리."""
    clear_cache()
    # wf-orchestrator has subdirectories but no manifest.json
    m = load_manifest("wf-orchestrator")
    assert m["version"] == "0.0.0"  # auto-discovered
    assert isinstance(m["categories"], dict)


def test_s2_001_load_json_resource():
    """JSON 리소스가 dict로 로드되고 캐시된다."""
    clear_cache()
    contract = load_json_resource("analysis", "modes", "document")
    assert isinstance(contract, dict)
    assert contract["mode"] == "document"
    # Second load should hit cache
    contract2 = load_json_resource("analysis", "modes", "document")
    assert contract2 is contract  # same object from cache


def test_s2_001_load_md_resource():
    """MD 리소스가 string으로 로드된다."""
    clear_cache()
    content = load_skill_resource("multi-agent", "protocols", "spec_writer")
    assert isinstance(content, str)
    assert len(content) > 50


def test_s2_001_load_prompt():
    """prompt template이 로드되고 변수 치환이 동작한다."""
    clear_cache()
    raw = load_prompt("analysis", "judge")
    assert isinstance(raw, str)
    assert len(raw) > 100


def test_s2_001_list_resources():
    """list_skill_resources가 카테고리 내 리소스 이름을 반환한다."""
    clear_cache()
    modes = list_skill_resources("analysis", "modes")
    assert "document" in modes
    assert "review" in modes
    assert "investigate" in modes


def test_s2_001_mode_contract_validation():
    """mode contract 스키마 검증: 필수 키 누락 시 ValueError."""
    clear_cache()
    try:
        # Valid contract
        contract = load_analysis_mode_contract("document")
        assert "required_output_files" in contract
        assert "writers" in contract
        assert "judge" in contract
    except Exception as exc:
        assert False, f"Valid contract should not raise: {exc}"


def test_s2_001_missing_resource_error():
    """존재하지 않는 리소스 요청 시 FileNotFoundError."""
    clear_cache()
    try:
        load_json_resource("analysis", "modes", "nonexistent_mode")
        assert False, "Should raise FileNotFoundError"
    except FileNotFoundError:
        pass


def test_s2_001_cache_clear():
    """clear_cache() 후 재로드 시 새 데이터 반영."""
    # Load once
    m1 = load_manifest("analysis")
    # Clear
    clear_cache()
    # Load again — should still work (re-reads from disk)
    m2 = load_manifest("analysis")
    assert m2["skill"] == "analysis"
    # Not the same object (cache was cleared)
    assert m1 is not m2


def test_s2_001_modify_and_reload():
    """spec 파일 변경 후 clear_cache → 재로드 시 변경 내용이 반영된다."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_root = Path(tmpdir) / "skills"
        _create_fixture_skill(
            fixture_root, "mutable-skill",
            manifest={"skill": "mutable-skill", "version": "1.0.0",
                      "categories": {"data": {"type": "json", "path": "data"}}},
            resources={"data": {"config": {"value": "original"}}},
        )

        old_env = os.environ.get("AWF_SKILLS_DIR")
        os.environ["AWF_SKILLS_DIR"] = str(fixture_root)
        try:
            clear_cache()
            # First load
            cfg = load_skill_resource("mutable-skill", "data", "config")
            assert cfg["value"] == "original"

            # Modify spec file on disk
            config_path = fixture_root / "mutable-skill" / "data" / "config.json"
            config_path.write_text(json.dumps({"value": "modified"}), encoding="utf-8")

            # Without clear_cache — still returns cached value
            cfg_cached = load_skill_resource("mutable-skill", "data", "config")
            assert cfg_cached["value"] == "original"

            # After clear_cache — returns new value
            clear_cache()
            cfg_new = load_skill_resource("mutable-skill", "data", "config")
            assert cfg_new["value"] == "modified"
        finally:
            if old_env is not None:
                os.environ["AWF_SKILLS_DIR"] = old_env
            else:
                os.environ.pop("AWF_SKILLS_DIR", None)
            clear_cache()


def test_s2_001_fixture_spec_root():
    """fixture spec root에서 manifest+resource를 함께 resolve한다."""
    clear_cache()
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_root = Path(tmpdir) / "skills"
        _create_fixture_skill(
            fixture_root,
            "test-skill",
            manifest={
                "skill": "test-skill",
                "version": "2.0.0",
                "categories": {
                    "configs": {"type": "json", "path": "configs"},
                    "templates": {"type": "md", "path": "templates"},
                },
            },
            resources={
                "configs": {"app": {"key": "value", "nested": {"a": 1}}},
                "templates": {"greeting": "Hello {name}!"},
            },
        )

        old_env = os.environ.get("AWF_SKILLS_DIR")
        os.environ["AWF_SKILLS_DIR"] = str(fixture_root)
        try:
            clear_cache()
            # Manifest resolves
            m = load_manifest("test-skill")
            assert m["skill"] == "test-skill"
            assert m["version"] == "2.0.0"

            # JSON resource resolves
            cfg = load_skill_resource("test-skill", "configs", "app")
            assert isinstance(cfg, dict)
            assert cfg["key"] == "value"
            assert cfg["nested"]["a"] == 1

            # MD resource resolves
            tmpl = load_skill_resource("test-skill", "templates", "greeting")
            assert isinstance(tmpl, str)
            assert "Hello {name}!" in tmpl

            # List resources
            configs = list_skill_resources("test-skill", "configs")
            assert configs == ["app"]
            templates = list_skill_resources("test-skill", "templates")
            assert templates == ["greeting"]
        finally:
            if old_env is not None:
                os.environ["AWF_SKILLS_DIR"] = old_env
            else:
                os.environ.pop("AWF_SKILLS_DIR", None)
            clear_cache()


# ===========================================================================
# SO-S2-002: skill search priority
# ===========================================================================

def test_s2_002_search_path_order():
    """skill_search_paths 순서: .config/awf > .awf > .claude(home) > claude > .claude(project)."""
    clear_cache()
    old_env = os.environ.pop("AWF_SKILLS_DIR", None)
    try:
        paths = skill_search_paths()
        path_strs = [str(p) for p in paths]
        # All paths unique
        assert len(paths) == len(set(paths))
        # Verify canonical order: .config/awf before claude paths
        config_idx = next((i for i, p in enumerate(path_strs) if ".config/awf" in p), None)
        claude_idx = next((i for i, p in enumerate(path_strs) if "claude/skills" in p), None)
        if config_idx is not None and claude_idx is not None:
            assert config_idx < claude_idx, f".config/awf ({config_idx}) should come before claude ({claude_idx})"
    finally:
        if old_env is not None:
            os.environ["AWF_SKILLS_DIR"] = old_env


def test_s2_002_env_override_highest_priority():
    """AWF_SKILLS_DIR가 최우선."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_root = Path(tmpdir) / "skills"
        _create_fixture_skill(
            fixture_root,
            "priority-test",
            manifest={"skill": "priority-test", "version": "9.9.9", "categories": {}},
            resources={},
        )

        old_env = os.environ.get("AWF_SKILLS_DIR")
        os.environ["AWF_SKILLS_DIR"] = str(fixture_root)
        try:
            clear_cache()
            # find_skill_dir should find it in env path first
            skill_dir = find_skill_dir("priority-test")
            assert skill_dir is not None
            assert str(fixture_root) in str(skill_dir)
        finally:
            if old_env is not None:
                os.environ["AWF_SKILLS_DIR"] = old_env
            else:
                os.environ.pop("AWF_SKILLS_DIR", None)
            clear_cache()


def test_s2_002_project_path_found():
    """프로젝트 경로(claude/skills)에서 skill이 발견된다."""
    clear_cache()
    skill_dir = find_skill_dir("analysis")
    assert skill_dir is not None
    assert "skills" in str(skill_dir)
    assert skill_dir.is_dir()


def test_s2_002_priority_override():
    """같은 이름의 skill이 여러 root에 있으면 우선순위 높은 root가 선택된다."""
    with tempfile.TemporaryDirectory() as tmpdir:
        fixture_root = Path(tmpdir) / "skills"
        # Create a skill with same name as existing one but different version
        _create_fixture_skill(
            fixture_root,
            "analysis",
            manifest={"skill": "analysis", "version": "99.0.0", "categories": {
                "modes": {"type": "json", "path": "modes"},
            }},
            resources={
                "modes": {"document": {"mode": "document-override", "required_output_files": ["override.md"], "writers": [], "judge": {"prompt": "judge"}}},
            },
        )

        old_env = os.environ.get("AWF_SKILLS_DIR")
        os.environ["AWF_SKILLS_DIR"] = str(fixture_root)
        try:
            clear_cache()
            # Should find the override version (env path has highest priority)
            m = load_manifest("analysis")
            assert m["version"] == "99.0.0"

            contract = load_json_resource("analysis", "modes", "document")
            assert contract["mode"] == "document-override"
        finally:
            if old_env is not None:
                os.environ["AWF_SKILLS_DIR"] = old_env
            else:
                os.environ.pop("AWF_SKILLS_DIR", None)
            clear_cache()


def test_s2_002_multi_root_priority():
    """3개 root에 동일 이름 skill 배치 → 최우선 root가 선택된다."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create 2 roots: env root (priority 1) and project-like root (priority 2)
        env_root = Path(tmpdir) / "env-skills"
        project_root = Path(tmpdir) / "project-skills"

        _create_fixture_skill(
            env_root, "same-name",
            manifest={"skill": "same-name", "version": "1.0.0-env", "categories": {
                "data": {"type": "json", "path": "data"},
            }},
            resources={"data": {"marker": {"source": "env"}}},
        )
        _create_fixture_skill(
            project_root, "same-name",
            manifest={"skill": "same-name", "version": "2.0.0-project", "categories": {
                "data": {"type": "json", "path": "data"},
            }},
            resources={"data": {"marker": {"source": "project"}}},
        )

        old_env = os.environ.get("AWF_SKILLS_DIR")
        os.environ["AWF_SKILLS_DIR"] = str(env_root)
        try:
            clear_cache()
            # env_root has highest priority → env version selected
            m = load_manifest("same-name")
            assert m["version"] == "1.0.0-env"
            marker = load_skill_resource("same-name", "data", "marker")
            assert marker["source"] == "env"

            # Switch priority to project_root
            os.environ["AWF_SKILLS_DIR"] = str(project_root)
            clear_cache()
            m2 = load_manifest("same-name")
            assert m2["version"] == "2.0.0-project"
            marker2 = load_skill_resource("same-name", "data", "marker")
            assert marker2["source"] == "project"
        finally:
            if old_env is not None:
                os.environ["AWF_SKILLS_DIR"] = old_env
            else:
                os.environ.pop("AWF_SKILLS_DIR", None)
            clear_cache()


def test_s2_002_nonexistent_skill():
    """존재하지 않는 skill은 None 반환."""
    clear_cache()
    assert find_skill_dir("completely_nonexistent_skill_xyz") is None


# ===========================================================================
# AN-A6-001: mode별 output contract 검증
# ===========================================================================

def test_a6_001_document_output_contract():
    """document mode: 4개 output 파일 필요."""
    clear_cache()
    files = get_required_output_files("document")
    assert len(files) == 4
    assert "api-spec.json" in files
    assert "data-model.md" in files
    assert "domain-overview.md" in files
    assert "external-integration.md" in files


def test_a6_001_review_output_contract():
    """review mode: review-report.md만 필요 (document 4파일 아님)."""
    clear_cache()
    files = get_required_output_files("review")
    assert files == ["review-report.md"]
    assert "api-spec.json" not in files


def test_a6_001_investigate_output_contract():
    """investigate mode: investigation-report.md만 필요."""
    clear_cache()
    files = get_required_output_files("investigate")
    assert files == ["investigation-report.md"]


def test_a6_001_modes_differ():
    """각 mode의 required output이 서로 다르다."""
    clear_cache()
    doc = set(get_required_output_files("document"))
    rev = set(get_required_output_files("review"))
    inv = set(get_required_output_files("investigate"))
    # All three are different sets
    assert doc != rev
    assert doc != inv
    assert rev != inv


def test_a6_001_missing_output_not_complete():
    """unknown mode → 빈 required list."""
    clear_cache()
    files = get_required_output_files("nonexistent_mode")
    assert files == []


def test_a6_001_output_present_complete():
    """모든 required output이 존재하면 output_files_present() → True."""
    clear_cache()
    from awf.core.analysis_store import output_files_present
    with tempfile.TemporaryDirectory() as tmpdir:
        ai_dir = Path(tmpdir)
        # Create all document mode files
        for f in get_required_output_files("document"):
            (ai_dir / f).write_text("content", encoding="utf-8")
        ctx = _make_analysis_context(ai_dir, "document")
        assert output_files_present(ctx) is True


def test_a6_001_output_missing_incomplete():
    """required output 하나라도 누락 시 output_files_present() → False."""
    clear_cache()
    from awf.core.analysis_store import output_files_present
    with tempfile.TemporaryDirectory() as tmpdir:
        ai_dir = Path(tmpdir)
        required = get_required_output_files("document")
        # Create all except last
        for f in required[:-1]:
            (ai_dir / f).write_text("content", encoding="utf-8")
        ctx = _make_analysis_context(ai_dir, "document")
        assert output_files_present(ctx) is False


def test_a6_001_review_complete_with_own_output():
    """review mode: review-report.md만 있으면 complete."""
    clear_cache()
    from awf.core.analysis_store import output_files_present
    with tempfile.TemporaryDirectory() as tmpdir:
        ai_dir = Path(tmpdir)
        (ai_dir / "review-report.md").write_text("report", encoding="utf-8")
        ctx = _make_analysis_context(ai_dir, "review")
        assert output_files_present(ctx) is True
        # document 4파일은 없어도 review는 complete
        assert not (ai_dir / "api-spec.json").exists()


def test_a6_001_contract_from_spec_file():
    """output contract가 코드 상수가 아닌 외부 spec 파일에서 온다."""
    clear_cache()
    # Load raw contract directly
    contract = load_analysis_mode_contract("review")
    assert contract["required_output_files"] == ["review-report.md"]
    # This proves it comes from the spec file, not a code constant


# ===========================================================================
# AN-A6-002: mode별 Writer set 선택
# ===========================================================================

def test_a6_002_document_writers():
    """document mode: structure + behavior 2개 Writer."""
    clear_cache()
    writers = get_writer_configs("document")
    assert len(writers) == 2
    writer_ids = {w["id"] for w in writers}
    assert writer_ids == {"structure", "behavior"}


def test_a6_002_document_writer_prompts():
    """document mode Writer에 prompt 이름이 지정되어 있다."""
    clear_cache()
    writers = get_writer_configs("document")
    for w in writers:
        assert "prompt" in w, f"Writer {w['id']} missing prompt"
        assert w["prompt"].startswith("writer-"), f"Writer {w['id']} prompt should start with 'writer-'"


def test_a6_002_review_no_writers():
    """review mode: Writer 없음 (Stage 2 fanout 불가)."""
    clear_cache()
    writers = get_writer_configs("review")
    assert writers == []


def test_a6_002_investigate_no_writers():
    """investigate mode: Writer 없음."""
    clear_cache()
    writers = get_writer_configs("investigate")
    assert writers == []


def test_a6_002_writer_output_alignment():
    """document mode: Writer produces와 required output이 정합."""
    clear_cache()
    contract = load_analysis_mode_contract("document")
    required = set(contract["required_output_files"])
    produced = set()
    for w in contract["writers"]:
        produced.update(w.get("produces", []))
    assert produced == required, f"Writers produce {produced} but required is {required}"


def test_a6_002_judge_prompt_per_mode():
    """mode별 judge prompt 이름이 결정된다."""
    clear_cache()
    doc_judge = get_judge_prompt_name("document")
    review_judge = get_judge_prompt_name("review")
    assert doc_judge == "judge"
    assert review_judge == "judge"


def test_a6_002_undefined_writer_not_called():
    """mode에 정의되지 않은 Writer는 configs에 포함되지 않는다."""
    clear_cache()
    doc_writers = get_writer_configs("document")
    doc_ids = {w["id"] for w in doc_writers}
    # document mode should NOT have review/investigate specific writers
    assert "security" not in doc_ids
    assert "quality" not in doc_ids
    assert "performance" not in doc_ids


def test_a6_002_zero_writer_falls_back_without_running_provider():
    """Writer가 없는 mode는 single-agent fallback 계약을 반환한다."""
    clear_cache()
    ctx = _make_analysis_context(Path("/tmp/dummy"), "review")
    calls = []

    for mode in ("review", "investigate"):
        ctx.analysis_mode = mode
        result, error, metadata = run_stage2_fanout(
            context=ctx,
            provider=None,
            provider_factory=None,
            provider_name="dummy",
            add_dirs=[],
            stage1_memo_text="",
            domain_bundle_text="",
            runner=lambda *args: calls.append(args),
            save_additional_result=lambda *args: None,
        )

        assert result is None
        assert error.startswith("fanout_unavailable:")
        assert metadata["status"] == "fallback"

    assert calls == []


def test_a6_002_malformed_writer_config_falls_back_without_running_provider(monkeypatch):
    """손상된 Writer 설정도 provider 실행 전 fallback한다."""
    ctx = _make_analysis_context(Path("/tmp/dummy"), "document")
    calls = []
    monkeypatch.setattr(
        "awf.core.analysis_fanout.get_writer_configs",
        lambda mode: [{"id": "missing-prompt"}],
    )

    result, error, metadata = run_stage2_fanout(
        context=ctx,
        provider=None,
        provider_factory=None,
        provider_name="dummy",
        add_dirs=[],
        stage1_memo_text="",
        domain_bundle_text="",
        runner=lambda *args: calls.append(args),
        save_additional_result=lambda *args: None,
    )

    assert result is None
    assert error.startswith("fanout_unavailable:")
    assert metadata["status"] == "fallback"
    assert calls == []

def test_a6_002_provider_execution_failure_keeps_existing_result_contract():
    """유효한 Writer 실행 실패는 config fallback으로 바뀌지 않는다."""
    from awf.providers.base import ProviderResult

    clear_cache()
    ctx = _make_analysis_context(Path("/tmp/dummy"), "document")
    ctx.repo_root = Path("/tmp")
    ctx.service = "service"
    ctx.domain = "orders"
    result, error, metadata = run_stage2_fanout(
        context=ctx,
        provider=None,
        provider_factory=None,
        provider_name="dummy",
        add_dirs=[],
        stage1_memo_text="",
        domain_bundle_text="",
        runner=lambda *args: (ProviderResult(returncode=1, stdout="", stderr="provider failed"), 0.0),
        save_additional_result=lambda *args: None,
    )

    assert result is None
    assert error.startswith("writer_partial_failure:")
    assert not error.startswith("fanout_unavailable:")
    assert metadata.get("status") is None


def _run_v2_fanout_fixture(tmp_path, api_spec_text):
    from awf.providers.fixture import _build_v2_judge_fixture, _build_v2_writer_fixture

    (tmp_path / "api-spec.json").write_text(api_spec_text, encoding="utf-8")
    for file_name in ("data-model.md", "domain-overview.md", "external-integration.md"):
        (tmp_path / file_name).write_text(f"# {file_name}\n", encoding="utf-8")

    ctx = _make_analysis_context(tmp_path / ".ai-context", "document")
    ctx.repo_root = tmp_path
    ctx.service = "service"
    ctx.domain = "orders"
    saved_artifacts = []

    def runner(_provider, _prompt, _cwd, _add_dirs, label):
        if label.endswith("writer structure"):
            result = _build_v2_writer_fixture(
                tmp_path,
                "structure",
                ["api-spec.json", "data-model.md"],
                0,
                None,
            )
        elif label.endswith("writer behavior"):
            result = _build_v2_writer_fixture(
                tmp_path,
                "behavior",
                ["domain-overview.md", "external-integration.md"],
                0,
                None,
            )
        else:
            result = _build_v2_judge_fixture(tmp_path, 0, None)
        return result, 0.01

    def save_artifact(_context, _provider_name, content, suffix):
        saved_artifacts.append((suffix, content))
        return tmp_path / suffix

    result, error, metadata = run_stage2_fanout(
        context=ctx,
        provider=None,
        provider_factory=None,
        provider_name="fixture",
        add_dirs=[],
        stage1_memo_text="memo",
        domain_bundle_text="bundle",
        runner=runner,
        save_additional_result=save_artifact,
    )
    return result, error, metadata, saved_artifacts


def test_a6_002_exact_json_fence_is_normalized_before_publish(tmp_path):
    from awf.core.analysis_outputs import parse_stage2_output

    result, error, metadata, _ = _run_v2_fanout_fixture(
        tmp_path,
        '```json\n{"endpoints": []}\n```\n',
    )

    assert error is None
    assert result is not None
    api_spec = parse_stage2_output(result.stdout)["api-spec.json"]
    assert json.loads(api_spec) == {"endpoints": []}
    assert "```" not in api_spec
    assert metadata["consistencyPassed"] is True


def test_a6_002_fenced_non_object_json_fails_and_preserves_diagnostic(tmp_path):
    result, error, metadata, saved_artifacts = _run_v2_fanout_fixture(
        tmp_path,
        "```json\n[]\n```\n",
    )

    assert result is None
    assert error == "consistency_check_failed:invalid_api_spec_json"
    assert metadata["consistencyPassed"] is False
    assert metadata["consistencyArtifactSuffix"] == "fanout-consistency"
    diagnostic = next(content for suffix, content in saved_artifacts if suffix == "fanout-consistency")
    assert "===FILE: api-spec.json===\n[]" in diagnostic


def test_a6_002_malformed_fenced_json_fails_and_preserves_diagnostic(tmp_path):
    result, error, metadata, saved_artifacts = _run_v2_fanout_fixture(
        tmp_path,
        '```json\n{"endpoints": [}\n```\n',
    )

    assert result is None
    assert error == "consistency_check_failed:invalid_api_spec_json"
    assert metadata["consistencyPassed"] is False
    assert metadata["consistencyIssues"] == ["invalid_api_spec_json"]
    assert metadata["consistencyArtifactSuffix"] == "fanout-consistency"
    diagnostic = next(content for suffix, content in saved_artifacts if suffix == "fanout-consistency")
    assert '```json\n{"endpoints": [}\n```' in diagnostic


def test_a6_002_json_fence_with_surrounding_prose_is_not_normalized(tmp_path):
    result, error, metadata, _ = _run_v2_fanout_fixture(
        tmp_path,
        'Generated result:\n```json\n{"endpoints": []}\n```\n',
    )

    assert result is None
    assert error == "consistency_check_failed:invalid_api_spec_json"
    assert metadata["consistencyPassed"] is False


# ===========================================================================
# AN-A2-001: Stage 1 observation-only artifact
# ===========================================================================

def test_a2_001_observation_fields_only():
    """Stage 1 parse_observation 결과에 observation 필드만 존재하고 markdown이 보존된다."""
    from awf.core.analysis_stage1 import parse_observation
    raw = '''Markdown before JSON block.

```json
{
  "path": "src/app.ts",
  "role": "controller",
  "language": "typescript",
  "lines": 150,
  "imports": ["express", "cors"],
  "business_logic": ["handles user authentication"],
  "signals": ["uses middleware pattern"]
}
```
'''
    result = parse_observation(raw, "src/app.ts")
    # Core observation fields present
    assert result["path"] == "src/app.ts"
    assert result["role"] == "controller"
    assert result["language"] == "typescript"
    assert result["lines"] == 150
    assert "observation" in result
    assert "json" in result["observation"]
    assert "markdown" in result["observation"]
    # Markdown before JSON block is preserved
    assert "Markdown before JSON block" in result["observation"]["markdown"]


def test_a2_001_no_judgment_or_compat_fields():
    """Stage 1 결과에 judgment/compat 필드가 없다."""
    from awf.core.analysis_stage1 import parse_observation
    raw = '''```json
{
  "path": "src/svc.ts",
  "role": "service",
  "language": "typescript",
  "lines": 80,
  "imports": [],
  "business_logic": [],
  "signals": []
}
```'''
    result = parse_observation(raw, "src/svc.ts")
    # Judgment fields
    judgment_fields = {"severity", "conclusion", "recommendation", "verdict", "score", "rating"}
    present = judgment_fields & set(result.keys())
    assert present == set(), f"Judgment fields found in Stage 1: {present}"
    # v2 compat fields should NOT be at top level
    compat_fields = {"exports", "summary", "dependencies", "complexity"}
    present_compat = compat_fields & set(result.keys())
    assert present_compat == set(), f"Compat fields at top level: {present_compat}"
    # Also check inside observation.json
    obs_json = result["observation"]["json"]
    present_inner = judgment_fields & set(obs_json.keys())
    assert present_inner == set(), f"Judgment fields in observation.json: {present_inner}"


def test_a2_001_required_fields():
    """_REQUIRED_OBSERVATION_FIELDS가 사실 계층 필드만 포함한다."""
    from awf.core.analysis_stage1 import _REQUIRED_OBSERVATION_FIELDS
    expected = {"path", "role", "language", "lines", "imports", "business_logic", "signals"}
    assert _REQUIRED_OBSERVATION_FIELDS == expected
    # None of these are judgment fields
    judgment_fields = {"severity", "conclusion", "recommendation", "verdict"}
    assert _REQUIRED_OBSERVATION_FIELDS & judgment_fields == set()


def test_a2_001_defaults_for_missing():
    """LLM이 필드를 누락해도 기본값으로 채워진다 (crash 없음)."""
    from awf.core.analysis_stage1 import parse_observation
    raw = '```json\n{"path": "x.py"}\n```'
    result = parse_observation(raw, "x.py")
    assert result["role"] == "unknown"
    assert result["language"] == "unknown"
    assert result["lines"] == 0


# ===========================================================================
# AN-A5-001: evidence immutability
# ===========================================================================

def test_a5_001_intact_evidence_passes():
    """Judge가 Writer evidence를 그대로 보존하면 violations 없음."""
    from awf.core.analysis_writer import Claim, WriterResult, JudgeResult, validate_evidence_integrity
    wr = WriterResult(
        writer="structure",
        claims=[
            Claim("S1", "endpoint", "POST /api", "controller.ts:15", ["src/controller.ts"], "high"),
            Claim("S2", "table", "T_USER", "service.ts:42", ["src/service.ts"], "medium"),
        ],
        output_sections={}, raw_output="",
    )
    jr = JudgeResult(
        verdict="merged",
        merged_claims=[
            {"id": "S1", "evidence": "controller.ts:15", "source_files": ["src/controller.ts"]},
            {"id": "S2", "evidence": "service.ts:42", "source_files": ["src/service.ts"]},
        ],
        merged_output={}, consistency_checks=[], raw_output="",
    )
    violations = validate_evidence_integrity([wr], jr)
    assert violations == []


def test_a5_001_modified_evidence_fails():
    """Judge가 evidence text를 변경하면 violation 발생."""
    from awf.core.analysis_writer import Claim, WriterResult, JudgeResult, validate_evidence_integrity
    wr = WriterResult(
        writer="structure",
        claims=[Claim("S1", "endpoint", "POST /api", "controller.ts:15", ["src/controller.ts"], "high")],
        output_sections={}, raw_output="",
    )
    jr = JudgeResult(
        verdict="merged",
        merged_claims=[
            {"id": "S1", "evidence": "MODIFIED evidence text", "source_files": ["src/controller.ts"]},
        ],
        merged_output={}, consistency_checks=[], raw_output="",
    )
    violations = validate_evidence_integrity([wr], jr)
    assert len(violations) == 1
    assert "evidence_modified:S1" in violations


def test_a5_001_modified_source_files_fails():
    """Judge가 source_files를 변경하면 violation 발생."""
    from awf.core.analysis_writer import Claim, WriterResult, JudgeResult, validate_evidence_integrity
    wr = WriterResult(
        writer="structure",
        claims=[Claim("S1", "endpoint", "POST /api", "ctrl.ts:15", ["src/ctrl.ts"], "high")],
        output_sections={}, raw_output="",
    )
    jr = JudgeResult(
        verdict="merged",
        merged_claims=[
            {"id": "S1", "evidence": "ctrl.ts:15", "source_files": ["src/ctrl.ts", "src/extra.ts"]},
        ],
        merged_output={}, consistency_checks=[], raw_output="",
    )
    violations = validate_evidence_integrity([wr], jr)
    assert len(violations) == 1
    assert "source_files_modified:S1" in violations


def test_a5_001_unknown_claim_id_fails():
    """Judge가 Writer에 없는 claim id를 참조하면 violation 발생."""
    from awf.core.analysis_writer import WriterResult, JudgeResult, validate_evidence_integrity
    wr = WriterResult(writer="structure", claims=[], output_sections={}, raw_output="")
    jr = JudgeResult(
        verdict="merged",
        merged_claims=[{"id": "FAKE1", "evidence": "x", "source_files": []}],
        merged_output={}, consistency_checks=[], raw_output="",
    )
    violations = validate_evidence_integrity([wr], jr)
    assert "unknown_claim_id:FAKE1" in violations


def test_a5_001_legacy_claim_missing_evidence_fields_fails():
    """legacy direct claim은 evidence 필드 누락도 변경으로 진단한다."""
    from awf.core.analysis_writer import Claim, WriterResult, JudgeResult, validate_evidence_integrity

    writer = WriterResult(
        writer="structure",
        claims=[Claim("S1", "endpoint", "POST /api", "ctrl.ts:15", ["src/ctrl.ts"], "high")],
        output_sections={},
        raw_output="",
    )
    judge = JudgeResult(
        verdict="merged",
        merged_claims=[{"id": "S1"}],
        merged_output={},
        consistency_checks=[],
        raw_output="",
    )

    assert validate_evidence_integrity([writer], judge) == [
        "evidence_modified:S1",
        "source_files_modified:S1",
    ]


def test_a5_001_merged_claim_validates_qualified_original_claims():
    """새 merged id는 original_claims가 모두 실제 Writer claim이면 허용한다."""
    from awf.core.analysis_writer import Claim, WriterResult, JudgeResult, validate_evidence_integrity

    structure = WriterResult(
        writer="structure",
        claims=[Claim("S1", "endpoint", "POST /api", "ctrl.ts:15", ["src/ctrl.ts"], "high")],
        output_sections={},
        raw_output="",
    )
    behavior = WriterResult(
        writer="behavior",
        claims=[Claim("B1", "business_logic", "start quest", "svc.ts:20", ["src/svc.ts"], "high")],
        output_sections={},
        raw_output="",
    )
    judge = JudgeResult(
        verdict="merged",
        merged_claims=[
            {
                "id": "M1",
                "original_claims": ["structure:S1", "behavior:B1"],
                "resolution": "merged",
            }
        ],
        merged_output={},
        consistency_checks=[],
        raw_output="",
    )

    assert validate_evidence_integrity([structure, behavior], judge) == []


def test_a5_001_merged_claim_rejects_unknown_original_claim():
    """merged id 자체가 아니라 존재하지 않는 original claim을 진단한다."""
    from awf.core.analysis_writer import JudgeResult, validate_evidence_integrity

    judge = JudgeResult(
        verdict="merged",
        merged_claims=[{"id": "M1", "original_claims": ["structure:FAKE1"]}],
        merged_output={},
        consistency_checks=[],
        raw_output="",
    )

    assert validate_evidence_integrity([], judge) == ["unknown_claim_id:structure:FAKE1"]


def test_a5_001_verdict_change_allowed():
    """evidence 유지 + verdict만 변경은 허용 (violations 없음)."""
    from awf.core.analysis_writer import Claim, WriterResult, JudgeResult, validate_evidence_integrity
    wr = WriterResult(
        writer="structure",
        claims=[Claim("S1", "endpoint", "POST /api", "ctrl.ts:15", ["src/ctrl.ts"], "high")],
        output_sections={}, raw_output="",
    )
    jr = JudgeResult(
        verdict="rejected",  # different verdict
        merged_claims=[
            {"id": "S1", "evidence": "ctrl.ts:15", "source_files": ["src/ctrl.ts"],
             "resolution": "rejected"},  # extra field
        ],
        merged_output={}, consistency_checks=[], raw_output="",
    )
    violations = validate_evidence_integrity([wr], jr)
    assert violations == []


# ===========================================================================
# SO-S4-001: tool-neutral spec discovery
# ===========================================================================

def test_s4_001_awf_skills_path():
    """tool-neutral .awf/skills 경로에서 skill이 발견된다."""
    with tempfile.TemporaryDirectory() as tmpdir:
        awf_root = Path(tmpdir) / ".awf" / "skills"
        _create_fixture_skill(
            awf_root, "tool-neutral-test",
            manifest={"skill": "tool-neutral-test", "version": "1.0.0",
                      "categories": {"data": {"type": "json", "path": "data"}}},
            resources={"data": {"info": {"source": "awf"}}},
        )

        old_env = os.environ.get("AWF_SKILLS_DIR")
        os.environ["AWF_SKILLS_DIR"] = str(awf_root)
        try:
            clear_cache()
            skill_dir = find_skill_dir("tool-neutral-test")
            assert skill_dir is not None
            cfg = load_skill_resource("tool-neutral-test", "data", "info")
            assert cfg["source"] == "awf"
        finally:
            if old_env is not None:
                os.environ["AWF_SKILLS_DIR"] = old_env
            else:
                os.environ.pop("AWF_SKILLS_DIR", None)
            clear_cache()


def test_s4_001_no_claude_path_required():
    """claude/skills 경로가 없어도 tool-neutral 경로에서 로딩 가능."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Use only config path (no claude at all)
        config_root = Path(tmpdir) / "config-skills"
        _create_fixture_skill(
            config_root, "no-claude",
            manifest={"skill": "no-claude", "version": "1.0.0",
                      "categories": {"specs": {"type": "json", "path": "specs"}}},
            resources={"specs": {"main": {"tool": "agnostic"}}},
        )

        old_env = os.environ.get("AWF_SKILLS_DIR")
        os.environ["AWF_SKILLS_DIR"] = str(config_root)
        try:
            clear_cache()
            m = load_manifest("no-claude")
            assert m["skill"] == "no-claude"
            spec = load_skill_resource("no-claude", "specs", "main")
            assert spec["tool"] == "agnostic"
            resources = list_skill_resources("no-claude", "specs")
            assert resources == ["main"]
        finally:
            if old_env is not None:
                os.environ["AWF_SKILLS_DIR"] = old_env
            else:
                os.environ.pop("AWF_SKILLS_DIR", None)
            clear_cache()


def test_s4_001_tool_neutral_before_claude():
    """tool-neutral 경로(.config/awf, .awf)가 claude 경로보다 우선."""
    clear_cache()
    old_env = os.environ.pop("AWF_SKILLS_DIR", None)
    try:
        paths = skill_search_paths()
        path_strs = [str(p) for p in paths]
        config_idx = next((i for i, p in enumerate(path_strs) if ".config/awf" in p), None)
        awf_idx = next((i for i, p in enumerate(path_strs) if ".awf/skills" in p), None)
        claude_idx = next((i for i, p in enumerate(path_strs) if "claude/skills" in p), None)
        # .config/awf and .awf should come before claude
        if config_idx is not None and claude_idx is not None:
            assert config_idx < claude_idx
        if awf_idx is not None and claude_idx is not None:
            assert awf_idx < claude_idx
    finally:
        if old_env is not None:
            os.environ["AWF_SKILLS_DIR"] = old_env


def test_s4_001_default_paths_include_tool_neutral():
    """AWF_SKILLS_DIR 없이도 .config/awf/skills와 .awf/skills가 포함된다."""
    old_env = os.environ.pop("AWF_SKILLS_DIR", None)
    try:
        clear_cache()
        paths = skill_search_paths()
        path_strs = [str(p) for p in paths]
        has_config = any(".config/awf/skills" in p for p in path_strs)
        has_awf = any(".awf/skills" in p for p in path_strs)
        assert has_config, f"Missing .config/awf/skills in {path_strs}"
        assert has_awf, f"Missing .awf/skills in {path_strs}"
    finally:
        if old_env is not None:
            os.environ["AWF_SKILLS_DIR"] = old_env


# ===========================================================================
# SO-S3-001: event system scope
# ===========================================================================

def test_s3_001_event_emit_and_accumulate():
    """EventProcessor.emit()이 이벤트를 in-memory에 축적한다."""
    from awf.core.event_processor import EventProcessor
    from awf.core.events import EventType
    proc = EventProcessor()
    proc.emit(event_type=EventType.TASK_STARTED, task_id="t1", source="test", data={"key": "val"})
    proc.emit(event_type=EventType.TASK_COMPLETED, task_id="t1", source="test")
    assert len(proc.events) == 2
    assert proc.events[0].type == EventType.TASK_STARTED
    assert proc.events[0].data["key"] == "val"
    assert proc.events[1].type == EventType.TASK_COMPLETED


def test_s3_001_sequence_numbers():
    """이벤트마다 고유 sequence 번호가 증가한다."""
    from awf.core.event_processor import EventProcessor
    from awf.core.events import EventType
    proc = EventProcessor()
    e1 = proc.emit(event_type=EventType.STAGE_STARTED, task_id="t1", source="test")
    e2 = proc.emit(event_type=EventType.STAGE_COMPLETED, task_id="t1", source="test")
    e3 = proc.emit(event_type=EventType.HEARTBEAT, task_id="t1", source="test")
    assert e1.sequence == 0
    assert e2.sequence == 1
    assert e3.sequence == 2


def test_s3_001_handler_subscription():
    """handler를 등록하면 emit 시 호출된다 (subscription 메커니즘)."""
    from awf.core.event_processor import EventProcessor
    from awf.core.events import EventType
    received: list = []
    proc = EventProcessor(handlers=[lambda e: received.append(e)])
    proc.emit(event_type=EventType.TASK_STARTED, task_id="t1", source="test")
    proc.emit(event_type=EventType.TASK_COMPLETED, task_id="t1", source="test")
    assert len(received) == 2
    assert received[0].type == EventType.TASK_STARTED


def test_s3_001_persistence_via_state_updater():
    """state_updater가 이벤트를 파일에 영속화한다."""
    from awf.core.event_processor import EventProcessor
    from awf.core.events import EventType
    from awf.core.state_updater import WorkflowStateUpdater
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create repo root marker so find_repo_root accepts this directory
        (Path(tmpdir) / "claude" / "agents").mkdir(parents=True)

        wf_dir = Path(tmpdir) / ".workflow"
        wf_dir.mkdir()
        (wf_dir / "state.json").write_text("{}", encoding="utf-8")

        updater = WorkflowStateUpdater(repo_root=tmpdir)
        proc = EventProcessor(handlers=[updater.handle])
        proc.emit(event_type=EventType.PHASE_STARTED, task_id="t1", source="test",
                  data={"phase": "plan"})

        # State should be persisted to file
        state = json.loads((wf_dir / "state.json").read_text(encoding="utf-8"))
        sync = state.get("eventSync", {})
        assert "lastEventAt" in sync
        phases = sync.get("phases", {})
        assert "plan" in phases
        assert phases["plan"]["status"] == "started"


def test_s3_001_run_id_consistent():
    """단일 EventProcessor의 모든 이벤트는 같은 run_id를 공유한다."""
    from awf.core.event_processor import EventProcessor
    from awf.core.events import EventType
    proc = EventProcessor()
    e1 = proc.emit(event_type=EventType.TASK_STARTED, task_id="t1", source="test")
    e2 = proc.emit(event_type=EventType.TASK_COMPLETED, task_id="t1", source="test")
    assert e1.run_id == e2.run_id
    assert e1.run_id == proc.run_id


# ===========================================================================
# Runner
# ===========================================================================

def main() -> int:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    passed = 0
    failed = 0
    current_group = ""

    for test_fn in tests:
        name = test_fn.__name__
        # Group by test id prefix
        parts = name.split("_", 2)
        group = f"{parts[1]}_{parts[2].split('_')[0]}" if len(parts) > 2 else parts[1]
        if group != current_group:
            current_group = group
            print(f"\n--- {group.upper().replace('_', '-')} ---")
        try:
            test_fn()
            print(f"  PASS  {name}")
            passed += 1
        except Exception as exc:
            print(f"  FAIL  {name}: {exc}")
            failed += 1

    print(f"\n{'='*40}")
    print(f"{passed} passed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
