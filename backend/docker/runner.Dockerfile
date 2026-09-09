# PatchPilot sandbox runner image.
#
# The Docker sandbox runs with `--network none`, which is the correct posture
# for untrusted repository code but means **nothing can be installed at run
# time**. Everything the agent needs to inspect a repository and run its tests
# must already be in the image.
#
# Build:
#   docker build -f backend/docker/runner.Dockerfile -t patchpilot/runner:latest backend
#
# Then:
#   PATCHPILOT_SANDBOX=docker
#   PATCHPILOT_DOCKER_IMAGE=patchpilot/runner:latest
#
# For a repository with its own dependencies, derive from this image and add
# them, then point PATCHPILOT_DOCKER_IMAGE at your derived tag:
#
#   FROM patchpilot/runner:latest
#   COPY requirements.txt /tmp/
#   RUN pip install --no-cache-dir -r /tmp/requirements.txt

FROM python:3.11-slim

# Node.js, so a JS/TS repository has a runtime at all. Copied from the official
# image rather than apt so the version is current and the layer is small.
# NB: this gives node/npm/npx, NOT a repository's node_modules -- with
# --network none those cannot be installed at run time either. A JS project
# with dependencies still needs a derived image that has run `npm ci`.
COPY --from=node:20-slim /usr/local/bin/node /usr/local/bin/node
COPY --from=node:20-slim /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s /usr/local/lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm     && ln -s /usr/local/lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

# git: the workspace layer uses it for diff, apply, status and checkout.
# The rest are what a Python test suite typically shells out to.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git \
        ca-certificates \
        make \
    && rm -rf /var/lib/apt/lists/*

# Test runners and the assertion/mocking libraries most projects expect.
RUN pip install --no-cache-dir \
        pytest \
        pytest-timeout \
        pytest-xdist \
        hypothesis

# A stable, non-interactive git identity: `git apply --3way` and `git stash`
# refuse to run without one, and the sandbox has no user config.
RUN git config --system user.email "agent@patchpilot.local" \
    && git config --system user.name "PatchPilot Agent" \
    && git config --system --add safe.directory '*' \
    && git config --system init.defaultBranch main

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    NO_COLOR=1

WORKDIR /workspace
