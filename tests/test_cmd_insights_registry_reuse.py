# SPDX-FileCopyrightText: 2026 Lokesh Selvam <lokeshselvam7025@gmail.com>
# SPDX-License-Identifier: Apache-2.0

"""CLI rendering of registry reuse suggestions and the cold-start note.

The web UI and the CLI must agree about when a component is safe to point a
user at: only a server-validated ``component_ref`` counts. These tests pin
that, plus the four "why was nothing reused" messages.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

from observal_cli import cmd_insights
from observal_cli.cmd_insights import insights_app

runner = CliRunner()

AGENT_ID = "c6185803-8c32-4c39-b347-78f8281e306e"
REPORT_ID = "be5aa083-d84a-49e7-8a35-b37b3e687780"
COMPONENT_ID = "0f2b8a1c-2f4d-4c0e-9f7a-1b2c3d4e5f60"


@pytest.fixture(autouse=True)
def _reset_registry_name(monkeypatch):
    """The registry name is cached per process; keep tests independent."""
    monkeypatch.setattr(cmd_insights, "_registry_name_cache", None)


def _report(narrative: dict) -> dict:
    return {
        "id": REPORT_ID,
        "agent_id": AGENT_ID,
        "status": "completed",
        "period_start": "2026-05-17T00:00:00Z",
        "period_end": "2026-05-31T00:00:00Z",
        "sessions_analyzed": 42,
        "llm_model_used": "test-model",
        "narrative": narrative,
    }


def _install_fake_get(monkeypatch, narrative: dict, *, branding: str | None = "Observal"):
    """Serve the three calls `insights show` makes, plus the branding lookup."""

    def fake_get(path: str, params: dict | None = None):
        if path == "/api/v1/agents/ultra-pi":
            return {"id": AGENT_ID, "name": "ultra-pi"}
        if path == f"/api/v1/agents/{AGENT_ID}/insights/reports":
            return [{"id": REPORT_ID, "status": "completed"}]
        if path == f"/api/v1/agents/{AGENT_ID}/insights/reports/{REPORT_ID}":
            return _report(narrative)
        if path == "/api/v1/config/public":
            if branding is None:
                raise RuntimeError("config unavailable")
            return {"branding_app_name": branding}
        raise AssertionError(f"unexpected path: {path}")

    monkeypatch.setattr("observal_cli.config.resolve_alias", lambda value: value)
    monkeypatch.setattr("observal_cli.cmd_insights.client.get", fake_get)


def _reuse_feature(**overrides) -> dict:
    feature = {
        "action_type": "reuse_existing_component",
        "feature": "Skill",
        "name": "terraform-review",
        "one_liner": "Reviews terraform plans before apply.",
        "match_reason": "Sessions repeatedly hand-review plan output.",
        "existing_component_id": COMPONENT_ID,
        "component_ref": {
            "type": "skill",
            "id": COMPONENT_ID,
            "name": "terraform-plan-review",
            "qualified_name": "super/terraform-plan-review",
            "latest_version": "1.0.0",
        },
    }
    feature.update(overrides)
    return feature


def _create_feature(name: str = "scope-guard") -> dict:
    return {
        "action_type": "create_new_skill",
        "feature": "Skill",
        "name": name,
        "one_liner": "Keeps edits inside the requested scope.",
        "why_for_you": "You corrected scope creep in 6 sessions.",
    }


# ── Reuse suggestions ──────────────────────────────────────────────────────


class TestReuseRendering:
    def test_validated_component_is_shown_as_already_in_registry(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {"suggestions": {"features_to_try": [_reuse_feature()]}},
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "ALREADY IN OBSERVAL" in result.output
        assert "super/terraform-plan-review" in result.output
        assert "v1.0.0" in result.output
        assert "Sessions repeatedly hand-review plan output." in result.output

    def test_reuse_is_rendered_before_create_new(self, monkeypatch):
        # The model emits create-new first; "you already own this" must lead.
        _install_fake_get(
            monkeypatch,
            {"suggestions": {"features_to_try": [_create_feature(), _reuse_feature()]}},
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert result.output.index("ALREADY IN OBSERVAL") < result.output.index("scope-guard")

    def test_suggestion_without_component_ref_gets_no_install_instructions(self, monkeypatch):
        # A rejected reuse keeps its text but loses its ids. Rendering it as a
        # registry component would send the user to install something fake.
        rejected = _reuse_feature(action_type="create_new_skill", component_ref=None, existing_component_id=None)
        _install_fake_get(monkeypatch, {"suggestions": {"features_to_try": [rejected]}})

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "ALREADY IN" not in result.output
        assert "observal agent add" not in result.output
        assert COMPONENT_ID not in result.output

    def test_non_dict_component_ref_is_ignored(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {"suggestions": {"features_to_try": [_reuse_feature(component_ref="super/thing")]}},
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "ALREADY IN" not in result.output

    def test_non_dict_feature_entries_do_not_crash(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {"suggestions": {"features_to_try": ["just a string", None, _reuse_feature()]}},
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "super/terraform-plan-review" in result.output

    def test_white_label_name_is_honoured(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {"suggestions": {"features_to_try": [_reuse_feature()]}},
            branding="Acme Hub",
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "ALREADY IN ACME HUB" in result.output

    def test_branding_lookup_failure_falls_back(self, monkeypatch):
        # A cosmetic label must never be the reason a report fails to render.
        _install_fake_get(
            monkeypatch,
            {"suggestions": {"features_to_try": [_reuse_feature()]}},
            branding=None,
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "ALREADY IN YOUR REGISTRY" in result.output


# ── Cold-start note ────────────────────────────────────────────────────────


class TestRegistryMatchNote:
    def test_searched_but_nothing_fit(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {
                "suggestions": {"features_to_try": [_create_feature()]},
                "registry_match": {"enabled": True, "offered": 12, "reused": 0, "registry_has_components": None},
            },
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "Checked 12 components already in Observal" in result.output

    def test_singular_component_reads_correctly(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {
                "suggestions": {"features_to_try": [_create_feature()]},
                "registry_match": {"enabled": True, "offered": 1, "reused": 0, "registry_has_components": None},
            },
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert "Checked 1 component already in Observal" in result.output

    def test_empty_registry_says_so(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {
                "suggestions": {"features_to_try": [_create_feature()]},
                "registry_match": {"enabled": True, "offered": 0, "reused": 0, "registry_has_components": False},
            },
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert "No components have been published to Observal yet" in result.output

    def test_thin_signal_asks_for_more_sessions(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {
                "suggestions": {"features_to_try": [_create_feature()]},
                "registry_match": {"enabled": True, "offered": 0, "reused": 0, "registry_has_components": True},
            },
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert "More sessions give the match more to go on" in result.output

    def test_disabled_feature_says_so(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {
                "suggestions": {"features_to_try": [_create_feature()]},
                "registry_match": {"enabled": False, "offered": 0, "reused": 0, "registry_has_components": None},
            },
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert "Reuse suggestions are turned off" in result.output

    def test_no_note_when_something_was_reused(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {
                "suggestions": {"features_to_try": [_reuse_feature()]},
                "registry_match": {"enabled": True, "offered": 2, "reused": 1, "registry_has_components": None},
            },
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert "Checked" not in result.output
        assert "super/terraform-plan-review" in result.output

    def test_reports_predating_the_feature_stay_silent(self, monkeypatch):
        # No registry_match key at all: say nothing rather than guess.
        _install_fake_get(monkeypatch, {"suggestions": {"features_to_try": [_create_feature()]}})

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "Checked" not in result.output
        assert "Reuse suggestions are turned off" not in result.output

    def test_note_renders_for_section_scoped_output(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {
                "suggestions": {"features_to_try": [_create_feature()]},
                "registry_match": {"enabled": True, "offered": 3, "reused": 0, "registry_has_components": None},
            },
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi", "--section", "suggestions"])

        assert result.exit_code == 0, result.output
        assert "Checked 3 components already in Observal" in result.output

    def test_malformed_summary_is_ignored(self, monkeypatch):
        _install_fake_get(
            monkeypatch,
            {
                "suggestions": {"features_to_try": [_create_feature()]},
                "registry_match": "not a dict",
            },
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "Checked" not in result.output

    def test_json_output_is_untouched(self, monkeypatch):
        narrative = {
            "suggestions": {"features_to_try": [_reuse_feature()]},
            "registry_match": {"enabled": True, "offered": 2, "reused": 1, "registry_has_components": None},
        }
        _install_fake_get(monkeypatch, narrative)

        result = runner.invoke(insights_app, ["show", "ultra-pi", "--output", "json"])

        assert result.exit_code == 0, result.output
        assert COMPONENT_ID in result.output


class TestLegacySectionShapes:
    def test_string_section_does_not_crash_typed_renderer(self, monkeypatch):
        # Older reports stored some sections as prose; `.get` would explode.
        _install_fake_get(monkeypatch, {"suggestions": "Try writing shorter prompts."})

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "Try writing shorter prompts." in result.output

    def test_list_section_does_not_crash_typed_renderer(self, monkeypatch):
        _install_fake_get(monkeypatch, {"suggestions": ["one", "two"]})

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output


class TestReuseBlockMarkupSafety:
    """Reuse text is LLM-written and must render literally, not as Rich markup."""

    HOSTILE = "Clean up [/tmp] and index array[0] with [bold] markers"

    def test_reuse_suggestion_text_renders_literally(self, monkeypatch):
        monkeypatch.setenv("COLUMNS", "300")
        _install_fake_get(
            monkeypatch,
            {
                "suggestions": {
                    "features_to_try": [
                        _reuse_feature(one_liner=self.HOSTILE, match_reason=self.HOSTILE),
                    ]
                }
            },
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert self.HOSTILE in " ".join(result.output.split())
        assert "ALREADY IN OBSERVAL" in result.output

    def test_hostile_component_name_cannot_forge_styling(self, monkeypatch):
        # Component names are server data, but must still never be able to
        # close the CLI's own markup tags.
        monkeypatch.setenv("COLUMNS", "300")
        ref = dict(_reuse_feature()["component_ref"], qualified_name="super/[/bold]evil")
        _install_fake_get(
            monkeypatch,
            {"suggestions": {"features_to_try": [_reuse_feature(component_ref=ref)]}},
        )

        result = runner.invoke(insights_app, ["show", "ultra-pi"])

        assert result.exit_code == 0, result.output
        assert "super/[/bold]evil" in " ".join(result.output.split())
