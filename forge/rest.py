"""REST v3 API under /api/v3. Every reply is application/json and carries a
non-empty X-GitHub-Request-Id (the server adds that header); errors carry a
JSON body with a string message.
"""

import base64
import json

from . import serial
from .store import ForgeError

JSON = "application/json"


def _err(status, message):
    return status, JSON, serial.dumps({"message": message}), {}


def _ok(value, status=200, extra=None):
    return status, JSON, serial.dumps(value), extra or {}


def _parse_body(body):
    if not body:
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise ForgeError(400, "malformed JSON body")
    if not isinstance(value, dict):
        raise ForgeError(400, "JSON body must be an object")
    return value


def _int_param(query, name, default):
    try:
        return int(query.get(name, [str(default)])[0])
    except (ValueError, TypeError):
        return default


def _paginate(items, query, base_url):
    per_page = _int_param(query, "per_page", 30)
    per_page = max(1, min(per_page, 100))
    page = max(1, _int_param(query, "page", 1))
    total = len(items)
    last_page = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    page_items = items[start:start + per_page]
    links = []
    if page < last_page:
        links.append('<%s?page=%d&per_page=%d>; rel="next"' % (base_url, page + 1, per_page))
    if page > 1:
        links.append('<%s?page=%d&per_page=%d>; rel="prev"' % (base_url, page - 1, per_page))
    links.append('<%s?page=1&per_page=%d>; rel="first"' % (base_url, per_page))
    links.append('<%s?page=%d&per_page=%d>; rel="last"' % (base_url, last_page, per_page))
    return page_items, {"Link": ", ".join(links)} if links else {}


def _repo_or_404(store, owner, name):
    repo = store.repo_for(owner, name)
    if repo is None:
        raise ForgeError(404, "Not Found")
    return repo


def _number(segment):
    try:
        return int(segment)
    except ValueError:
        raise ForgeError(404, "Not Found")


def handle(store, method, segments, query, headers, body, host):
    """Route one REST call. segments are the URL-decoded path parts after
    /api/v3. Returns (status, content_type, body_bytes, extra_headers)."""
    try:
        return _route(store, method, segments, query, headers, body, host)
    except ForgeError as exc:
        return _err(exc.status, exc.message)


def _route(store, method, segments, query, headers, body, host):
    base = "http://%s/api/v3" % host
    if not segments:
        if method == "GET":
            return _ok({}, 200, {"X-OAuth-Scopes": "repo, read:org"})
        raise ForgeError(404, "Not Found")
    if segments == ["meta"]:
        if method == "GET":
            return _ok({"installed_version": "1.0.0"})
        raise ForgeError(404, "Not Found")
    if segments == ["user"]:
        if method == "GET":
            viewer = store.viewer
            return _ok({
                "login": viewer["login"], "id": viewer["id"],
                "name": viewer["name"], "email": viewer["email"],
                "type": "User",
                "html_url": "https://github.com/%s" % viewer["login"],
                "avatar_url": "https://avatars.githubusercontent.com/u/%d" % viewer["id"],
            })
        raise ForgeError(404, "Not Found")
    if segments == ["user", "repos"]:
        if method == "GET":
            repos = [serial.repo_json(store, r) for r in store.list_repos()]
            page, extra = _paginate(repos, query, base + "/user/repos")
            return _ok(page, 200, extra)
        if method == "POST":
            data = _parse_body(body)
            repo = store.create_repo(
                data.get("name"), data.get("private", False),
                data.get("description", ""))
            return _ok(serial.repo_json(store, repo), 201)
        raise ForgeError(404, "Not Found")
    if len(segments) >= 3 and segments[0] == "repos":
        owner, name = segments[1], segments[2]
        rest = segments[3:]
        return _repo_route(store, method, owner, name, rest, query, body, base)
    raise ForgeError(404, "Not Found")


def _repo_route(store, method, owner, name, rest, query, body, base):
    repo = _repo_or_404(store, owner, name)
    if not rest:
        if method == "GET":
            return _ok(serial.repo_json(store, repo))
        if method == "PATCH":
            data = _parse_body(body)
            if "description" in data:
                repo.description = data["description"] or ""
            if "default_branch" in data:
                branch = data["default_branch"]
                if repo.refs.get("refs/heads/" + branch) is None:
                    raise ForgeError(422, "invalid default branch")
                repo.default_branch = branch
            repo.updated_at = store.clock.now_iso()
            return _ok(serial.repo_json(store, repo))
        if method == "DELETE":
            store.delete_repo(repo)
            return 204, JSON, b"", {}
        raise ForgeError(404, "Not Found")
    head = rest[0]
    if head == "branches":
        return _branches(store, method, repo, rest[1:], query, base)
    if head == "git" and len(rest) >= 2 and rest[1] == "refs":
        return _git_refs(store, method, repo, rest[2:], body)
    if head == "commits":
        return _commits(store, method, repo, rest[1:], query, base)
    if head == "contents":
        return _contents(store, method, repo, rest[1:], query, body)
    if head == "labels":
        return _labels(store, method, repo, rest[1:], body)
    if head == "issues":
        return _issues(store, method, repo, rest[1:], query, body, base)
    if head == "pulls":
        return _pulls(store, method, repo, rest[1:], query, body, base)
    raise ForgeError(404, "Not Found")


def _branches(store, method, repo, rest, query, base):
    if method != "GET":
        raise ForgeError(404, "Not Found")
    if not rest:
        branches = [serial.branch_dict(name[11:], oid)
                    for name, oid in sorted(repo.refs.items())
                    if name.startswith("refs/heads/")]
        page, extra = _paginate(
            branches, query,
            "%s/repos/%s/%s/branches" % (base, repo.owner, repo.name))
        return _ok(page, 200, extra)
    branch = "/".join(rest)
    oid = repo.refs.get("refs/heads/" + branch)
    if oid is None:
        raise ForgeError(404, "Not Found")
    return _ok(serial.branch_dict(branch, oid))


def _git_refs(store, method, repo, rest, body):
    if method == "GET" and not rest:
        refs = [serial.ref_dict(name, oid) for name, oid in sorted(repo.refs.items())]
        return _ok(refs)
    if method == "GET":
        refname = "/".join(rest)
        if not refname.startswith("refs/"):
            refname = "refs/" + refname
        oid = repo.refs.get(refname)
        if oid is None:
            raise ForgeError(404, "Not Found")
        return _ok(serial.ref_dict(refname, oid))
    if method == "POST" and not rest:
        data = _parse_body(body)
        refname = data.get("ref", "")
        sha = data.get("sha", "")
        if not isinstance(refname, str) or not refname.startswith("refs/"):
            raise ForgeError(422, "invalid ref")
        if store.get_commit(repo, sha) is None:
            raise ForgeError(422, "invalid sha")
        if refname in repo.refs:
            raise ForgeError(422, "Reference already exists")
        store.set_ref(repo, refname, sha)
        return _ok(serial.ref_dict(refname, sha), 201)
    raise ForgeError(404, "Not Found")


def _commits(store, method, repo, rest, query, base):
    if method != "GET":
        raise ForgeError(404, "Not Found")
    if not rest:
        sha = query.get("sha", [None])[0]
        tip = store.resolve_ref(repo, sha)
        if tip is None:
            raise ForgeError(404, "Not Found")
        commits = []
        for oid in store.ancestors(repo, tip):
            entry = serial.commit_dict(store, repo, oid)
            if entry is not None:
                commits.append(entry)
        page, extra = _paginate(
            commits, query,
            "%s/repos/%s/%s/commits" % (base, repo.owner, repo.name))
        return _ok(page, 200, extra)
    sha = "/".join(rest)
    oid = store.resolve_ref(repo, sha)
    if oid is None:
        raise ForgeError(404, "Not Found")
    entry = serial.commit_dict(store, repo, oid)
    if entry is None:
        raise ForgeError(404, "Not Found")
    return _ok(entry)


def _contents(store, method, repo, rest, query, body):
    path = "/".join(rest)
    if method == "GET":
        ref = query.get("ref", [None])[0]
        found = store.get_content(repo, path, ref)
        if found is None:
            raise ForgeError(404, "Not Found")
        kind, info = found
        if kind == "file":
            entry = repo.objects.get(info["sha"])
            content_b64 = base64.b64encode(entry[1]).decode() if entry else ""
            return _ok(serial.content_file_dict(info, content_b64))
        return _ok(info)
    if method == "PUT":
        data = _parse_body(body)
        status, info, _commit = store.put_content(
            repo, path, data.get("message"), data.get("content"),
            branch=data.get("branch"), sha=data.get("sha"))
        entry = repo.objects.get(info["sha"])
        content_b64 = base64.b64encode(entry[1]).decode() if entry else ""
        return _ok({
            "content": serial.content_file_dict(info, content_b64),
            "commit": {"sha": _commit, "message": data.get("message")},
        }, status)
    if method == "DELETE":
        data = _parse_body(body)
        commit_oid = store.delete_content(
            repo, path, data.get("message"), data.get("sha"),
            branch=data.get("branch"))
        return _ok({"commit": {"sha": commit_oid, "message": data.get("message")}})
    raise ForgeError(404, "Not Found")


def _labels(store, method, repo, rest, body):
    if rest:
        raise ForgeError(404, "Not Found")
    if method == "GET":
        return _ok([serial.label_json(label) for label in repo.labels.values()])
    if method == "POST":
        data = _parse_body(body)
        label = store.create_label(repo, data.get("name"), data.get("color"),
                                   data.get("description", ""))
        return _ok(serial.label_json(label), 201)
    raise ForgeError(404, "Not Found")


def _issues(store, method, repo, rest, query, body, base):
    if not rest:
        if method == "GET":
            state = query.get("state", ["open"])[0]
            direction = query.get("direction", ["desc"])[0]
            issues = store.list_issues(repo, state, direction)
            url = "%s/repos/%s/%s/issues" % (base, repo.owner, repo.name)
            page, extra = _paginate_with_bytes(issues, query, url)
            return 200, JSON, page, extra
        if method == "POST":
            data = _parse_body(body)
            labels = data.get("labels")
            if labels is not None:
                labels = [item["name"] if isinstance(item, dict) else item
                          for item in labels]
            issue = store.create_issue(repo, data.get("title"), data.get("body"),
                                       labels)
            return 201, JSON, issue["_json"], {}
        raise ForgeError(404, "Not Found")
    number = _number(rest[0])
    if len(rest) == 1:
        issue = store.get_issue(repo, number)
        if issue is None:
            raise ForgeError(404, "Not Found")
        if method == "GET":
            return 200, JSON, issue["_json"], {}
        if method == "PATCH":
            data = _parse_body(body)
            labels = data.get("labels")
            if labels is not None:
                labels = [item["name"] if isinstance(item, dict) else item
                          for item in labels]
            store.patch_issue(repo, issue, title=data.get("title"),
                              body=data.get("body"), state=data.get("state"),
                              labels=labels)
            return 200, JSON, issue["_json"], {}
        raise ForgeError(404, "Not Found")
    if len(rest) == 2 and rest[1] == "comments":
        issue = store.get_issue(repo, number)
        pull = None
        if issue is None:
            # In GitHub, a pull request shares its number's timeline with
            # the issue of the same number; comments on a PR read back here.
            pull = store.get_pull(repo, number)
            if pull is None:
                raise ForgeError(404, "Not Found")
        if method == "GET":
            comments = issue["comments"] if issue is not None else pull.get("comments", [])
            comment_dicts = [serial.comment_dict(store, repo, issue, c)
                             if issue is not None else
                             {"id": c["id"], "body": c["body"],
                              "user": serial.user_json(c["user"], 1000),
                              "created_at": c["created_at"],
                              "updated_at": c["updated_at"],
                              "html_url": "https://github.com/%s/pull/%d#issuecomment-%d" % (
                                  repo.full_name, number, c["id"])}
                             for c in comments]
            url = "%s/repos/%s/%s/issues/%d/comments" % (
                base, repo.owner, repo.name, number)
            page, extra = _paginate(comment_dicts, query, url)
            return _ok(page, 200, extra)
        if method == "POST":
            if issue is None:
                raise ForgeError(404, "Not Found")
            data = _parse_body(body)
            comment = store.add_comment(repo, issue, data.get("body"))
            return _ok(serial.comment_dict(store, repo, issue, comment), 201)
        raise ForgeError(404, "Not Found")
    raise ForgeError(404, "Not Found")


def _paginate_with_bytes(issues, query, url):
    """Paginate a list of issue dicts, joining their cached JSON bytes."""
    per_page = _int_param(query, "per_page", 30)
    per_page = max(1, min(per_page, 100))
    page = max(1, _int_param(query, "page", 1))
    total = len(issues)
    last_page = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    page_items = issues[start:start + per_page]
    body = b"[" + b",".join(item["_json"] for item in page_items) + b"]"
    links = []
    if page < last_page:
        links.append('<%s?page=%d&per_page=%d>; rel="next"' % (url, page + 1, per_page))
    if page > 1:
        links.append('<%s?page=%d&per_page=%d>; rel="prev"' % (url, page - 1, per_page))
    links.append('<%s?page=1&per_page=%d>; rel="first"' % (url, per_page))
    links.append('<%s?page=%d&per_page=%d>; rel="last"' % (url, last_page, per_page))
    return body, {"Link": ", ".join(links)}


def _pulls(store, method, repo, rest, query, body, base):
    if not rest:
        if method == "GET":
            state = query.get("state", ["open"])[0]
            direction = query.get("direction", ["desc"])[0]
            pulls = [serial.pull_dict(store, repo, p)
                     for p in store.list_pulls(repo, state, direction)]
            url = "%s/repos/%s/%s/pulls" % (base, repo.owner, repo.name)
            page, extra = _paginate(pulls, query, url)
            return _ok(page, 200, extra)
        if method == "POST":
            data = _parse_body(body)
            pull = store.create_pull(repo, data.get("title"), data.get("head"),
                                     data.get("base"), data.get("body"))
            return _ok(serial.pull_dict(store, repo, pull), 201)
        raise ForgeError(404, "Not Found")
    number = _number(rest[0])
    if len(rest) == 1:
        pull = store.get_pull(repo, number)
        if pull is None:
            raise ForgeError(404, "Not Found")
        if method == "GET":
            return _ok(serial.pull_dict(store, repo, pull))
        if method == "PATCH":
            data = _parse_body(body)
            if "title" in data:
                pull["title"] = data["title"]
            if "body" in data:
                pull["body"] = data["body"]
            if data.get("state") == "closed" and pull["state"] == "open":
                pull["state"] = "closed"
                pull["closed_at"] = store.clock.now_iso()
            pull["updated_at"] = store.clock.now_iso()
            return _ok(serial.pull_dict(store, repo, pull))
        raise ForgeError(404, "Not Found")
    if len(rest) == 2 and rest[1] == "files":
        if method != "GET":
            raise ForgeError(404, "Not Found")
        pull = store.get_pull(repo, number)
        if pull is None:
            raise ForgeError(404, "Not Found")
        files = store.pr_files(repo, pull)
        url = "%s/repos/%s/%s/pulls/%d/files" % (base, repo.owner, repo.name, number)
        page, extra = _paginate(files, query, url)
        return _ok(page, 200, extra)
    if len(rest) == 2 and rest[1] == "commits":
        if method != "GET":
            raise ForgeError(404, "Not Found")
        pull = store.get_pull(repo, number)
        if pull is None:
            raise ForgeError(404, "Not Found")
        head_oid = store.pull_head_oid(repo, pull)
        base_oid = store.pull_base_oid(repo, pull)
        merge_base = store.merge_base(repo, head_oid, base_oid)
        commits = []
        if head_oid is not None and merge_base is not None:
            seen = set(store.ancestors(repo, merge_base))
            for oid in store.ancestors(repo, head_oid):
                if oid in seen:
                    continue
                entry = serial.commit_dict(store, repo, oid)
                if entry is not None:
                    commits.append(entry)
        url = "%s/repos/%s/%s/pulls/%d/commits" % (base, repo.owner, repo.name, number)
        page, extra = _paginate(commits, query, url)
        return _ok(page, 200, extra)
    if len(rest) == 2 and rest[1] == "merge":
        if method != "PUT":
            raise ForgeError(404, "Not Found")
        pull = store.get_pull(repo, number)
        if pull is None:
            raise ForgeError(404, "Not Found")
        data = _parse_body(body)
        method_name = data.get("merge_method", "squash")
        sha, message = store.merge_pull(repo, pull, method_name)
        return _ok({"merged": True, "sha": sha, "message": message})
    raise ForgeError(404, "Not Found")
