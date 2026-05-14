# Worker Images

Dark Factory runs all agent and target-repository work inside a per-workflow
Docker worker container. The worker image is now configurable, so language
support is an image concern rather than a hard-coded Java assumption.

## Default Image

The default image tag is:

```bash
darkfactory-worker:polyglot
```

Build it before starting runs that need a worker:

```bash
docker compose --profile worker-image build darkfactory-worker-image
docker compose up -d
```

The default `Dockerfile.worker` is a polyglot baseline:

- Dark Factory worker runtime: Python 3.13, `uv`, Claude Code, Temporal SDK.
- Shared repo tools: `git`, `gh`, `ripgrep`, `make`, build essentials.
- Java: Temurin JDK 21, Maven, Gradle.
- TypeScript / Node: Node.js, npm, Corepack for package-manager shims.
- Python target projects: Python 3.13, pip, `uv`.

## Configure A Different Image

Set `DARKFACTORY_WORKER_IMAGE` in the orchestrator environment:

```bash
DARKFACTORY_WORKER_IMAGE=acme/darkfactory-worker-node:2026-05 docker compose up -d
```

For compose-built local images, use the same variable when building so the tag
matches what the orchestrator later launches:

```bash
DARKFACTORY_WORKER_IMAGE=acme/darkfactory-worker-python:dev \
  docker compose --profile worker-image build darkfactory-worker-image
```

## Custom Image Contract

A custom worker image can add or remove target language toolchains, but it must
keep the Dark Factory runtime contract:

- `python` must satisfy `pyproject.toml` (`>=3.13` today).
- `/opt/darkfactory` must contain the installed Dark Factory project.
- `/workspace` must exist and be writable by UID/GID `1000:1000`.
- User `1000:1000` must be able to run `git`, `gh`, `claude`, and `uv`.
- The container command must start `darkfactory.runtime.worker_main`.

The smallest customization is usually to build from the default image:

```Dockerfile
FROM darkfactory-worker:polyglot
USER root
RUN apt-get update && apt-get install -y --no-install-recommends golang rustc cargo \
    && rm -rf /var/lib/apt/lists/*
USER agent
```
