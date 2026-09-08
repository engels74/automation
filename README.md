# Automation

Reusable validation workflows, dependency-update presets and checked PR merging.
Licensed under AGPL-3.0-only; see [LICENSE](LICENSE).

## CI workflows

The `bun`, `python`, `go`, `rust`, `zig` and `content` workflows provide pinned
runtime setup and run explicit repository-owned commands. Keep native builds,
browser tests, contracts, migrations and other specialized checks in the caller.
Use frozen dependencies and reject unintended tracked-file changes.

Run full CI on pull requests, configured default-branch pushes and explicit
dispatch. The final `ci / required` job must run with `if: always()` and depend
directly on every mandatory job. Call
`engels74/automation/actions/gate@v1.0.1` with the complete `needs` JSON and the
exact mandatory job IDs. Missing, failed, cancelled, pending, neutral and skipped
mandatory results fail the aggregate. Reporting may skip only when optional.

## Dependency updates

Start with the versioned base preset:

```json
{
  "extends": ["github>engels74/automation//default.json#v1.0.1"]
}
```

After the checked merge workflow and its policy are validated, append
`github>engels74/automation//automerge.json#v1.0.1`. Mixed projects can append
`github>engels74/automation//mixed.json#v1.0.1`. Preserve repository-specific
compatibility constraints and manual dependency policies.

The base groups non-major updates while keeping Actions, TypeScript, Biome and
prek groups separate. Both `0.x` and `v0.x` minor updates require Dependency
Dashboard approval. Majors remain manual except the explicit tooling allowlist,
which also requires dashboard approval. Shared policy updates remain manual.

Renovate uses `platformAutomerge: false`, `automergeType: pr-comment` and the
`/merge-when-green` handoff. Its authenticated request attests that the current
update satisfies Renovate's configured eligibility and dashboard-approval rules.
The merge helper independently checks repository opt-in, request freshness,
reviews, current branch state and complete CI. `manual-dependencies` and
`do-not-merge` labels block dependency handoffs. Never enable `ignoreTests`.

## Checked merging

Keep native GitHub automerge disabled. Branch protection and branch rulesets are
not required by this helper. Authorized users and tools can still merge directly;
the helper does not provide server-enforced protection.

Store `.github/merge-policy.json` on the default branch. Its `required_checks`
must list every mandatory **display name** returned by the Actions jobs API,
including `ci / required`. Declare optional reporting in `optional_checks`.
Unexpected contexts and duplicate names block merging. Use this repository's
policy as a schema example, replacing the job names with the caller's actual CI.

Call `engels74/automation/actions/merge@v1.0.1` from trusted default-branch
workflows for `issue_comment: created`, `pull_request_target: synchronize,
reopened, edited, ready_for_review`, and completion of the `ci` workflow.
Grant contents, pull requests, issues and Actions write, plus checks read.
Serialize the helper with a repository-wide concurrency group and
`cancel-in-progress: false`. Do not check out PR code in this privileged job.
The workflow in this repository is a complete example; consumers use the
versioned action instead of its local action path and need no checkout.

For a development PR, an authorized maintainer can request a merge with:

```text
/merge <full-40-character-head-SHA> <full-40-character-default-branch-SHA>
```

Post the request after full CI finishes. Edited requests, requests predating PR
changes or current CI, outstanding review requests, requested changes and missing
current-head approvals block merging. The head must contain the latest default
branch. CI must be the newest full run for that exact head, from the configured
workflow and GitHub Actions app, with matching run attempts and check-suite IDs.

The helper reads policy from the current default-branch commit and repeats its
evidence collection immediately before merging with the expected head SHA.
GitHub's merge endpoint does not atomically lock the base SHA; an independent
writer can still race the final check. Stale Renovate handoffs are removed after
PR changes so a later eligible Renovate run can issue a fresh request.

After merging, the helper explicitly dispatches full default-branch CI because
`GITHUB_TOKEN` merges suppress ordinary push-triggered workflows. CI must accept
`expected-default-sha` and pass it to the dispatch guard in validation and the
aggregate. A successful, current default-branch CI run can dispatch the optional
`deploy_workflows` listed in policy. Those workflows must accept and validate the
same exact-commit input before publishing. Failed dispatches are retried and fail
visibly, with the merged SHA and required CI target recorded for recovery.

## Biome repair

`biome-repair.yml@v1.0.1` separates computation from publication. Computation
uses the exact same-repository Renovate Biome head, disables install scripts,
installs the official formatter separately and checks migration/fix idempotency.
Formatter processes receive no API token.

Publication executes trusted action code, validates artifact identity and allowed
paths, rejects races and pushes without force. It cannot modify workflows,
manifests, lockfiles, hidden paths or add files. The new commit receives an
explicit full-CI dispatch. Callers must accept `pr-number` and
`expected-head-sha` and pass them to the dispatch guard in validation and the
aggregate. Repair does not replace CI or update approvals.

## Maintenance and releases

Use focused Conventional Commits with DCO sign-offs and a complete implementation
PR. Validate locally with:

```sh
bun install --frozen-lockfile --ignore-scripts
bun run prepare:native
bun run test
bun run validate:renovate
actionlint
```

The explicit native step prepares the locked RE2 dependency. Validation requires
RE2; it does not silently accept a JavaScript-regex fallback. Tests exercise
actual Renovate extraction and resolved policy, both pre-1.0 forms, manual
overrides, strict aggregation, real Biome migration, dispatch identity and merge
request/CI races.

Publish immutable full-version releases only after the implementation PR and
merged revision pass CI. Never move release tags. Callers reference versioned
workflows, actions and presets.
