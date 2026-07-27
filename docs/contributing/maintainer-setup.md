# GitHub maintainer setup

Files in the repository can guide collaboration, but only a repository owner
can change visibility, rules, security features, and collaborator permissions.
Use this checklist after reviewing and merging the readiness pull request.

## 1. Confirm the license

This repository proposes the Apache License 2.0: permissive reuse with an
explicit patent grant and preservation requirements. Confirm that this is the
license you want before making the repository public. Changing a license after
accepting outside contributions is much harder.

GitHub explains why an explicit license is necessary in
[Licensing a repository](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).

## 2. Audit before changing visibility

Review the entire Git history for secrets and personal information, including
deleted files. At minimum, look for:

- Telegram bot tokens and invitation links;
- provider credentials and session exports;
- `.env` files, Keychain exports, and config backups;
- SQLite databases, logs, attachments, and audio;
- personal usernames, home paths, chat IDs, and client/project names.

Also review GitHub Actions, deploy keys, webhooks, collaborators, branch names,
releases, issue content, and repository description. Rotate a secret if it was
ever committed; deleting the current file is not enough.

Then change **Settings → General → Danger Zone → Change repository
visibility**. Public visibility is not needed for the software to run. It only
allows anyone to read and fork the source.

## 3. Protect `main` with a ruleset

Create a branch ruleset targeting the default branch and make it active. GitHub
documents the available controls in
[Available rules for rulesets](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-rulesets/available-rules-for-rulesets).

Recommended initial rules:

- restrict deletions;
- block force pushes;
- require a pull request before merging;
- require all review conversations to be resolved;
- require the two CI checks:
  `Python 3.9 on macOS` and `Python 3.13 on macOS`;
- require branches to be up to date before merging;
- require linear history.

For a sole maintainer, set required approving reviews to **0** initially. The
pull request itself, CI, resolved conversations, and protected history are still
mandatory, while the owner can merge their own reviewed change. GitHub does not
allow an author to approve their own pull request, so requiring one approval
would need a second trusted reviewer.

Once a second maintainer is consistently available, require one approval,
enable dismissal of stale approvals, and require CODEOWNER review for the
critical files. Keep bypass permissions empty or limited to an explicit
emergency role; otherwise an administrator can silently avoid the PR rule.

GitHub's branch-protection guide describes the equivalent protection settings:
[Managing a branch protection rule](https://docs.github.com/en/repositories/configuring-branches-and-merges-in-your-repository/managing-protected-branches/managing-a-branch-protection-rule).

## 4. Limit repository access

Keep direct write or maintain access small. Outside contributors normally fork
the public repository, push branches to their own fork, and open pull requests;
they cannot change the canonical repository merely because it is public.
GitHub documents the permission model in
[About permissions and visibility of forks](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/about-permissions-and-visibility-of-forks).

Grant collaborators the least role they need. Add maintainers gradually, and
periodically review dormant access, deploy keys, installed apps, and webhooks.
CODEOWNERS requests review but does not itself prevent a merge; the ruleset
makes that review enforceable.

## 5. Configure Actions and security

- Restrict Actions to GitHub-authored actions or an explicit allowlist.
- Require actions to be pinned to a full commit SHA. The included workflow is
  already pinned.
- Keep the default workflow token read-only unless a job needs a specific
  additional permission.
- Enable Dependabot alerts, dependency review, secret scanning, and push
  protection where the repository/account plan offers them.
- Enable private vulnerability reporting so `SECURITY.md` has a working
  confidential path.

## 6. Avoid becoming the bottleneck

At first, the owner will make canonical merge decisions, but automation should
handle mechanical checks. As contribution volume grows:

1. label issues by area and impact;
2. ask contributors to keep pull requests small;
3. invite repeat contributors to triage and review before giving write access;
4. delegate ownership by subsystem in CODEOWNERS;
5. require a second maintainer for security or invariant changes;
6. publish releases and a changelog when downstream users need stability.

Forks are for independent experimentation or long-term divergence. Normal
customization should use the layered install/workspace files, and generally
useful work should return through pull requests.
