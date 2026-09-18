"""The deploy job must ship the image its own pipeline built.

This is guarding a bug that already happened: the job pulled whatever TALAIA_TAG said in
the server's .env, that value was pinned to an old release, and so the deploy button
redeployed a five-week-old image and went green doing it. Nothing in the pipeline could
have caught it, because the job did exactly what it was told.
"""

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
PIPELINE_PATH = REPO_ROOT / ".gitlab-ci.yml"


@pytest.fixture(scope="module")
def pipeline() -> dict[str, Any]:
    return yaml.safe_load(PIPELINE_PATH.read_text(encoding="utf-8"))  # type: ignore[no-any-return]


@pytest.fixture(scope="module")
def deploy_script(pipeline: dict[str, Any]) -> str:
    return "\n".join(pipeline["production"]["script"])


class TestDeployTag:
    def test_the_tag_comes_from_the_pipeline(self, deploy_script: str) -> None:
        assert 'tag="${CI_COMMIT_TAG:-$CI_COMMIT_SHORT_SHA}"' in deploy_script

    def test_no_version_is_hard_coded(self, deploy_script: str) -> None:
        """A literal vX.Y.Z here would pin production the same way .env did."""
        assert not re.search(r"\bv\d+\.\d+\.\d+\b", deploy_script)

    def test_the_tag_is_written_back_to_the_env_file(self, deploy_script: str) -> None:
        """Exporting it for one command leaves .env stale.

        The next manual `docker compose up -d` on the host would roll production back.
        """
        assert 'echo "TALAIA_TAG=$tag"' in deploy_script

    def test_the_env_file_is_rewritten_in_place(self, deploy_script: str) -> None:
        """A mv would hand .env to the runner user with mktemp's 0600 and lock out its owner."""
        assert 'cat "$tmp" > .env' in deploy_script
        assert 'mv "$tmp" .env' not in deploy_script


class TestDeployGate:
    def test_production_waits_for_a_person(self, pipeline: dict[str, Any]) -> None:
        rules = pipeline["production"]["rules"]

        assert [rule["when"] for rule in rules] == ["manual", "manual"]

    def test_only_main_and_tags_can_deploy(self, pipeline: dict[str, Any]) -> None:
        conditions = " ".join(rule["if"] for rule in pipeline["production"]["rules"])

        assert "$CI_COMMIT_BRANCH == $CI_DEFAULT_BRANCH" in conditions
        assert "$CI_COMMIT_TAG" in conditions

    def test_the_image_it_deploys_is_one_the_build_pushed(self, pipeline: dict[str, Any]) -> None:
        """The two jobs agree on the tag, or the deploy pulls something that is not there."""
        build = "\n".join(pipeline["build-image"]["script"])

        assert '"$CI_REGISTRY_IMAGE:$CI_COMMIT_SHORT_SHA"' in build
        assert '"$CI_REGISTRY_IMAGE:$CI_COMMIT_TAG"' in build
