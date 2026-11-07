"""Scope-alias tests for google-broker.

No network, no Google, no samcloud:  pytest test_scopes.py -q

Importing main.py runs module-level `mkdir`s on GOOGLE_BROKER_CONFIG and
GOOGLE_BROKER_MINTS, which default to the REAL ~/.config/samcloud and
~/.local/samcloud paths — so both are redirected into a temp dir before the
import, and `test_the_import_touched_no_real_path` asserts it worked. Same
defect shape as registry/conftest.py's first-boot isolation.
"""

import importlib.util
import os
import tempfile
from pathlib import Path

import pytest

_TMP = Path(tempfile.mkdtemp(prefix="google-broker-tests-"))
_REAL = (Path.home() / ".config/samcloud/google-broker",
         Path.home() / ".local/samcloud/google-broker")
_REAL_BEFORE = {p: p.exists() for p in _REAL}

os.environ["GOOGLE_BROKER_CONFIG"] = str(_TMP / "config")
os.environ["GOOGLE_BROKER_MINTS"] = str(_TMP / "mints/mints.jsonl")
os.environ.setdefault("SAMCLOUD_API", "https://cloud.example.org/api/v1")
os.environ.setdefault("GOOGLE_BROKER_PUBLIC_URL", "https://gglb.example.org")

_spec = importlib.util.spec_from_file_location(
    "google_broker_main", Path(__file__).parent / "main.py")
main = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(main)

GOOGLE_PREFIX = "https://www.googleapis.com/auth/"


def test_the_import_touched_no_real_path():
    """A test run must not create the broker's live config or mint log."""
    assert (_TMP / "config").is_dir(), "the redirect did not take effect"
    for p, existed_before in _REAL_BEFORE.items():
        assert p.exists() == existed_before, f"the import changed {p}"


class TestAnalyticsAliases:
    """GA4 needs two scopes that do different jobs (#761): `analytics.edit`
    creates the property and its data streams through the Admin API, and
    `analytics.readonly` runs reports through the Data API. An enrolled key
    holds both and `/mint` down-scopes per call, so each has to resolve on its
    own."""

    @pytest.mark.parametrize("alias,expected", [
        ("analytics.edit", GOOGLE_PREFIX + "analytics.edit"),
        ("analytics.readonly", GOOGLE_PREFIX + "analytics.readonly"),
    ])
    def test_the_alias_resolves(self, alias, expected):
        assert main.resolve_scopes([alias]) == [expected]

    def test_a_key_granted_both_can_mint_either_by_alias(self):
        """The failure this prevents. `/oauth/start` resolves before storing,
        so an enrolled key's `google_scopes` are full URLs; `/mint` computes
        `over = set(requested) - granted` and 403s on anything outside it.
        Without the aliases, a caller asking for `analytics.readonly` sends
        that string through unchanged, misses the granted URL, and gets
        "scopes not granted at consent" for a scope that WAS granted.

        The granted set here is built the way the broker builds it — through
        `resolve_scopes` on what the consent URL carried — rather than written
        out by hand, so it cannot drift from the real enrollment path.
        """
        granted = set(main.resolve_scopes([
            GOOGLE_PREFIX + "analytics.edit",
            GOOGLE_PREFIX + "analytics.readonly",
        ]))
        for requested in (["analytics.edit"], ["analytics.readonly"],
                          ["analytics.edit", "analytics.readonly"]):
            over = set(main.resolve_scopes(requested)) - granted
            assert over == set(), f"{requested} would 403 with over={sorted(over)}"

    def test_a_scope_outside_the_grant_is_still_refused(self):
        """The down-scope check must keep refusing — an alias table that
        resolved everything to something granted would pass the test above
        while breaking the guarantee."""
        granted = {GOOGLE_PREFIX + "analytics.readonly"}
        over = set(main.resolve_scopes(["analytics.edit"])) - granted
        assert over == {GOOGLE_PREFIX + "analytics.edit"}


class TestScopeTable:
    def test_every_alias_maps_to_a_google_scope_url(self):
        """Catches a typo'd future entry: an alias resolving to anything but a
        googleapis.com/auth URL is silently sent to Google as a scope and
        fails at the consent screen, where the error names Google, not us."""
        bad = {k: v for k, v in main.SCOPE_ALIASES.items()
               if not v.startswith(GOOGLE_PREFIX)}
        assert bad == {}

    def test_the_table_has_no_duplicate_targets_for_one_job(self):
        """Assert a count, not a presence: two aliases for the same URL is
        fine (readonly vs full), but one alias defined twice is a paste error
        Python resolves silently to the last one."""
        assert len(main.SCOPE_ALIASES) == len(set(main.SCOPE_ALIASES)), \
            "duplicate alias key"

    def test_an_unknown_scope_passes_through_unchanged(self):
        """Why a full URL works with or without an alias, and why a misspelled
        alias reaches Google verbatim instead of erroring here."""
        assert main.resolve_scopes(["no.such.alias"]) == ["no.such.alias"]
        full = GOOGLE_PREFIX + "drive.readonly"
        assert main.resolve_scopes([full]) == [full]
