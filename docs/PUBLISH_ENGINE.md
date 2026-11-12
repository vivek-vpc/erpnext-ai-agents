# Publishing the engine to PyPI: the steps that are yours

The workflow `.github/workflows/publish-engine.yml` builds `ai-munimji-engine` from `engine/` and publishes it when a tag `engine-vX.Y.Z` is pushed. It stores no token: PyPI trusts this workflow ("trusted publishing"). On every pull request the same file runs as a dry run (build, `twine check`, and a check that the names below are still in it). Version 0.1.0 was published through it on 9 Nov 2026 (tag `engine-v0.1.0`) and cannot be changed; the examples below are for 0.1.3, the version that carries Telegram.

1. PyPI account with two-factor authentication. *Done by you (reported 9 Nov 2026).*
2. On PyPI, a "pending publisher": project `ai-munimji-engine`, owner `vivek-vpc`, repository `erpnext-ai-agents`, workflow `publish-engine.yml`, environment `pypi`. *Done by you (reported 9 Nov 2026).*
3. On GitHub, in `vivek-vpc/erpnext-ai-agents`: Settings, Environments, New environment, name it exactly `pypi`, tick "Required reviewers" and add yourself. Also limit "Deployment branches and tags" to the tag pattern `engine-v*`. *Yours: not done by me.*
4. Before tagging: the pull request with this workflow is merged, CI on main is green, and the version in `engine/pyproject.toml` (now 0.1.3) is the one you mean, with the changelog saying so.
5. Tag and push: `git tag engine-v0.1.3`, then `git push origin engine-v0.1.3`. The tag must be `engine-v` plus that version; the workflow refuses to publish otherwise.
6. In the run on GitHub Actions, approve the `pypi` environment when it asks. The workflow publishes. Then check https://pypi.org/project/ai-munimji-engine/ and, in a clean environment, `pip install ai-munimji-engine==0.1.3`.
7. PyPI never accepts the same file name twice: a mistake means a new version and a new tag, never a re-publish.

If a name in the workflow has to change, change it on PyPI first or at the same time; the dry run fails when the file and `tools/check_publish_workflow.py` disagree.
