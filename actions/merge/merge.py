"""Merge only after a fresh request, trusted policy and complete exact-head CI."""
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import re
import time
import urllib.error
import urllib.parse
import urllib.request


class Blocked(ValueError):
    pass


def require(condition, message):
    if not condition:
        raise Blocked(message)


def sha(value):
    require(isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value), "invalid commit identity")
    return value


def timestamp(value):
    require(isinstance(value, str), "missing evidence timestamp")
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class API:
    def __init__(self, repository, token):
        require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository), "invalid repository")
        self.root = "https://api.github.com/repos/" + repository
        self.token = token

    def request(self, path, method="GET", data=None):
        require(path == "" or (path.startswith("/") and not path.startswith("//") and "/../" not in path), "invalid API path")
        request = urllib.request.Request(self.root + path, method=method,
            data=json.dumps(data).encode() if data is not None else None,
            headers={"Authorization": "Bearer " + self.token, "Accept": "application/vnd.github+json",
                     "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as error:
            raise Blocked(f"GitHub API returned HTTP {error.code}") from None

    def pages(self, path, field=None):
        result = []
        for page in range(1, 101):
            separator = "&" if "?" in path else "?"
            data = self.request(f"{path}{separator}per_page=100&page={page}")
            values = data[field] if field else data
            require(isinstance(values, list), "invalid paginated evidence")
            result.extend(values)
            if len(values) < 100:
                return result
        raise Blocked("evidence pagination limit reached")


def policy(api, base):
    raw = api.request("/contents/.github/merge-policy.json?ref=" + sha(base))
    require(raw.get("encoding") == "base64", "unsupported policy encoding")
    config = json.loads(base64.b64decode(raw["content"]))
    require(config.get("schema") == 1 and config.get("enabled") is True, "checked merging is disabled")
    required = config.get("required_checks")
    optional = config.get("optional_checks", [])
    require(isinstance(required, list) and required and len(set(required)) == len(required), "empty or duplicate mandatory checks")
    require(isinstance(optional, list) and len(set(optional)) == len(optional) and not set(required) & set(optional), "invalid optional checks")
    require("ci / required" in required and all(isinstance(name, str) and name for name in required + optional), "invalid mandatory aggregate")
    require(config.get("check_app_id") == 15368, "CI must come from GitHub Actions")
    require(config.get("renovate") == {"login": "renovate[bot]", "id": 29139614}, "unexpected Renovate identity")
    require(isinstance(config.get("minimum_approvals", 0), int) and config.get("minimum_approvals", 0) >= 0, "invalid approval policy")
    for workflow in [config.get("ci_workflow"), *config.get("deploy_workflows", [])]:
        require(isinstance(workflow, str) and re.fullmatch(r"[A-Za-z0-9_.-]+\.ya?ml", workflow), "invalid workflow filename")
    return config


def is_renovate(user, config):
    return user.get("login") == config["renovate"]["login"] and user.get("id") == config["renovate"]["id"] and user.get("type") == "Bot"


def approvals(pr, reviews, minimum):
    require(not pr.get("requested_reviewers") and not pr.get("requested_teams"), "review requests remain outstanding")
    latest = {}
    for review in sorted(reviews, key=lambda item: item["id"]):
        state = review.get("state")
        if state in {"APPROVED", "CHANGES_REQUESTED", "DISMISSED"}:
            latest[review["user"]["id"]] = review
        require(state != "PENDING", "a review remains pending")
    require(not any(r["state"] == "CHANGES_REQUESTED" for r in latest.values()), "changes are requested")
    count = sum(r["state"] == "APPROVED" and r.get("commit_id") == pr["head"]["sha"]
                and r["user"]["id"] != pr["user"]["id"] for r in latest.values())
    require(count >= minimum, "current-head approvals are missing")


def validate_ci(config, runs, jobs, checks, head, branch, workflow_id, number=None):
    require(runs, "full CI is missing")
    newest = max(runs, key=lambda r: (timestamp(r.get("run_started_at") or r["created_at"]), r["id"], r["run_attempt"]))
    require(newest["workflow_id"] == workflow_id and newest["head_sha"] == head and newest["head_branch"] == branch, "CI workflow, branch or commit differs")
    require(newest["event"] in ({"pull_request", "workflow_dispatch"} if number else {"push", "workflow_dispatch"}), "unexpected CI event source")
    if number and newest["event"] == "pull_request":
        require(any(p["number"] == number for p in newest.get("pull_requests", [])), "CI belongs to a different PR")
    require(newest["status"] == "completed" and newest["conclusion"] == "success", "newest full CI did not succeed")
    by_name = {}
    for job in jobs:
        require(job["run_id"] == newest["id"] and job["run_attempt"] == newest["run_attempt"], "job belongs to another CI run or attempt")
        require(job["name"] not in by_name, "ambiguous duplicate CI context")
        by_name[job["name"]] = job
    required, optional = set(config["required_checks"]), set(config.get("optional_checks", []))
    require(set(by_name) == required | optional, "missing or undeclared CI contexts")
    by_id = {check["id"]: check for check in checks}
    for name, job in by_name.items():
        require(job["status"] == "completed", "CI job is incomplete")
        allowed = {"success", "skipped"} if name in optional else {"success"}
        require(job["conclusion"] in allowed, "CI context did not succeed")
        match = re.fullmatch(r"https://api\.github\.com/repos/[^/]+/[^/]+/check-runs/(\d+)", job["check_run_url"])
        require(match is not None, "invalid check-run identity")
        check = by_id.get(int(match.group(1)), {})
        require(check.get("name") == name and check.get("head_sha") == head and check.get("app", {}).get("id") == config["check_app_id"], "wrong CI check source")
        require(check.get("check_suite", {}).get("id") == newest["check_suite_id"], "check belongs to another suite")
        require(check.get("status") == "completed" and check.get("conclusion") == job["conclusion"], "check-run conclusion differs")
    return newest


def ci_evidence(api, config, head, branch, number=None):
    workflow = api.request("/actions/workflows/" + config["ci_workflow"])
    require(workflow["path"] == ".github/workflows/" + config["ci_workflow"] and workflow["state"] == "active", "unexpected or inactive CI workflow")
    runs = api.pages(f"/actions/workflows/{workflow['id']}/runs?head_sha={sha(head)}", "workflow_runs")
    require(runs, "full CI is missing")
    newest = max(runs, key=lambda r: (timestamp(r.get("run_started_at") or r["created_at"]), r["id"], r["run_attempt"]))
    jobs = api.pages(f"/actions/runs/{newest['id']}/attempts/{newest['run_attempt']}/jobs", "jobs")
    checks = api.pages(f"/check-suites/{newest['check_suite_id']}/check-runs?filter=all", "check_runs")
    return validate_ci(config, runs, jobs, checks, head, branch, workflow["id"], number)


def validate_request(comment, pr, config, head, base, run, permission):
    require(comment["created_at"] == comment["updated_at"], "edited merge requests are not accepted")
    require(timestamp(comment["created_at"]) >= timestamp(pr["updated_at"]), "merge request predates PR changes")
    require(timestamp(comment["created_at"]) >= timestamp(run["updated_at"]), "merge request predates current CI evidence")
    if is_renovate(comment["user"], config):
        require(is_renovate(pr["user"], config), "Renovate request is not on a Renovate PR")
        require(comment["body"] == config["renovate_comment"], "unexpected Renovate handoff")
        require(pr["head"]["ref"].startswith("renovate/"), "unexpected Renovate branch")
        require(not {"manual-dependencies", "do-not-merge"} & {label["name"] for label in pr.get("labels", [])}, "manual dependency policy applies")
        return "renovate"
    require(permission in {"admin", "maintain", "write"}, "requester lacks merge permission")
    require(comment["body"] == f"/merge {head} {base}", "manual request must name the exact head and base commits")
    return "maintainer"


def snapshot(api, repository, default_branch, number, comment_id):
    repo = api.request("")
    require(repo["full_name"] == repository and repo["default_branch"] == default_branch and not repo["archived"], "repository identity or configured default branch changed")
    require(repo["allow_auto_merge"] is False, "native automerge must remain disabled")
    ref = api.request("/git/ref/heads/" + urllib.parse.quote(default_branch, safe=""))
    base = sha(ref["object"]["sha"])
    config = policy(api, base)
    pr = api.request(f"/pulls/{number}")
    require(pr["state"] == "open" and not pr["draft"] and not pr["merged"], "PR is not ready")
    require(pr["base"]["ref"] == default_branch and pr["base"]["sha"] == base, "PR base differs from current default branch")
    require(pr["head"].get("repo", {}).get("full_name") == repository and pr["base"]["repo"]["full_name"] == repository, "cross-repository PR is not eligible")
    head = sha(pr["head"]["sha"])
    compare = api.request(f"/compare/{base}...{head}")
    require(compare["behind_by"] == 0 and compare["merge_base_commit"]["sha"] == base and compare["ahead_by"] > 0, "head does not include the latest default branch")
    require(pr.get("mergeable") is True, "PR mergeability is not confirmed")
    approvals(pr, api.pages(f"/pulls/{number}/reviews"), config.get("minimum_approvals", 0))
    run = ci_evidence(api, config, head, pr["head"]["ref"], number)
    comment = api.request(f"/issues/comments/{comment_id}")
    require(comment["issue_url"] == api.root + f"/issues/{number}", "request belongs to a different PR")
    permission = None
    if not is_renovate(comment["user"], config):
        permission = api.request("/collaborators/" + urllib.parse.quote(comment["user"]["login"], safe="") + "/permission")["permission"]
    requester = validate_request(comment, pr, config, head, base, run, permission)
    if requester == "renovate":
        raw = api.request("/contents/renovate.json?ref=" + base)
        renovate = json.loads(base64.b64decode(raw["content"]))
        owner = repository.split("/")[0]
        require(any(re.fullmatch(r"github>" + re.escape(owner) + r"/automation//automerge\.json#v\d+\.\d+\.\d+", preset) for preset in renovate.get("extends", [])), "trusted default branch has not opted into checked dependency merging")
        require(renovate.get("automerge") is not False and renovate.get("platformAutomerge") is not True and renovate.get("ignoreTests") is not True, "repository dependency policy forbids this handoff")
    return {"head": head, "base": base, "run_id": run["id"], "run_attempt": run["run_attempt"],
            "request_id": comment["id"], "requester_id": comment["user"]["id"], "request_created": comment["created_at"],
            "pr_updated": pr["updated_at"], "policy": config}


def dispatch(api, workflow, branch, expected):
    for attempt in range(3):
        try:
            current = api.request("/git/ref/heads/" + urllib.parse.quote(branch, safe=""))["object"]["sha"]
            require(current == expected, "default branch advanced before follow-up dispatch")
            api.request("/actions/workflows/" + workflow + "/dispatches", "POST",
                        {"ref": branch, "inputs": {"expected-default-sha": expected}})
            return
        except (Blocked, OSError):
            if attempt == 2:
                raise
            time.sleep(attempt + 1)


def merge(api, repository, default_branch, number, comment_id):
    first = snapshot(api, repository, default_branch, number, comment_id)
    second = snapshot(api, repository, default_branch, number, comment_id)
    require(first == second, "head, base, policy, request or CI changed during final checks")
    result = api.request(f"/pulls/{number}/merge", "PUT", {"sha": first["head"], "merge_method": "squash"})
    require(result.get("merged") is True, "GitHub did not merge the expected commit")
    merged = sha(result["sha"])
    # GITHUB_TOKEN merges suppress push-triggered workflows. Dispatch explicitly.
    print(f"Merged PR #{number} as {merged}; required follow-up: {first['policy']['ci_workflow']} on {default_branch} at {merged}.", flush=True)
    dispatch(api, first["policy"]["ci_workflow"], default_branch, merged)
    print(f"Merged PR #{number}; dispatched default-branch CI for {merged}.")


def invalidate(api, default_branch, number):
    base = api.request("/git/ref/heads/" + urllib.parse.quote(default_branch, safe=""))["object"]["sha"]
    config = policy(api, base)
    pr = api.request(f"/pulls/{number}")
    if not is_renovate(pr["user"], config):
        return
    for comment in api.pages(f"/issues/{number}/comments"):
        if is_renovate(comment["user"], config) and comment["body"] == config["renovate_comment"] and timestamp(comment["created_at"]) < timestamp(pr["updated_at"]):
            api.request(f"/issues/comments/{comment['id']}", "DELETE")
    # Renovate's next eligible run can issue a fresh supported pr-comment handoff.


def complete(api, default_branch, event_run):
    base = api.request("/git/ref/heads/" + urllib.parse.quote(default_branch, safe=""))["object"]["sha"]
    if event_run["head_sha"] != base or event_run["head_branch"] != default_branch:
        return
    config = policy(api, base)
    run = ci_evidence(api, config, base, default_branch)
    require(run["id"] == event_run["id"] and run["run_attempt"] == event_run["run_attempt"], "stale default-branch CI event")
    for workflow in config.get("deploy_workflows", []):
        dispatch(api, workflow, default_branch, base)


def main():
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    repository = os.environ["GITHUB_REPOSITORY"]
    require(repository.split("/")[0] == os.environ["AUTOMATION_OWNER"], "automation account mismatch")
    require(event["repository"]["full_name"] == repository, "event repository mismatch")
    default_branch = event["repository"]["default_branch"]
    api = API(repository, os.environ["GH_TOKEN"])
    live = api.request("")
    require(live["id"] == event["repository"]["id"] and live["full_name"] == repository and live["default_branch"] == default_branch, "event repository identity or default branch is stale")
    current = api.request("/git/ref/heads/" + urllib.parse.quote(default_branch, safe=""))["object"]["sha"]
    require(os.environ["GITHUB_SHA"] == current, "trusted default-branch workflow is stale")
    name = os.environ["GITHUB_EVENT_NAME"]
    if name == "issue_comment" and event["action"] == "created" and event["issue"].get("pull_request"):
        body = event["comment"]["body"]
        config = policy(api, current)
        if body == config["renovate_comment"] or body.startswith("/merge "):
            require(event["sender"]["id"] == event["comment"]["user"]["id"], "request event actor mismatch")
            merge(api, repository, default_branch, event["issue"]["number"], event["comment"]["id"])
    elif name == "pull_request_target":
        invalidate(api, default_branch, event["pull_request"]["number"])
    elif name == "workflow_run" and event["action"] == "completed":
        complete(api, default_branch, event["workflow_run"])


if __name__ == "__main__":
    try:
        main()
    except Blocked as error:
        # Blocked messages are fixed descriptions constructed by this module.
        raise SystemExit("Blocked: " + str(error)) from None
    except (KeyError, TypeError, ValueError, OSError):
        # Do not echo untrusted payloads, exception response bodies or credentials.
        raise SystemExit("Merge or follow-up blocked: required evidence is missing, stale or invalid.") from None
