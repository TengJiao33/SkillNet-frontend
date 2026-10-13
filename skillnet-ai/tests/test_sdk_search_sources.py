"""Search source metadata is usable in Python, JSON and terminal output."""

import json
from unittest.mock import Mock

import pytest
import requests
from rich.console import Console
from typer.testing import CliRunner

from skillnet_ai.core.models import SkillModel
from skillnet_ai.interfaces import cli
from skillnet_ai.interfaces.client import SkillNetClient, SkillNetError


@pytest.fixture
def search_response(monkeypatch):
    rows = [
        {
            "skill_name": "example",
            "repo_name": "owner/repo",
            "skill_dir": f"owner/repo/releases/{version}/example",
            "skill_url": f"https://github.com/owner/repo/tree/commit/releases/{version}/example",
            "stars": 10,
        }
        for version in ("v1", "v2")
    ]
    # Old servers omit the fields; local/non-GitHub records may return null.
    rows.extend(
        [{"skill_name": "legacy"}, {"skill_name": "local", "repo_name": None, "skill_dir": None}]
    )
    response = requests.Response()
    response.status_code = 200
    response._content = json.dumps({"success": True, "data": rows, "meta": {}}).encode()
    get = Mock(return_value=response)
    monkeypatch.setattr(requests.Session, "get", get)
    return rows, get


def test_sdk_and_cli_json_preserve_source_identity(search_response):
    rows, get = search_response
    result = SkillNetClient().search("example")
    assert [(r.repo_name, r.skill_dir) for r in result] == [
        (r.get("repo_name"), r.get("skill_dir")) for r in rows
    ]
    assert get.call_count == 1
    assert get.call_args.kwargs["params"]["sort_by"] == "relevance"
    assert "sort_order" not in get.call_args.kwargs["params"]
    assert "repo_name" not in get.call_args.kwargs["params"]
    assert "skill_dir" not in get.call_args.kwargs["params"]
    output = CliRunner().invoke(cli.app, ["search", "example", "--json"])
    assert output.exit_code == 0, output.output
    data = json.loads(output.stdout)["data"]
    assert [(r["repo_name"], r["skill_dir"]) for r in data] == [
        (r.get("repo_name"), r.get("skill_dir")) for r in rows
    ]
    assert [r["skill_url"] for r in data] == [r.get("skill_url") for r in rows]


@pytest.mark.parametrize("mode", ["keyword", "vector"])
def test_both_modes_keep_the_existing_sdk_and_cli_output(search_response, monkeypatch, mode):
    rows, get = search_response
    response = get.return_value
    response._content = json.dumps(
        {
            "success": True,
            "data": [
                {
                    **row,
                    "relevance_score": 3.25 if mode == "keyword" else None,
                    "similarity": 0.9 if mode == "vector" else None,
                }
                for row in rows
            ],
            "meta": {},
        }
    ).encode()
    expected = [SkillModel(**row).model_dump() for row in rows]
    results = SkillNetClient().search("example", mode=mode)
    assert [row.model_dump() for row in results] == expected
    params = get.call_args.kwargs["params"]
    assert "sort_order" not in params
    if mode == "keyword":
        assert params["sort_by"] == "relevance"
    else:
        assert "sort_by" not in params

    output = CliRunner().invoke(cli.app, ["search", "example", "--mode", mode, "--json"])
    assert output.exit_code == 0, output.output
    assert json.loads(output.stdout)["data"] == expected

    monkeypatch.setattr(cli, "console", Console(width=180, force_terminal=False, highlight=False))
    output = CliRunner().invoke(cli.app, ["search", "example", "--mode", mode])
    assert output.exit_code == 0, output.output
    assert "Repo Stars" in output.stdout and "Description" in output.stdout
    assert "relevance_score" not in output.stdout and "similarity" not in output.stdout


@pytest.mark.parametrize("sort_by", ["stars", "recent"])
def test_explicit_keyword_sorts_remain_available(search_response, sort_by):
    _, get = search_response
    result = CliRunner().invoke(cli.app, ["search", "example", "--sort-by", sort_by])
    assert result.exit_code == 0, result.output
    assert get.call_args.kwargs["params"]["sort_by"] == sort_by
    assert "sort_order" not in get.call_args.kwargs["params"]


def test_cli_has_no_sort_order_option():
    help_result = CliRunner().invoke(cli.app, ["search", "--help"])
    assert help_result.exit_code == 0
    assert "--sort-order" not in help_result.stdout
    result = CliRunner().invoke(cli.app, ["search", "example", "--sort-order", "asc"])
    assert result.exit_code == 2


@pytest.mark.parametrize("mode", ["keyword", "vector"])
@pytest.mark.parametrize("limit", [0, 51, 100])
def test_invalid_limit_is_rejected_before_http(search_response, mode, limit):
    _, get = search_response
    with pytest.raises(SkillNetError, match="limit 1–50"):
        SkillNetClient().search("example", mode=mode, limit=limit)
    result = CliRunner().invoke(
        cli.app, ["search", "example", "--mode", mode, "--limit", str(limit)]
    )
    assert result.exit_code == 2
    get.assert_not_called()


@pytest.mark.parametrize("mode", ["keyword", "vector"])
@pytest.mark.parametrize("limit", [1, 50])
def test_public_limit_boundaries_reach_the_service(search_response, mode, limit):
    _, get = search_response
    SkillNetClient().search("example", mode=mode, limit=limit)
    assert get.call_args.kwargs["params"]["limit"] == limit
    result = CliRunner().invoke(
        cli.app, ["search", "example", "--mode", mode, "--limit", str(limit)]
    )
    assert result.exit_code == 0, result.output
    assert get.call_args.kwargs["params"]["limit"] == limit


def test_cli_displays_sources_without_changing_download_urls(search_response, monkeypatch):
    monkeypatch.setattr(cli, "console", Console(width=180, force_terminal=False, highlight=False))
    output = CliRunner().invoke(cli.app, ["search", "example"])
    assert output.exit_code == 0, output.output
    assert "Source" in output.stdout
    assert "Repo Stars" in output.stdout
    assert "owner/repo" in output.stdout
    assert "releases/v1/example" in output.stdout
    assert "releases/v2/example" in output.stdout
    assert "legacy" in output.stdout and "local" in output.stdout


def test_cli_treats_source_metadata_as_literal_text(search_response, monkeypatch):
    rows, _ = search_response
    rows[0]["repo_name"] = "owner/[demo]"
    rows[0]["skill_dir"] = "owner/[demo]/[red]example[/red]"
    monkeypatch.setattr(SkillNetClient, "search", lambda *a, **k: [SkillModel(**rows[0])])
    monkeypatch.setattr(cli, "console", Console(width=180, force_terminal=False, highlight=False))
    output = CliRunner().invoke(cli.app, ["search", "example"])
    assert output.exit_code == 0, output.output
    assert "[demo]" in output.stdout
    assert "[red]example[/red]" in output.stdout


def test_cli_keeps_long_sources_readable_in_a_narrow_terminal(monkeypatch):
    repo = "danielmiessler/Personal_AI_Infrastructure"
    directory = "Releases/v4.0.2/.claude/skills/Security/PromptInjection"
    row = SkillModel(
        skill_name="PromptInjection",
        repo_name=repo,
        skill_dir=f"{repo}/{directory}",
        skill_description="Prompt injection testing.",
        skill_url=f"https://github.com/{repo}/tree/commit/{directory}",
    )
    monkeypatch.setattr(SkillNetClient, "search", lambda *a, **k: [row])
    monkeypatch.setattr(cli, "console", Console(width=80, force_terminal=False, highlight=False))
    output = CliRunner().invoke(cli.app, ["search", "prompt injection"])
    assert output.exit_code == 0, output.output
    # Reassemble the wrapped source cell; truncation would lose real characters.
    identity = "".join(
        line.split("│")[1].strip() for line in output.stdout.splitlines() if line.startswith("│")
    )
    assert repo in identity and directory in identity


@pytest.mark.parametrize("directory", ["owner/repo", "owner/repo/"])
def test_cli_displays_repository_root_as_dot(directory, monkeypatch):
    row = SkillModel(skill_name="example", repo_name="owner/repo", skill_dir=directory)
    monkeypatch.setattr(SkillNetClient, "search", lambda *a, **k: [row])
    monkeypatch.setattr(cli, "console", Console(width=140, force_terminal=False, highlight=False))
    output = CliRunner().invoke(cli.app, ["search", "example"])
    assert output.exit_code == 0, output.output
    cells = [
        line.split("│")[1].strip() for line in output.stdout.splitlines() if line.startswith("│")
    ]
    assert cells.count("owner/repo") == 1 and "." in cells
