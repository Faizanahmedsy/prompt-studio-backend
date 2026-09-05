"""`docs/frontend/` and the copy living in the Next app must not diverge.

The TypeScript client is a hand-written mirror of the pydantic schemas, kept in
this repo so it sits next to the contracts it mirrors, and copied into the app.
Two copies of anything drift; this turns "remember to sync them" into a test.

The only difference allowed is the import rewrite the drop-in README documents:
relative paths here (`../api/client`) become the app's alias (`@/lib/api/client`)
on the way across, and the app's formatter reorders them afterwards. Comparing
import lines would therefore fail on a difference that carries no meaning, so
they are excluded and everything else has to match exactly.

Skipped when the sibling repo is not checked out, so this suite still runs for
anyone who cloned the backend on its own.
"""

import re
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_ROOT = BACKEND_ROOT.parent / "prompt-studio"

# canonical copy here -> where it lives in the app
PAIRS = {
    "api/types.ts": "lib/api/types.ts",
    "api/client.ts": "lib/api/client.ts",
    "api/token-store.ts": "lib/api/token-store.ts",
    "api/auth.ts": "lib/api/auth.ts",
    "api/projects.ts": "lib/api/projects.ts",
    "api/admin.ts": "lib/api/admin.ts",
    "api/discovery.ts": "lib/api/discovery.ts",
    "api/tokens.ts": "lib/api/tokens.ts",
    "stores/auth-store.ts": "stores/use-auth-store.ts",
    "collab/use-collaboration.ts": "features/collab/use-collaboration.ts",
}

# An import statement, however many lines it is spread over. `[^;]*?` is what
# bounds it: a statement contains no semicolon before its `from`, so this cannot
# run past the end of one and swallow the code after it.
IMPORT_STATEMENT = re.compile(
    r"""^[ \t]*(?:import|export)\b[^;]*?\bfrom[ \t]+["'][^"']+["'];?[ \t]*$""",
    re.MULTILINE,
)
# A side-effect import, which has no `from` at all.
BARE_IMPORT = re.compile(r"""^[ \t]*import[ \t]+["'][^"']+["'];?[ \t]*$""", re.MULTILINE)


def meaningful_lines(source: str) -> list[str]:
    """Everything except import statements and blank lines.

    Whole statements, not lines: a multi-line `import type { … }` is reordered
    by the app's formatter, so comparing its individual lines would fail on a
    difference that carries no meaning.
    """
    stripped = BARE_IMPORT.sub("", IMPORT_STATEMENT.sub("", source))
    return [line.rstrip() for line in stripped.splitlines() if line.strip()]


@pytest.mark.skipif(
    not FRONTEND_ROOT.is_dir(),
    reason="the prompt-studio app is not checked out beside this repo",
)
@pytest.mark.parametrize(("canonical", "copied"), sorted(PAIRS.items()))
def test_the_client_matches_the_copy_in_the_app(canonical: str, copied: str) -> None:
    source = BACKEND_ROOT / "docs" / "frontend" / canonical
    target = FRONTEND_ROOT / copied
    assert source.is_file(), f"docs/frontend/{canonical} is missing"
    assert target.is_file(), f"the app is missing {copied}"

    here = meaningful_lines(source.read_text())
    there = meaningful_lines(target.read_text())

    only_here = [line for line in here if line not in there][:6]
    only_there = [line for line in there if line not in here][:6]
    detail = "\n".join(
        [f"  only in docs/frontend: {line}" for line in only_here]
        + [f"  only in the app:      {line}" for line in only_there]
    )
    assert here == there, (
        f"docs/frontend/{canonical} and the app's {copied} have diverged.\n"
        f"This repo's copy is the canonical one — copy whichever is correct over the other.\n"
        f"{detail}"
    )


@pytest.mark.skipif(not FRONTEND_ROOT.is_dir(), reason="the app is not checked out")
def test_every_client_file_has_a_home() -> None:
    """A new file under `docs/frontend` that nobody copied across is a file the
    app is silently missing."""
    shipped = {
        str(path.relative_to(BACKEND_ROOT / "docs" / "frontend"))
        for path in (BACKEND_ROOT / "docs" / "frontend").rglob("*.ts")
        # The shim exists only so this folder typechecks inside this repo, where
        # react and zustand are not installed. The README says not to copy it.
        if path.name != "react-shim.d.ts"
    }
    assert shipped == set(PAIRS), (
        f"unmapped files under docs/frontend: {sorted(shipped - set(PAIRS))}"
    )
