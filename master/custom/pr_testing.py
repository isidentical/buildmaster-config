# Functions to enable buildbot to test pull requests and report back
import logging

from dateutil.parser import parse as dateparse

from twisted.internet import defer
from twisted.python import log

from buildbot.util import httpclientservice
from buildbot.www.hooks.github import GitHubEventHandler

TESTING_PREFIX = "!buildbot"
TESTING_LABEL = ":hammer: test-with-buildbots"

GITHUB_PROPERTIES_WHITELIST = ["*.labels"]


def should_pr_be_tested(change):
    if TESTING_LABEL in {
        label["name"]
        for label in change.properties.asDict().get("github.labels", [set()])[0]
    }:
        print(f"Label detected in PR {change.branch} (commit {change.revision})")
        return True
    print(f"Label not found in PR {change.branch} (commit {change.revision})")
    return False


class CustomGitHubEventHandler(GitHubEventHandler):

    @defer.inlineCallbacks
    def handle_issue_comment(self, payload, event):
        comment = payload["comment"]["body"].split()
        log.msg(f"Handled comment: {comment}")

        if payload.get("action") != "created":
            return ([], "git")

        if len(comment) < 2 and comment[0] != TESTING_PREFIX:
            return ([], "git")

        pull_request = payload["issue"].get("pull_request")
        if pull_request is None:
            return ([], "git")
        else:
            pull_request = pull_request["url"].replace(self.github_api_endpoint, "")

        headers = {
            'User-Agent': 'Buildbot'
        }
        if self._token:
            headers['Authorization'] = 'token ' + self._token

        http = yield httpclientservice.HTTPClientService.getService(
            self.master, self.github_api_endpoint, headers=headers,
            debug=self.debug, verify=self.verify)

        pr_payload = {}
        pr_payload["action"] = "commented"
        pr_payload["sender"] = payload["sender"].copy()
        pr_payload["number"] = payload["issue"]["number"]
        pr_payload["repository"] = payload["repository"].copy()

        pull_request_object = yield http.get(pull_request)
        pr_payload["pull_request"] = yield pull_request_object.json()

        changes, vcs = yield self.handle_pull_request(pr_payload, event)
        changes[0]["properties"].update({"is_comment": True})
        changes[0]["properties"].update({"action": comment[1]}) # ex: @mycustombot [run] x y
        changes[0]["properties"].update({"bots": comment[2:]}) # ex: @mycustombot run [x y]
        return changes, vcs

    @defer.inlineCallbacks
    def handle_pull_request(self, payload, event):
        changes = []
        number = payload["number"]
        refname = "refs/pull/{}/{}".format(number, self.pullrequest_ref)
        basename = payload["pull_request"]["base"]["ref"]
        commits = payload["pull_request"]["commits"]
        title = payload["pull_request"]["title"]
        comments = payload["pull_request"]["body"]
        repo_full_name = payload["repository"]["full_name"]
        head_sha = payload["pull_request"]["head"]["sha"]

        log.msg("Processing GitHub PR #{}".format(number), logLevel=logging.DEBUG)

        head_msg = yield self._get_commit_msg(repo_full_name, head_sha)
        if self._has_skip(head_msg):
            log.msg(
                "GitHub PR #{}, Ignoring: "
                "head commit message contains skip pattern".format(number)
            )
            return ([], "git")

        action = payload.get("action")
        if action not in ("opened", "reopened", "synchronize", "labeled", "commented"):
            log.msg("GitHub PR #{} {}, ignoring".format(number, action))
            return (changes, "git")

        if action == "labeled" and payload.get("label")["name"] != TESTING_LABEL:
            log.msg("Invalid label in PR #{}, ignoring".format(number))
            return (changes, "git")
        elif TESTING_LABEL not in {
            label["name"] for label in payload.get("pull_request")["labels"]
        }:
            log.msg("Invalid label in PR #{}, ignoring".format(number))
            return (changes, "git")

        properties = self.extractProperties(payload["pull_request"])
        properties.update({"event": event})
        properties.update({"basename": basename})
        change = {
            "revision": payload["pull_request"]["head"]["sha"],
            "when_timestamp": dateparse(payload["pull_request"]["created_at"]),
            "branch": refname,
            "revlink": payload["pull_request"]["_links"]["html"]["href"],
            "repository": payload["repository"]["html_url"],
            "project": payload["pull_request"]["base"]["repo"]["full_name"],
            "category": "pull",
            "author": payload["sender"]["login"],
            "comments": "GitHub Pull Request #{0} ({1} commit{2})\n{3}\n{4}".format(
                number, commits, "s" if commits != 1 else "", title, comments
            ),
            "properties": properties,
        }

        if callable(self._codebase):
            change["codebase"] = self._codebase(payload)
        elif self._codebase is not None:
            change["codebase"] = self._codebase

        changes.append(change)

        log.msg("Received {} changes from GitHub PR #{}".format(len(changes), number))
        return (changes, "git")
