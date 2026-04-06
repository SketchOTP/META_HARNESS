from __future__ import annotations

import textwrap
from pathlib import Path

from meta_harness.config import load_config


def test_load_defaults(tmp_path: Path):
    cfg = load_config(tmp_path)
    assert cfg.project.name == "my-project"
    assert cfg.cursor.model == "composer-2"
    assert cfg.cursor.model_fast == "composer-2-fast"
    assert cfg.cursor.json_retries == 3
    assert cfg.slack.notify_research_queue is True


def test_load_from_toml(tmp_path: Path):
    toml = textwrap.dedent(
        """
        [project]
        name = "p1"
        description = "d"

        [run]
        command = "make"
        working_dir = "sub"
        settle_seconds = 3

        [test]
        command = "tox"
        working_dir = "t"
        timeout_seconds = 99
        junit_xml = false

        [evidence]
        log_patterns = ["a.log"]
        metrics_patterns = ["b.json"]
        max_age_hours = 12
        max_log_lines = 100

        [scope]
        modifiable = ["*.rs"]
        protected = ["Cargo.toml"]

        [cycle]
        interval_seconds = 60
        veto_seconds = 30
        min_evidence_items = 2
        max_directive_history = 10

        [cursor]
        agent_bin = "agent2"
        model = "m1"
        model_fast = "m0"
        timeout_seconds = 600

        [memory]
        enabled = false
        pattern_refresh_every = 5

        [goals]
        objectives = ["o1"]
        primary_metric = "acc"
        optimization_direction = "minimize"

        [slack]
        enabled = false
        """
    )
    (tmp_path / "metaharness.toml").write_text(toml, encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.project.name == "p1"
    assert cfg.run.command == "make"
    assert cfg.test.junit_xml is False
    assert cfg.cursor.model_fast == "m0"
    assert cfg.memory.enabled is False
    assert cfg.goals.primary_metric == "acc"


def test_derived_paths(tmp_path: Path):
    cfg = load_config(tmp_path)
    assert cfg.harness_dir == tmp_path / ".metaharness"
    assert cfg.directives_dir == tmp_path / ".metaharness" / "directives"
    assert cfg.cycles_dir == tmp_path / ".metaharness" / "cycles"
    assert cfg.maintenance_cycles_dir == tmp_path / ".metaharness" / "cycles" / "maintenance"
    assert cfg.product_cycles_dir == tmp_path / ".metaharness" / "cycles" / "product"
    assert cfg.pending_product_veto_path.name == "PENDING_PRODUCT_VETO"
    assert cfg.memory_dir == tmp_path / ".metaharness" / "memory"
    assert cfg.reasoning_dir == tmp_path / ".metaharness" / "reasoning"
    assert cfg.prompts_dir == tmp_path / ".metaharness" / "prompts"
    assert cfg.kg_path == tmp_path / ".metaharness" / "knowledge_graph.db"


def test_dirs_created_on_load(tmp_path: Path):
    cfg = load_config(tmp_path)
    assert cfg.harness_dir.is_dir()
    assert cfg.prompts_dir.is_dir()
    assert cfg.reasoning_dir.is_dir()


def test_cursor_config_parsed(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        "[cursor]\nmodel = \"big\"\nmodel_fast = \"small\"\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.cursor.model == "big"
    assert cfg.cursor.model_fast == "small"


def test_cursor_max_files_per_cycle_default_and_toml(tmp_path: Path):
    assert load_config(tmp_path).cursor.max_files_per_cycle == 0
    (tmp_path / "metaharness.toml").write_text(
        "[cursor]\nmax_files_per_cycle = 25\n",
        encoding="utf-8",
    )
    assert load_config(tmp_path).cursor.max_files_per_cycle == 25


def test_cycle_schedule_loads_from_toml(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        '[cycle]\nschedule = ["07:00", " 12:30 ", ""]\n',
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.cycle.schedule == ["07:00", "12:30"]


def test_maintenance_section_merges_over_cycle(tmp_path: Path):
    toml = textwrap.dedent(
        """
        [cycle]
        interval_seconds = 100
        veto_seconds = 50
        schedule = ["08:00"]

        [maintenance]
        interval_seconds = 200
        slack_channel = "#ops"
        """
    )
    (tmp_path / "metaharness.toml").write_text(toml, encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg.cycle.interval_seconds == 200
    assert cfg.cycle.veto_seconds == 50
    assert cfg.cycle.schedule == ["08:00"]
    assert cfg.maintenance.slack_channel == "#ops"


def test_vision_and_product_load(tmp_path: Path):
    toml = textwrap.dedent(
        """
        [vision]
        statement = "Build the best CLI"
        target_users = "devs"
        features_wanted = ["auth", "export"]

        [product]
        enabled = true
        veto_seconds = 900
        schedule = ["10:00"]
        """
    )
    (tmp_path / "metaharness.toml").write_text(toml, encoding="utf-8")
    cfg = load_config(tmp_path)
    assert "CLI" in cfg.vision.statement
    assert cfg.vision.features_wanted == ["auth", "export"]
    assert cfg.product.enabled is True
    assert cfg.product.veto_seconds == 900
    assert cfg.product.schedule == ["10:00"]


def test_slack_notify_research_queue_from_toml(tmp_path: Path):
    (tmp_path / "metaharness.toml").write_text(
        "[slack]\nenabled = false\nnotify_research_queue = false\n",
        encoding="utf-8",
    )
    cfg = load_config(tmp_path)
    assert cfg.slack.notify_research_queue is False
