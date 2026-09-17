# Development

Requires [uv](https://docs.astral.sh/uv/) and Docker.

```sh
uv sync --all-groups          # create .venv and install everything, including dev tools
cp .env.example .env          # then edit it

uv run ruff check .           # lint
uv run ruff format .          # format
uv run mypy                   # type check, strict
uv run pytest                 # full suite
uv run pytest -m "not integration"   # fast inner loop, no Docker needed
```

The interpreter is Python 3.14, managed by uv — the system Python is not used. Integration
tests start a real PostgreSQL container through testcontainers, so the Docker daemon must be
reachable.

**Known gap in the test suite:** the ICMP checker's tests cover how icmplib's results and
errors are turned into check outcomes, but never send a real ping. Whether a ping succeeds
depends on kernel and container settings that cannot be relied on in CI, and faking it
would prove nothing. The HTTP and TCP checkers are tested against a mocked transport and a
real local socket respectively.

Run the same three commands CI runs before pushing, and the pipeline will rarely surprise
you.

## Contributing and conventions

- Everything is in English: code, comments, docstrings, commit messages, variable names.
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `chore:`, `docs:`, `test:`, `ci:`).
- The repository is treated as public from the first commit. Secrets are scanned by
  `gitleaks` in the pipeline. A secret that reaches git history must be rotated, whatever
  is done to the history afterwards.


## Dependency updates

`renovate.json` configures [Renovate](https://docs.renovatebot.com/) to watch
`pyproject.toml`, `uv.lock`, the Dockerfile, the compose files and `.gitlab-ci.yml`.

The shape of it, and why:

| | |
|---|---|
| Weekly, Monday before 6am | One batch to review, not a trickle all week |
| Minor and patch grouped | One reviewer; separate PRs for each would be noise |
| Majors need approval | These are the ones that need reading before merging |
| Security releases any time | A CVE should not wait for Monday |
| Python and `uv` need approval | Recorded decisions in TALAIA.md §20.1, not dependency bumps |
| Nothing automerges | The pipeline is good, but a green pipeline is not a review |

Renovate is not self-hosted here. Either run it as a scheduled GitLab pipeline using the
`renovate/renovate` image with a project access token, or point the hosted app at the
repository. Until then the file is inert and costs nothing — but the `uv.lock` in this
repository will age, and nothing else is watching it.
