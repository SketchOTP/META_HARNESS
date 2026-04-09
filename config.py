"""
meta_harness/config.py
Load and validate metaharness.toml from a project root.
"""
from __future__ import annotations

from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent / ".env")
except ImportError:
    pass

import sys
from dataclasses import dataclass, field

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore


# ── Sub-configs ────────────────────────────────────────────────────────────────

@dataclass
class ProjectConfig:
    name: str = "my-project"
    description: str = ""


@dataclass
class RunConfig:
    command: str = "none"
    working_dir: str = "."
    settle_seconds: int = 10


@dataclass
class TestConfig:
    command: str = "pytest"
    working_dir: str = "."
    timeout_seconds: int = 120
    junit_xml: bool = True


@dataclass
class EvidenceConfig:
    log_patterns: list[str] = field(default_factory=lambda: ["*.log", "logs/*.log"])
    metrics_patterns: list[str] = field(default_factory=lambda: ["metrics.json"])
    max_age_hours: int = 24
    max_log_lines: int = 500
    # Character / line budgets for diagnosis-style prompts (collect() may use higher raw read sizes).
    max_test_output_chars: int = 4000
    max_log_lines_diagnose: int = 120
    max_log_chars_diagnose: int = 12000
    max_git_diff_chars: int = 8000
    max_cycle_history_files: int = 12
    max_cycle_history_lines: int = 12
    max_cycle_history_chars: int = 4000
    max_file_tree_lines: int = 120
    max_file_tree_chars: int = 6000
    max_cursor_failure_chars: int = 4000
    metrics_json_compact: bool = True


@dataclass
class ScopeConfig:
    modifiable: list[str] = field(default_factory=lambda: ["src/**/*.py", "*.py"])
    protected: list[str] = field(default_factory=lambda: ["metaharness.toml", ".git/**"])


@dataclass
class CycleConfig:
    interval_seconds: int = 0
    # Local wall-clock times (24h "HH:MM") for `metaharness daemon` when non-empty; ignores interval_seconds.
    schedule: list[str] = field(default_factory=list)
    veto_seconds: int = 120
    min_evidence_items: int = 1
    max_directive_history: int = 50
    # Opt-in: restore agent-touched paths to HEAD after failed tests or metric regression (requires git).
    rollback_enabled: bool = False
    # When rollback_enabled: restore tracked/untracked agent paths after pytest fails (default on).
    rollback_on_test_failure: bool = True
    # When rollback_enabled: if tests pass but primary metric regresses, revert before completing.
    rollback_on_metric_regression: bool = True
    # When true, skip rollback if the worktree has uncommitted changes outside agent paths (conservative).
    rollback_require_git: bool = True
    # When false (default), wait until the first scheduled time before the first cycle (no catch-up on daemon start).
    catch_up: bool = False


@dataclass
class MaintenanceConfig:
    """Maintenance agent — maps from [maintenance] or legacy [cycle]."""

    interval_seconds: int = 0
    schedule: list[str] = field(default_factory=list)
    veto_seconds: int = 120
    min_evidence_items: int = 1
    max_directive_history: int = 50
    slack_channel: str = ""


@dataclass
class ProductConfig:
    enabled: bool = False
    interval_seconds: int = 0
    schedule: list[str] = field(default_factory=list)
    veto_seconds: int = 600
    slack_channel: str = ""
    modifiable: list[str] = field(
        default_factory=lambda: [
            "**/*.py",
            "**/*.md",
            "**/*.html",
            "**/*.css",
            "**/*.js",
            "**/*.yaml",
            "**/*.toml",
        ]
    )
    protected: list[str] = field(default_factory=list)
    # When false (default), wait until the first scheduled product time before the first product cycle.
    catch_up: bool = False


@dataclass
class VisionConfig:
    statement: str = ""
    target_users: str = ""
    core_value: str = ""
    north_star_metric: str = ""
    features_wanted: list[str] = field(default_factory=list)
    features_done: list[str] = field(default_factory=list)
    out_of_scope: list[str] = field(default_factory=list)


@dataclass
class CursorConfig:
    """Cursor Agent CLI (`agent`) — model ids from `agent models` (e.g. composer-2).

    Set ``agent_bin`` to a name on ``PATH`` or an absolute path. On Linux, installs
    often place the CLI under ``~/.local/bin/``. Override Python subprocess choice via
    ``META_HARNESS_PYTHON`` (see :mod:`platform_runtime`).
    """

    agent_bin: str = "agent"
    model: str = "composer-2"
    # Optional faster model id (reserved / future use). ANALYZE, diagnose, and EXECUTE json_call use ``model``.
    model_fast: str = "composer-2-fast"
    # Single LLM call timeout for agent_call / general phases (alias in TOML: agent_timeout)
    timeout_seconds: int = 1200
    # json_call (diagnosis, etc.); None means use timeout_seconds
    json_timeout: int | None = None
    json_retries: int = 3
    # ANALYZE phase: cap loaded files after relevance filter; 0 = unlimited
    max_files_per_cycle: int = 0


@dataclass
class MemoryConfig:
    enabled: bool = True
    # Refresh inferred patterns every N completed cycles
    pattern_refresh_every: int = 3
    compact_n_wins: int = 3
    compact_n_miss: int = 2
    kg_max_directives: int = 8
    kg_use_query_context: bool = True
    kg_query_max_chars: int = 900
    kg_query_max_directives: int = 6


@dataclass
class GoalsConfig:
    objectives: list[str] = field(default_factory=list)
    primary_metric: str = ""
    optimization_direction: str = "maximize"


@dataclass
class EmbeddingConfig:
    """Optional semantic recall: index directive text + retrieve similar chunks."""

    enabled: bool = False
    provider: str = "openai"  # openai | local
    model: str = "text-embedding-3-small"
    base_url: str = ""
    api_key_env: str = "OPENAI_API_KEY"
    local_model_id: str = "all-MiniLM-L6-v2"
    top_k: int = 4
    max_chunk_chars: int = 1200
    min_similarity: float = 0.25


@dataclass
class SlackConfig:
    """
    Per-project Slack app. Tokens via environment variables (see *_env keys);
    optional inline bot_token / app_token for local dev only — do not commit secrets.
    """

    enabled: bool = False
    bot_token: str = ""
    bot_token_env: str = "SLACK_BOT_TOKEN"
    app_token: str = ""
    app_token_env: str = "SLACK_APP_TOKEN"
    channel: str = "#meta-harness"
    # Legacy TOML key `default_channel` maps to `channel` on load when `channel` is unset.
    default_channel: str = ""
    socket_mode: bool = True
    slash_command: str = "metaharness"
    socket_autostart_with_daemon: bool = True
    post_veto_window: bool = True
    post_cycle_result: bool = True
    # When true, post a short Slack message when a paper is queued for implementation
    # (`metaharness research eval` or `/metaharness research <url>`).
    notify_research_queue: bool = True

    @property
    def post_channel(self) -> str:
        """Channel ID or name used for chat.postMessage (prefers `channel`, then legacy default_channel)."""
        return (self.channel or self.default_channel or "").strip()


# ── Root config ────────────────────────────────────────────────────────────────

@dataclass
class HarnessConfig:
    project_root: Path
    project: ProjectConfig = field(default_factory=ProjectConfig)
    run: RunConfig = field(default_factory=RunConfig)
    test: TestConfig = field(default_factory=TestConfig)
    evidence: EvidenceConfig = field(default_factory=EvidenceConfig)
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    cycle: CycleConfig = field(default_factory=CycleConfig)
    maintenance: MaintenanceConfig = field(default_factory=MaintenanceConfig)
    product: ProductConfig = field(default_factory=ProductConfig)
    vision: VisionConfig = field(default_factory=VisionConfig)
    cursor: CursorConfig = field(default_factory=CursorConfig)
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    goals: GoalsConfig = field(default_factory=GoalsConfig)
    slack: SlackConfig = field(default_factory=SlackConfig)

    # Runtime paths (derived, not from toml)
    @property
    def harness_dir(self) -> Path:
        return self.project_root / ".metaharness"

    @property
    def directives_dir(self) -> Path:
        return self.harness_dir / "directives"

    @property
    def cycles_dir(self) -> Path:
        return self.harness_dir / "cycles"

    @property
    def maintenance_cycles_dir(self) -> Path:
        return self.harness_dir / "cycles" / "maintenance"

    @property
    def product_cycles_dir(self) -> Path:
        return self.harness_dir / "cycles" / "product"

    @property
    def maintenance_reasoning_dir(self) -> Path:
        return self.harness_dir / "reasoning" / "maintenance"

    @property
    def product_reasoning_dir(self) -> Path:
        return self.harness_dir / "reasoning" / "product"

    @property
    def memory_dir(self) -> Path:
        return self.harness_dir / "memory"

    @property
    def embedding_index_path(self) -> Path:
        return self.memory_dir / "embedding_index.sqlite"

    @property
    def pending_veto_path(self) -> Path:
        return self.harness_dir / "PENDING_VETO"

    @property
    def pending_product_veto_path(self) -> Path:
        return self.harness_dir / "PENDING_PRODUCT_VETO"

    @property
    def slack_early_approve_path(self) -> Path:
        """Touch or create (empty) to skip veto wait and proceed immediately (Slack / operator)."""
        return self.harness_dir / "SLACK_EARLY_APPROVE"

    @property
    def slack_early_product_approve_path(self) -> Path:
        return self.harness_dir / "SLACK_EARLY_APPROVE_PRODUCT"

    @property
    def daemon_pause_path(self) -> Path:
        """While this file exists, the daemon skips new cycles (Slack / operator)."""
        return self.harness_dir / "DAEMON_PAUSE"

    @property
    def prompts_dir(self) -> Path:
        return self.harness_dir / "prompts"

    @property
    def reasoning_dir(self) -> Path:
        return self.harness_dir / "reasoning"

    @property
    def kg_path(self) -> Path:
        return self.harness_dir / "knowledge_graph.db"

    @property
    def research_dir(self) -> Path:
        return self.harness_dir / "research"

    @property
    def research_queue_path(self) -> Path:
        return self.research_dir / "queue.json"

    @property
    def veto_context_path(self) -> Path:
        return self.harness_dir / "veto_context.json"

    @property
    def agent_lock_path(self) -> Path:
        """Marker file for agent implementation; lock file is ``AGENT_LOCK.lock``."""
        return self.harness_dir / "AGENT_LOCK"

    @property
    def maintenance_slack_channel(self) -> str:
        return (self.maintenance.slack_channel or "").strip() or self.slack.post_channel

    @property
    def product_slack_channel(self) -> str:
        return (self.product.slack_channel or "").strip() or self.slack.post_channel


def _slack_channel_from_raw(sl_raw: dict) -> str:
    if "channel" in sl_raw:
        return str(sl_raw.get("channel") or "")
    if "default_channel" in sl_raw:
        return str(sl_raw.get("default_channel") or "")
    return "#meta-harness"


def load_config(project_root: Path) -> HarnessConfig:
    """Load metaharness.toml from project_root. Returns defaults if missing."""
    config_path = project_root / "metaharness.toml"
    raw: dict = {}
    if config_path.exists():
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)

    def _section(key: str) -> dict:
        return raw.get(key, {})

    proj_raw = _section("project")
    run_raw = _section("run")
    test_raw = _section("test")
    ev_raw = _section("evidence")
    sc_raw = _section("scope")
    cy_raw = _section("cycle")
    _mh_exists = "maintenance" in raw and isinstance(raw.get("maintenance"), dict)
    mh_raw = _section("maintenance") if _mh_exists else {}
    pr_raw = _section("product")
    vi_raw = _section("vision")
    cu_raw = _section("cursor")
    go_raw = _section("goals")
    sl_raw = _section("slack")
    mem_raw = _section("memory")
    emb_raw = _section("embedding")

    def _schedule_list(src: dict) -> list[str]:
        sraw = src.get("schedule", [])
        if isinstance(sraw, list):
            return [str(x).strip() for x in sraw if str(x).strip()]
        return []

    if _mh_exists:
        _cycle_src = {**cy_raw, **mh_raw}
    else:
        _cycle_src = cy_raw

    _schedule = _schedule_list(_cycle_src)

    def _multiline_str(d: dict, key: str, default: str = "") -> str:
        v = d.get(key, default)
        if v is None:
            return ""
        return str(v).strip()

    _features_list = lambda d, k: (
        [str(x).strip() for x in d.get(k, []) if str(x).strip()]
        if isinstance(d.get(k), list)
        else []
    )

    maintenance_cfg = MaintenanceConfig(
        interval_seconds=int(_cycle_src.get("interval_seconds", 0)),
        schedule=_schedule,
        veto_seconds=int(_cycle_src.get("veto_seconds", 120)),
        min_evidence_items=int(_cycle_src.get("min_evidence_items", 1)),
        max_directive_history=int(_cycle_src.get("max_directive_history", 50)),
        slack_channel=str(mh_raw.get("slack_channel", "") or "").strip()
        if _mh_exists
        else "",
    )

    product_modifiable = pr_raw.get("modifiable")
    if not isinstance(product_modifiable, list) or not product_modifiable:
        product_modifiable = [
            "**/*.py",
            "**/*.md",
            "**/*.html",
            "**/*.css",
            "**/*.js",
            "**/*.yaml",
            "**/*.toml",
        ]
    product_protected = pr_raw.get("protected", [])
    if not isinstance(product_protected, list):
        product_protected = []

    product_cfg = ProductConfig(
        enabled=bool(pr_raw.get("enabled", False)),
        interval_seconds=int(pr_raw.get("interval_seconds", 0)),
        schedule=_schedule_list(pr_raw),
        veto_seconds=int(pr_raw.get("veto_seconds", 600)),
        slack_channel=str(pr_raw.get("slack_channel", "") or "").strip(),
        modifiable=[str(x) for x in product_modifiable],
        protected=[str(x) for x in product_protected],
        catch_up=bool(pr_raw.get("catch_up", False)),
    )

    vision_cfg = VisionConfig(
        statement=_multiline_str(vi_raw, "statement"),
        target_users=_multiline_str(vi_raw, "target_users"),
        core_value=_multiline_str(vi_raw, "core_value"),
        north_star_metric=_multiline_str(vi_raw, "north_star_metric"),
        features_wanted=_features_list(vi_raw, "features_wanted"),
        features_done=_features_list(vi_raw, "features_done"),
        out_of_scope=_features_list(vi_raw, "out_of_scope"),
    )

    cfg = HarnessConfig(
        project_root=project_root,
        project=ProjectConfig(
            name=proj_raw.get("name", "my-project"),
            description=proj_raw.get("description", ""),
        ),
        run=RunConfig(
            command=run_raw.get("command", "none"),
            working_dir=run_raw.get("working_dir", "."),
            settle_seconds=run_raw.get("settle_seconds", 10),
        ),
        test=TestConfig(
            command=test_raw.get("command", "pytest"),
            working_dir=test_raw.get("working_dir", "."),
            timeout_seconds=test_raw.get("timeout_seconds", 120),
            junit_xml=bool(test_raw.get("junit_xml", True)),
        ),
        evidence=EvidenceConfig(
            log_patterns=ev_raw.get("log_patterns", ["*.log"]),
            metrics_patterns=ev_raw.get("metrics_patterns", ["metrics.json"]),
            max_age_hours=ev_raw.get("max_age_hours", 24),
            max_log_lines=ev_raw.get("max_log_lines", 500),
            max_test_output_chars=int(ev_raw.get("max_test_output_chars", 4000)),
            max_log_lines_diagnose=int(ev_raw.get("max_log_lines_diagnose", 120)),
            max_log_chars_diagnose=int(ev_raw.get("max_log_chars_diagnose", 12000)),
            max_git_diff_chars=int(ev_raw.get("max_git_diff_chars", 8000)),
            max_cycle_history_files=int(ev_raw.get("max_cycle_history_files", 12)),
            max_cycle_history_lines=int(ev_raw.get("max_cycle_history_lines", 12)),
            max_cycle_history_chars=int(ev_raw.get("max_cycle_history_chars", 4000)),
            max_file_tree_lines=int(ev_raw.get("max_file_tree_lines", 120)),
            max_file_tree_chars=int(ev_raw.get("max_file_tree_chars", 6000)),
            max_cursor_failure_chars=int(ev_raw.get("max_cursor_failure_chars", 4000)),
            metrics_json_compact=bool(ev_raw.get("metrics_json_compact", True)),
        ),
        scope=ScopeConfig(
            modifiable=sc_raw.get("modifiable", ["src/**/*.py", "*.py"]),
            protected=sc_raw.get("protected", ["metaharness.toml", ".git/**"]),
        ),
        cycle=CycleConfig(
            interval_seconds=int(_cycle_src.get("interval_seconds", 0)),
            schedule=maintenance_cfg.schedule,
            veto_seconds=int(_cycle_src.get("veto_seconds", 120)),
            min_evidence_items=int(_cycle_src.get("min_evidence_items", 1)),
            max_directive_history=int(_cycle_src.get("max_directive_history", 50)),
            rollback_enabled=bool(_cycle_src.get("rollback_enabled", False)),
            rollback_on_test_failure=bool(_cycle_src.get("rollback_on_test_failure", True)),
            rollback_on_metric_regression=bool(
                _cycle_src.get("rollback_on_metric_regression", True)
            ),
            rollback_require_git=bool(_cycle_src.get("rollback_require_git", True)),
            catch_up=bool(_cycle_src.get("catch_up", False)),
        ),
        maintenance=maintenance_cfg,
        product=product_cfg,
        vision=vision_cfg,
        cursor=CursorConfig(
            agent_bin=cu_raw.get("agent_bin", "agent"),
            model=cu_raw.get("model", "composer-2"),
            model_fast=cu_raw.get("model_fast", "composer-2-fast"),
            timeout_seconds=cu_raw.get(
                "timeout_seconds", cu_raw.get("agent_timeout", 1200)
            ),
            json_timeout=cu_raw.get("json_timeout"),
            json_retries=int(cu_raw.get("json_retries", 3)),
            max_files_per_cycle=int(cu_raw.get("max_files_per_cycle", 0)),
        ),
        memory=MemoryConfig(
            enabled=mem_raw.get("enabled", True),
            pattern_refresh_every=int(mem_raw.get("pattern_refresh_every", 3)),
            compact_n_wins=int(mem_raw.get("compact_n_wins", 3)),
            compact_n_miss=int(mem_raw.get("compact_n_miss", 2)),
            kg_max_directives=int(mem_raw.get("kg_max_directives", 8)),
            kg_use_query_context=bool(mem_raw.get("kg_use_query_context", True)),
            kg_query_max_chars=int(mem_raw.get("kg_query_max_chars", 900)),
            kg_query_max_directives=int(mem_raw.get("kg_query_max_directives", 6)),
        ),
        embedding=EmbeddingConfig(
            enabled=bool(emb_raw.get("enabled", False)),
            provider=str(emb_raw.get("provider", "openai") or "openai").strip().lower(),
            model=str(emb_raw.get("model", "text-embedding-3-small") or "text-embedding-3-small"),
            base_url=str(emb_raw.get("base_url", "") or "").strip(),
            api_key_env=str(emb_raw.get("api_key_env", "OPENAI_API_KEY") or "OPENAI_API_KEY"),
            local_model_id=str(
                emb_raw.get("local_model_id", "all-MiniLM-L6-v2") or "all-MiniLM-L6-v2"
            ),
            top_k=int(emb_raw.get("top_k", 4)),
            max_chunk_chars=int(emb_raw.get("max_chunk_chars", 1200)),
            min_similarity=float(emb_raw.get("min_similarity", 0.25)),
        ),
        goals=GoalsConfig(
            objectives=go_raw.get("objectives", []),
            primary_metric=go_raw.get("primary_metric", ""),
            optimization_direction=go_raw.get("optimization_direction", "maximize"),
        ),
        slack=SlackConfig(
            enabled=bool(sl_raw.get("enabled", False)),
            bot_token=str(sl_raw.get("bot_token", "") or ""),
            bot_token_env=str(
                sl_raw.get("bot_token_env", "SLACK_BOT_TOKEN") or "SLACK_BOT_TOKEN"
            ),
            app_token=str(sl_raw.get("app_token", "") or ""),
            app_token_env=str(
                sl_raw.get("app_token_env", "SLACK_APP_TOKEN") or "SLACK_APP_TOKEN"
            ),
            channel=_slack_channel_from_raw(sl_raw),
            default_channel=str(sl_raw.get("default_channel", "") or ""),
            socket_mode=bool(sl_raw.get("socket_mode", True)),
            slash_command=str(sl_raw.get("slash_command", "metaharness") or "metaharness").lstrip(
                "/"
            ),
            socket_autostart_with_daemon=bool(
                sl_raw.get("socket_autostart_with_daemon", True)
            ),
            post_veto_window=bool(sl_raw.get("post_veto_window", True)),
            post_cycle_result=bool(sl_raw.get("post_cycle_result", True)),
            notify_research_queue=bool(sl_raw.get("notify_research_queue", True)),
        ),
    )

    # Ensure runtime dirs exist
    cfg.harness_dir.mkdir(parents=True, exist_ok=True)
    cfg.directives_dir.mkdir(parents=True, exist_ok=True)
    cfg.cycles_dir.mkdir(parents=True, exist_ok=True)
    cfg.maintenance_cycles_dir.mkdir(parents=True, exist_ok=True)
    cfg.product_cycles_dir.mkdir(parents=True, exist_ok=True)
    cfg.memory_dir.mkdir(parents=True, exist_ok=True)
    cfg.prompts_dir.mkdir(parents=True, exist_ok=True)
    cfg.reasoning_dir.mkdir(parents=True, exist_ok=True)
    cfg.maintenance_reasoning_dir.mkdir(parents=True, exist_ok=True)
    cfg.product_reasoning_dir.mkdir(parents=True, exist_ok=True)
    cfg.research_dir.mkdir(parents=True, exist_ok=True)

    return cfg
