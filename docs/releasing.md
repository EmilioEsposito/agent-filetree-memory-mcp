# Releasing to PyPI

Releases use GitHub Actions and PyPI Trusted Publishing. The workflow never
stores a long-lived PyPI API token. Publishing a GitHub release tests the
package, builds and checks its distributions, and publishes them to PyPI.

## One-time account setup

1. Review the repository and its Git history for secrets, then make the GitHub
   repository public. PyPI distributions are public, and the project metadata
   links users back to this repository. If the repository must remain private,
   remove those public-facing links and confirm that the GitHub plan supports
   environments for private repositories before continuing.
2. Create an account on [PyPI](https://pypi.org/account/register/). Verify its
   email address, configure two-factor authentication, and securely store the
   recovery codes.
3. In this GitHub repository, create an environment named `pypi`. When the
   repository and GitHub plan support it, add a required reviewer so a
   production upload needs an additional manual approval. Do not add API-token
   secrets.
4. On [PyPI's pending publisher page](https://pypi.org/manage/account/publishing/),
   register these exact values:

   | Field | Value |
   | --- | --- |
   | PyPI project name | `agent-filetree-memory-mcp` |
   | Owner | `EmilioEsposito` |
   | Repository | `agent-filetree-memory-mcp` |
   | Workflow | `publish.yml` |
   | Environment | `pypi` |

The first successful trusted upload creates the project. A missing public PyPI
project page suggests that the name is available, but the first upload is the
definitive availability check.

## Preflight a release

Choose a version that has never been uploaded to the target index. PyPI release
filenames and versions cannot be replaced. Update `project.version` in
`pyproject.toml`; the runtime `__version__` is read from the installed package
metadata automatically. Then run:

```shell
uv run --locked pytest
uv run python -m devtools.postgres
uv run --locked python -m build
uvx --from twine twine check --strict dist/*
```

Inspect the pending changes and merge the reviewed change after CI passes.

## Publish the production release

The examples below use the repository's current version, `0.5.0`. Substitute
the version being released on later runs.

1. Confirm that CI passes on the commit to be released.
2. Create a GitHub release for a tag named exactly `v<version>`; for version
   `0.5.0`, the tag must be `v0.5.0`. Target the preflighted commit.
3. Publish the GitHub release. The workflow tests all supported Python
   versions, verifies that the tag and package versions match, builds and
   checks the artifacts, and enters the `pypi` environment.
4. If the environment has a required reviewer, review and approve the
   deployment in GitHub. This performs the irreversible upload.
5. Verify the PyPI project page and install from the public index in a fresh
   environment:

```shell
uv venv /tmp/agent-filetree-memory-pypi
uv pip install \
  --python /tmp/agent-filetree-memory-pypi/bin/python \
  'agent-filetree-memory-mcp[all]==0.5.0'
/tmp/agent-filetree-memory-pypi/bin/python -c \
  'import agent_filetree_memory; print(agent_filetree_memory.__version__)'
```

If a published release is broken, do not delete and reuse its version. Yank it
on PyPI, fix the problem, increment the version, and publish a new release.
