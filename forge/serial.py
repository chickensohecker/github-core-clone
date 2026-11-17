"""JSON shapes for the REST v3, GraphQL and MCP surfaces. Serializers take
plain dicts; nothing here touches the HTTP layer.
"""

import base64
import json


def dumps(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def user_json(login, user_id=None, name=None):
    out = {"login": login, "type": "User"}
    if user_id is not None:
        out["id"] = user_id
    if name:
        out["name"] = name
    return out


def repo_json(store, repo):
    return {
        "id": repo.id,
        "name": repo.name,
        "full_name": repo.full_name,
        "owner": user_json(repo.owner, 1000, "ada iversen"),
        "private": repo.private,
        "description": repo.description,
        "default_branch": repo.default_branch,
        "created_at": repo.created_at,
        "updated_at": repo.updated_at,
        "pushed_at": repo.pushed_at,
        "html_url": "https://github.com/%s" % repo.full_name,
        "clone_url": "https://github.com/%s.git" % repo.full_name,
        "git_url": "git://github.com/%s.git" % repo.full_name,
        "ssh_url": "git@github.com:%s.git" % repo.full_name,
        "has_issues": True,
        "has_projects": True,
        "has_wiki": False,
        "fork": False,
        "archived": False,
        "disabled": False,
        "open_issues_count": sum(1 for i in repo.issues.values() if i["state"] == "open"),
        "license": None,
        "topics": [],
        "visibility": "private" if repo.private else "public",
    }


def label_json(label):
    return {
        "id": label["id"], "name": label["name"], "color": label["color"],
        "description": label.get("description", ""), "default": label.get("default", False),
    }


def issue_dict(store, repo, issue):
    return {
        "id": issue["id"],
        "number": issue["number"],
        "title": issue["title"],
        "body": issue["body"],
        "state": issue["state"],
        "labels": [label_json(label) for label in issue["labels"]],
        "user": user_json(issue["user"], 1000),
        "created_at": issue["created_at"],
        "updated_at": issue["updated_at"],
        "closed_at": issue["closed_at"],
        "comments": len(issue["comments"]),
        "html_url": "https://github.com/%s/issues/%d" % (repo.full_name, issue["number"]),
    }


def issue_json_bytes(store, repo, issue):
    return dumps(issue_dict(store, repo, issue))


def comment_dict(store, repo, issue, comment):
    return {
        "id": comment["id"],
        "body": comment["body"],
        "user": user_json(comment["user"], 1000),
        "created_at": comment["created_at"],
        "updated_at": comment["updated_at"],
        "html_url": "https://github.com/%s/issues/%d#issuecomment-%d" % (
            repo.full_name, issue["number"], comment["id"]),
    }


def pull_dict(store, repo, pull):
    head_oid = store.pull_head_oid(repo, pull)
    base_oid = store.pull_base_oid(repo, pull)
    return {
        "id": pull["id"],
        "number": pull["number"],
        "title": pull["title"],
        "body": pull["body"],
        "state": pull["state"],
        "user": user_json(pull["user"], 1000),
        "head": {
            "ref": pull["head"], "sha": head_oid,
            "label": "%s:%s" % (repo.owner, pull["head"]),
            "user": user_json(repo.owner, 1000),
            "repo": {"id": repo.id, "name": repo.name, "full_name": repo.full_name},
        },
        "base": {
            "ref": pull["base"], "sha": base_oid,
            "label": "%s:%s" % (repo.owner, pull["base"]),
            "user": user_json(repo.owner, 1000),
            "repo": {"id": repo.id, "name": repo.name, "full_name": repo.full_name},
        },
        "merged": pull["merged"],
        "mergeable": not store.pull_conflicts(repo, pull) if pull["state"] == "open" else None,
        "merged_at": pull["merged_at"],
        "merge_commit_sha": pull["merge_commit_sha"],
        "created_at": pull["created_at"],
        "updated_at": pull["updated_at"],
        "closed_at": pull["closed_at"],
        "html_url": "https://github.com/%s/pull/%d" % (repo.full_name, pull["number"]),
    }


def branch_dict(name, sha):
    return {"name": name, "commit": {"sha": sha}, "protected": False}


def ref_dict(refname, oid):
    return {
        "ref": refname,
        "node_id": base64.b64encode(("Ref:%s" % refname).encode()).decode(),
        "object": {"sha": oid, "type": "commit"},
    }


def commit_dict(store, repo, oid):
    parsed = store.get_commit(repo, oid)
    if parsed is None:
        return None
    return {
        "sha": oid,
        "commit": {
            "author": {
                "name": parsed["author_name"], "email": parsed["author_email"],
                "date": store.clock.iso_of_ts(parsed["author_ts"]),
            },
            "committer": {
                "name": parsed["committer_name"], "email": parsed["committer_email"],
                "date": store.clock.iso_of_ts(parsed["committer_ts"]),
            },
            "message": parsed["message"],
            "tree": {"sha": parsed["tree"]},
        },
        "author": user_json(parsed["author_name"]),
        "committer": user_json(parsed["committer_name"]),
        "parents": [{"sha": parent} for parent in parsed["parents"]],
    }


def content_file_dict(info, content_b64):
    return {
        "type": "file", "encoding": "base64", "size": info["size"],
        "name": info["name"], "path": info["path"],
        "content": content_b64, "sha": info["sha"],
    }


# ---------------------------------------------------------------- GraphQL ids

def gid(kind, *parts):
    raw = ":".join([kind] + [str(part) for part in parts])
    return base64.b64encode(raw.encode()).decode()


def decode_gid(value):
    try:
        raw = base64.b64decode(value.encode(), validate=True).decode()
    except Exception:
        return None
    parts = raw.split(":")
    if len(parts) < 2:
        return None
    return parts[0], parts[1:]
