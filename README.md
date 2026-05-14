# Dark Factory

Autonomous coding pipeline that turns a user prompt or labeled GitHub issue into a reviewed pull request via a Temporal-driven sequence of Claude Agent SDK roles.

See `CLAUDE.md` for architecture and operational details.

Worker language support is image-driven. The default worker is a configurable
polyglot image for Java, TypeScript/Node, and Python projects; see
[`docs/worker-images.md`](docs/worker-images.md).
