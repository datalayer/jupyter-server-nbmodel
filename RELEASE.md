# Making a new release of jupyter_server_nbmodel

## Automated release (tags)

A pushed tag `vX.Y.Z` publishes `jupyter_server_nbmodel` to PyPI and `@datalayer/jupyter-server-nbmodel` to npm, and creates the GitHub release, through [`.github/workflows/release.yaml`](.github/workflows/release.yaml). No token is stored: both registries trust that workflow (trusted publishing, OIDC).

1. Bump the version to `X.Y.Z` in `package.json`, the single source: `hatch-nodejs-version` gives it to the Python package and writes `jupyter_server_nbmodel/_version.py` at build time. That file is generated and not committed.
2. Add the release's entry to `CHANGELOG.md`, open a pull request with both, and merge it.
3. Tag the merge commit on `main` and push the tag. The tag must carry the `v`: `release.yaml` triggers on `v*` only, and a bare tag publishes nothing.

   ```bash
   git switch main && git pull
   git tag vX.Y.Z && git push origin vX.Y.Z
   ```

4. Check the published packages: `npm view @datalayer/jupyter-server-nbmodel@X.Y.Z version` and `pip index versions jupyter-server-nbmodel`.

What the workflow does:

- **check_version**: refuses a tag that differs from the version in `package.json`, before anything runs.
- **build**: calls [`.github/workflows/build.yaml`](.github/workflows/build.yaml) on the tagged commit, so the lint, the tests and the integration tests must pass. Only the link check is left out: a dead link is no reason to hold a release back. Its `package` job, which also runs on every pull request and on `main`, builds what is published: it installs JupyterLab for `jlpm`; runs `jlpm` and `jlpm build:prod` and packs the npm tarball with `npm pack`; runs `jlpm clean:all`, then `python -m build`, which builds the sdist and, from it, the wheel; runs `twine check` and `check-wheel-contents`; and checks that the wheel holds the labextension and the server configuration, that the sdist holds the labextension, and that they carry the version.
- **pypi**: publishes the sdist and the wheel with `pypa/gh-action-pypi-publish`, in the `pypi` environment, with `id-token: write`.
- **npm**: in the `npm` environment, with `id-token: write`, installs npm 11.5.1 or later, which trusted publishing needs, then runs `npm publish <tarball> --access public --provenance`.
- **github_release**: once both registries have the release, creates the GitHub release from the tag, with generated notes and the packages attached.

The pypi and npm jobs run independently: if one fails, the other still publishes. Re-running the failed job publishes the same packages.

One-time setup, done once per registry:

- **PyPI**: on `https://pypi.org/manage/project/jupyter-server-nbmodel/settings/publishing/`, a GitHub trusted publisher with owner `datalayer`, repository `jupyter-server-nbmodel`, workflow `release.yaml` and environment `pypi`.
- **npm**: in the settings of `@datalayer/jupyter-server-nbmodel` on npmjs.com, under _Trusted Publisher_, GitHub Actions with organization `datalayer`, repository `jupyter-server-nbmodel`, workflow `release.yaml` and environment `npm`.

Renaming the workflow file or an environment breaks the release: the registries refuse the OIDC token until they are pointed at the new name.

## Manual release

The steps below still work, for example for a release cut from a machine.

### Python package

All of the Python packaging instructions are in the `pyproject.toml` file. Before generating a package, you first need to install some tools:

```bash
pip install build twine
```

Make sure to clean up all the development files before building the package:

```bash
jlpm clean:all
```

You could also clean up the local git repository:

```bash
git clean -dfX
```

To create a Python source package (`.tar.gz`) and the binary package (`.whl`) in the `dist/` directory, do:

```bash
python -m build
```

> `python setup.py sdist bdist_wheel` is deprecated and will not work for this package.

Then to upload the package to PyPI, do:

```bash
twine upload dist/*
```

### NPM package

To publish the frontend part of the extension as a NPM package, do:

```bash
jlpm
jlpm build:prod
npm login
npm publish --access public
```

## Publishing to `conda-forge`

If the package is not on conda forge yet, check the documentation to learn how to add it: https://conda-forge.org/docs/maintainer/adding_pkgs.html

Otherwise a bot should pick up the new version publish to PyPI, and open a new PR on the feedstock repository automatically.
