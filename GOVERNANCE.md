# Governance

Telegram Control is a maintainer-led project. Public source and open
contribution do not mean that every proposed behavior becomes a project
default.

## Decision authority

The repository owner is the initial lead maintainer and has final responsibility
for releases, security response, collaborator access, project scope, defaults,
and merges. Maintainers may delegate review or merge authority to trusted
contributors as the project grows.

External contributors do not need advance permission to fork the repository,
experiment, or open a pull request. Only maintainers merge into the canonical
repository. Contributors should not expect the lead maintainer personally to
implement or approve every idea.

## Product-change principles

Changes are favored when they:

- preserve explicit owner authorization and workspace containment;
- keep accepted work durable across crashes and restarts;
- degrade by capability so optional integrations do not break the core;
- retain a simple local-Mac path;
- make personal or project preferences configurable without weakening
  invariants;
- include tests and documentation that make future maintenance safer.

Changes to defaults receive more scrutiny than opt-in additions. A proposal
that changes how every agent speaks, how every status is worded, or which
provider every group uses should first ask whether the choice belongs in the
layered configuration system.

Security boundaries, durable queue semantics, and route identity are not
ordinary style preferences. Altering them requires an explicit design
rationale, compatibility analysis, failure tests, and maintainer approval.

## Compatibility

The project currently supports one local macOS user and one Telegram owner.
Cloud, Linux, multi-user, and multi-host designs are welcome as proposals, but
must not make the local setup harder or silently broaden permissions.

Behavioral changes should preserve existing persisted state where practical.
Schema and mixed-version payload changes must account for old long-running
workers reading state written by fresh handler processes.

## Forks and variants

A GitHub fork is an independent copy for experimentation or a divergent product
direction. It does not affect users of the canonical repository. Reusable
preferences should be tried first in `.telegram-control.local.json`; shared
project conventions belong in `.telegram-control.json`; generally useful
improvements can return as pull requests.

The canonical project does not maintain an official fork for every preference.
If a fork develops a sustained community or distinct purpose, its maintainers
should name and govern it independently while observing the license.

## Becoming a maintainer

There is no automatic threshold. The lead maintainer may invite contributors
who demonstrate sustained, careful work; constructive review; respect for
security and compatibility; and reliable follow-through. Access should begin
with the least privilege needed and can be revisited.

This governance document can itself change through a pull request, but the
repository owner retains final authority while the project has a single lead
maintainer.
