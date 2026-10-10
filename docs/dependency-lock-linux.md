# Linux / Python 3.12 dependency-lock procedure

**Status:** No dependency lock has been generated or validated in this repository yet. The current source inputs are [`requirements.txt`](../requirements.txt) (production) and [`requirements-dev.txt`](../requirements-dev.txt) (development/tests). Both contain version ranges; neither is a reproducible lock. In particular, do **not** change the production Dockerfile to install a lock file until the lock has actually been generated, reviewed, committed, and tested in the intended Linux build environment.

Run the following from the repository root on the target **Linux x86_64, Python 3.12** environment (preferably the same base distribution and Python image used for deployment). Do not generate the lock on Windows: platform markers and available distributions can resolve differently. `python3` below must refer to Python 3.12; the assertion stops the process otherwise. No local absolute Python executable path is needed. Docker is **not** required for this procedure.

## Generate (on Linux only)

```sh
python3 -m venv .venv-lock-tools
./.venv-lock-tools/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'
./.venv-lock-tools/bin/python -m pip install --upgrade pip
./.venv-lock-tools/bin/python -m pip install 'pip-tools==7.4.1'
./.venv-lock-tools/bin/pip-compile --resolver=backtracking --generate-hashes --output-file=requirements-linux-py312.txt requirements.txt
./.venv-lock-tools/bin/pip-compile --resolver=backtracking --generate-hashes --output-file=requirements-dev-linux-py312.txt requirements-dev.txt
```

The two **prospective** outputs are `requirements-linux-py312.txt` and `requirements-dev-linux-py312.txt`. The development input already includes the production input; compile and validate it separately rather than installing both generated files together. `--generate-hashes` permits hash-checked installation and records the resolved transitive versions. Compilation is a network operation and can depend on the package index and the exact Linux environment. Review the complete diffs, including hashes, package sources and transitive versions, before committing any generated file. Re-run this procedure after changing either source input or deliberately refreshing pinned versions. Do not hand-edit resolved version or hash lines.

## Validate generated files before using them

Use clean, separate Linux Python 3.12 virtual environments. The following commands must succeed for **both** outputs; a missing lock file is a failure, not a reason to fall back silently to unpinned installation.

```sh
python3 -m venv .venv-lock-prod
./.venv-lock-prod/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'
./.venv-lock-prod/bin/python -m pip install --upgrade pip
./.venv-lock-prod/bin/python -m pip install --require-hashes -r requirements-linux-py312.txt
./.venv-lock-prod/bin/python -m pip check

python3 -m venv .venv-lock-dev
./.venv-lock-dev/bin/python -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version'
./.venv-lock-dev/bin/python -m pip install --upgrade pip
./.venv-lock-dev/bin/python -m pip install --require-hashes -r requirements-dev-linux-py312.txt
./.venv-lock-dev/bin/python -m pip check
./.venv-lock-dev/bin/python -m pytest -q
```

These steps verify resolution, wheel/sdist hashes, dependency compatibility and the test suite on that Linux host; they do not establish cross-platform portability or prove that an untested production image installs successfully. The validation environments and tool environment are disposable (`.venv-lock-tools`, `.venv-lock-prod`, `.venv-lock-dev`); do not commit virtual environments. Before adopting the production lock, separately verify the Docker image build on an available Linux Docker host and only then update the Dockerfile to copy/install the **existing** reviewed lock with `--require-hashes`. Until that follow-up is complete, the Dockerfile intentionally keeps installing `requirements.txt` as it does today.

CI can later add an explicit Linux lock-validation job **after** the files exist and have been reviewed. Do not make an unconditional CI or Docker dependency on these prospective filenames now.
