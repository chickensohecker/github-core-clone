"""The forge's in-memory store and business logic.

One Store holds every repository. A Repo holds git objects (commits, trees,
blobs, tags) by SHA-1, refs, issues, pull requests, labels and comments.
Everything lives in dicts; nothing is fsynced. All timestamps come from the
deterministic Clock; all ids come from counters, so a replay of the same
request sequence yields identical state.
"""

import difflib
import threading

from . import gitpack
from .clock import Clock

VIEWER_LOGIN = "adaiversen"
VIEWER_ID = 1000
VIEWER_NAME = "ada iversen"
VIEWER_EMAIL = "adaiversen@example.invalid"

ZERO_OID = gitpack.ZERO_OID


class ForgeError(Exception):
    """An error that maps to an HTTP status with a JSON {message} body."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def _is_oid(value):
    return (
        isinstance(value, str)
        and len(value) == 40
        and all(c in "0123456789abcdef" for c in value.lower())
    )


class Repo:
    __slots__ = (
        "id", "owner", "name", "description", "private", "default_branch",
        "created_at", "updated_at", "pushed_at",
        "objects", "commits", "trees", "refs",
        "issues", "pulls", "labels",
        "next_number", "label_seq", "version", "pack_cache",
    )

    def __init__(self, repo_id, owner, name, description, private, now):
        self.id = repo_id
        self.owner = owner
        self.name = name
        self.description = description or ""
        self.private = bool(private)
        self.default_branch = "main"
        self.created_at = now
        self.updated_at = now
        self.pushed_at = now
        self.objects = {}      # oid -> (otype, payload)
        self.commits = {}      # oid -> parsed commit dict
        self.trees = {}        # oid -> [(mode, name, oid)]
        self.refs = {}         # refname -> oid
        self.issues = {}       # number -> issue dict
        self.pulls = {}        # number -> pull dict
        self.labels = {}       # name -> label dict
        self.next_number = 1
        self.label_seq = 0
        self.version = 0
        self.pack_cache = {}

    @property
    def full_name(self):
        return "%s/%s" % (self.owner, self.name)

    def bump(self):
        self.version += 1
        self.pack_cache.clear()


class Store:
    def __init__(self, token):
        self.token = token
        self.clock = Clock()
        self.lock = threading.RLock()
        self.repos = {}        # (owner, name) -> Repo
        self.repo_seq = 0
        self.obj_seq = 0       # global id counter for repos/issues/pulls/...
        self.req_seq = 0

    # ---------------------------------------------------------------- ids

    def next_id(self):
        self.obj_seq += 1
        return self.obj_seq

    def next_request_id(self):
        self.req_seq += 1
        return "req-%d" % self.req_seq

    @property
    def viewer(self):
        return {
            "login": VIEWER_LOGIN, "id": VIEWER_ID,
            "name": VIEWER_NAME, "email": VIEWER_EMAIL,
        }

    # ---------------------------------------------------------------- repos

    def repo_for(self, owner, name):
        """A repository belongs to the login the token names; the same name
        under another owner is simply not found (404, never 403)."""
        if owner != VIEWER_LOGIN:
            return None
        return self.repos.get((owner, name))

    def create_repo(self, name, private=False, description=""):
        if not isinstance(name, str) or name == "":
            raise ForgeError(422, "name is required")
        if (VIEWER_LOGIN, name) in self.repos:
            raise ForgeError(422, "Repository already exists")
        with self.lock:
            self.repo_seq += 1
            now = self.clock.now_iso()
            repo = Repo(self.next_id(), VIEWER_LOGIN, name, description, private, now)
            self.repos[(VIEWER_LOGIN, name)] = repo
            return repo

    def delete_repo(self, repo):
        with self.lock:
            self.repos.pop((repo.owner, repo.name), None)

    def list_repos(self):
        return sorted(self.repos.values(), key=lambda r: r.id)

    # ---------------------------------------------------------------- objects

    def put_object(self, repo, otype, payload):
        oid = gitpack.oid_of(otype, payload)
        repo.objects[oid] = (otype, payload)
        return oid

    def get_object(self, repo, oid):
        return repo.objects.get(oid)

    def put_objects(self, repo, objects):
        for oid, (otype, payload) in objects.items():
            repo.objects[oid] = (otype, payload)

    def get_commit(self, repo, oid):
        parsed = repo.commits.get(oid)
        if parsed is not None:
            return parsed
        entry = repo.objects.get(oid)
        if entry is None or entry[0] != gitpack.OBJ_COMMIT:
            return None
        parsed = gitpack.parse_commit(entry[1])
        repo.commits[oid] = parsed
        return parsed

    def get_tree(self, repo, oid):
        entries = repo.trees.get(oid)
        if entries is not None:
            return entries
        entry = repo.objects.get(oid)
        if entry is None or entry[0] != gitpack.OBJ_TREE:
            return None
        entries = gitpack.parse_tree(entry[1])
        repo.trees[oid] = entries
        return entries

    def get_tag_target(self, repo, oid):
        """Follow an annotated tag to its target commit oid (or return the
        oid itself when it is not a tag)."""
        entry = repo.objects.get(oid)
        if entry is None:
            return None
        if entry[0] == gitpack.OBJ_TAG:
            return gitpack.parse_tag(entry[1]).get("object")
        return oid

    # ---------------------------------------------------------------- refs

    def get_ref(self, repo, refname):
        return repo.refs.get(refname)

    def set_ref(self, repo, refname, oid):
        with self.lock:
            repo.refs[refname] = oid
            repo.bump()

    def delete_ref(self, repo, refname):
        with self.lock:
            repo.refs.pop(refname, None)
            repo.bump()

    def list_refs(self, repo):
        return sorted(repo.refs.items())

    def default_tip(self, repo):
        return repo.refs.get("refs/heads/" + repo.default_branch)

    def resolve_ref(self, repo, ref):
        """Resolve a branch name, full ref name or commit oid to a commit oid.
        Returns None when it cannot be resolved."""
        if not ref:
            return self.default_tip(repo)
        if _is_oid(ref):
            if self.get_commit(repo, ref) is not None:
                return ref
            return None
        if ref.startswith("refs/"):
            oid = repo.refs.get(ref)
            if oid is None:
                return None
            return self.get_tag_target(repo, oid)
        oid = repo.refs.get("refs/heads/" + ref)
        if oid is not None:
            return oid
        oid = repo.refs.get("refs/tags/" + ref)
        if oid is not None:
            return self.get_tag_target(repo, oid)
        return None

    # ---------------------------------------------------------------- history

    def ancestors(self, repo, tip, limit=100000):
        """All commit oids reachable from tip, newest first (a stack walk;
        linear histories come out exactly newest first)."""
        out = []
        seen = set()
        stack = [tip]
        while stack and len(out) < limit:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            parsed = self.get_commit(repo, oid)
            if parsed is None:
                continue
            out.append(oid)
            for parent in parsed["parents"]:
                if parent not in seen:
                    stack.append(parent)
        return out

    def is_ancestor(self, repo, ancestor, descendant, limit=100000):
        """True when ancestor is reachable from descendant (or equal)."""
        if ancestor == descendant:
            return True
        stack = [descendant]
        seen = set()
        steps = 0
        while stack and steps < limit:
            oid = stack.pop()
            steps += 1
            if oid in seen:
                continue
            seen.add(oid)
            parsed = self.get_commit(repo, oid)
            if parsed is None:
                continue
            for parent in parsed["parents"]:
                if parent == ancestor:
                    return True
                if parent not in seen:
                    stack.append(parent)
        return False

    def merge_base(self, repo, a, b):
        """The nearest common ancestor of a and b, or None."""
        if a is None or b is None:
            return None
        ancestors_a = set(self.ancestors(repo, a))
        if b in ancestors_a:
            return b
        stack = [b]
        seen = set()
        while stack:
            oid = stack.pop()
            if oid in seen:
                continue
            seen.add(oid)
            if oid in ancestors_a:
                return oid
            parsed = self.get_commit(repo, oid)
            if parsed is None:
                continue
            for parent in parsed["parents"]:
                if parent not in seen:
                    stack.append(parent)
        return None

    # ---------------------------------------------------------------- trees

    def flatten_tree(self, repo, tree_oid):
        """A tree oid -> {path: (mode, oid)} for every blob in it."""
        flat = {}

        def walk(oid, prefix):
            entries = self.get_tree(repo, oid)
            if entries is None:
                return
            for mode, name, child in entries:
                path = prefix + name
                if mode == "40000":
                    walk(child, path + "/")
                else:
                    flat[path] = (mode, child)

        if tree_oid:
            walk(tree_oid, "")
        return flat

    def build_tree_from_map(self, repo, flat):
        """A {path: (mode, oid)} map -> a new root tree oid (new tree objects
        are stored in the repo)."""
        nested = {}

        def insert(parts, mode, oid):
            node = nested
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            node[parts[-1]] = (mode, oid)

        for path, (mode, oid) in flat.items():
            insert(path.split("/"), mode, oid)

        def build(node):
            entries = []
            for name, value in node.items():
                if isinstance(value, dict):
                    child_oid = build(value)
                    entries.append(("40000", name, child_oid))
                else:
                    mode, oid = value
                    entries.append((mode, name, oid))
            payload = gitpack.build_tree(entries)
            return self.put_object(repo, gitpack.OBJ_TREE, payload)

        return build(nested)

    def update_tree(self, repo, root_oid, path, blob_oid):
        """Return a new root tree oid with path set to blob_oid (or removed
        when blob_oid is None). root_oid may be None (empty tree)."""
        flat = self.flatten_tree(repo, root_oid) if root_oid else {}
        if blob_oid is None:
            flat.pop(path, None)
        else:
            flat[path] = ("100644", blob_oid)
        if not flat:
            payload = gitpack.build_tree([])
            return self.put_object(repo, gitpack.OBJ_TREE, payload)
        return self.build_tree_from_map(repo, flat)

    def diff_trees(self, repo, base_tree, head_tree):
        """{path: (old_oid_or_None, new_oid_or_None, old_mode, new_mode)}
        for every path that differs between two trees."""
        base = self.flatten_tree(repo, base_tree) if base_tree else {}
        head = self.flatten_tree(repo, head_tree) if head_tree else {}
        out = {}
        for path in sorted(set(base) | set(head)):
            b = base.get(path)
            h = head.get(path)
            if b == h:
                continue
            out[path] = (
                b[1] if b else None, h[1] if h else None,
                b[0] if b else None, h[0] if h else None,
            )
        return out

    # ---------------------------------------------------------------- commits

    def make_server_commit(self, repo, tree_oid, parents, message):
        """Create a commit object as the viewer, with a deterministic
        timestamp. Returns its oid."""
        ts = self.clock.now_ts()
        ident = "%s <%s> %d +0000" % (VIEWER_NAME, VIEWER_EMAIL, ts)
        payload = gitpack.build_commit(tree_oid, parents, ident, ident, message)
        return self.put_object(repo, gitpack.OBJ_COMMIT, payload)

    # ---------------------------------------------------------------- labels

    def create_label(self, repo, name, color, description=""):
        if not isinstance(name, str) or name == "":
            raise ForgeError(422, "name is required")
        if name in repo.labels:
            raise ForgeError(422, "Label already exists")
        label = {
            "id": self.next_id(),
            "name": name,
            "color": (color or "ededed").lstrip("#"),
            "description": description or "",
            "default": False,
        }
        repo.labels[name] = label
        return label

    # ---------------------------------------------------------------- contents

    def put_content(self, repo, path, message, content_b64, branch=None, sha=None):
        """Create or update a file. Returns (status, info, commit_oid) where
        info describes the stored blob. Raises ForgeError on 409/422."""
        import base64

        if not isinstance(message, str) or message == "":
            raise ForgeError(422, "message is required")
        if not isinstance(content_b64, str):
            raise ForgeError(422, "content is required")
        try:
            raw = base64.b64decode("".join(content_b64.split()), validate=True)
        except Exception:
            raise ForgeError(422, "content is not valid base64")
        branch = branch or repo.default_branch
        refname = "refs/heads/" + branch
        tip = repo.refs.get(refname)
        if tip is None and repo.refs:
            # A new branch starts at the default branch tip.
            tip = self.default_tip(repo)
            if tip is None:
                raise ForgeError(422, "no commits yet")
        if tip is None:
            # The repository is unborn: this commit founds the branch, and
            # the branch becomes the default branch.
            base_tree = None
            parents = []
            first = True
        else:
            parsed = self.get_commit(repo, tip)
            base_tree = parsed["tree"] if parsed else None
            parents = [tip]
            first = False
        current = None
        if base_tree:
            current = self.flatten_tree(repo, base_tree).get(path)
        current_blob = current[1] if current else None
        if current_blob is not None:
            if sha is None:
                raise ForgeError(422, "file already exists")
            if sha != current_blob:
                raise ForgeError(409, "sha does not match the current file")
            status = 200
        else:
            if sha is not None:
                raise ForgeError(409, "sha does not match: no such file")
            status = 201
        blob_oid = self.put_object(repo, gitpack.OBJ_BLOB, raw)
        new_tree = self.update_tree(repo, base_tree, path, blob_oid)
        commit_oid = self.make_server_commit(repo, new_tree, parents, message)
        with self.lock:
            repo.refs[refname] = commit_oid
            if first:
                repo.default_branch = branch
            repo.bump()
            repo.updated_at = self.clock.now_iso()
        info = {"name": path.rsplit("/", 1)[-1], "path": path, "sha": blob_oid,
                "size": len(raw)}
        return status, info, commit_oid

    def get_content(self, repo, path, ref=None):
        """Read a file or directory at a ref. Returns ('file', info) or
        ('dir', [entries]) or None when missing."""
        tip = self.resolve_ref(repo, ref)
        if tip is None:
            return None
        parsed = self.get_commit(repo, tip)
        if parsed is None:
            return None
        flat = self.flatten_tree(repo, parsed["tree"])
        entry = flat.get(path)
        if entry is not None:
            mode, blob_oid = entry
            blob = repo.objects.get(blob_oid)
            size = len(blob[1]) if blob else 0
            return ("file", {"name": path.rsplit("/", 1)[-1], "path": path,
                             "sha": blob_oid, "size": size, "mode": mode})
        prefix = path.rstrip("/") + "/" if path else ""
        children = {}
        for full, (mode, oid) in flat.items():
            if not full.startswith(prefix):
                continue
            rest = full[len(prefix):]
            first, _, _ = rest.partition("/")
            if first not in children:
                if "/" in rest:
                    children[first] = ("dir", None)
                else:
                    blob = repo.objects.get(oid)
                    children[first] = ("file", oid, len(blob[1]) if blob else 0)
        if not children:
            return None
        listing = []
        for name in sorted(children):
            value = children[name]
            if value[0] == "dir":
                listing.append({"type": "dir", "name": name,
                                "path": prefix + name, "sha": "", "size": 0})
            else:
                listing.append({"type": "file", "name": name,
                                "path": prefix + name, "sha": value[1],
                                "size": value[2]})
        return ("dir", listing)

    def delete_content(self, repo, path, message, sha, branch=None):
        """Delete a file. Returns the new commit oid. Raises ForgeError."""
        if not isinstance(message, str) or message == "":
            raise ForgeError(422, "message is required")
        branch = branch or repo.default_branch
        refname = "refs/heads/" + branch
        tip = repo.refs.get(refname)
        if tip is None:
            raise ForgeError(404, "Not Found")
        parsed = self.get_commit(repo, tip)
        flat = self.flatten_tree(repo, parsed["tree"]) if parsed else {}
        current = flat.get(path)
        if current is None:
            raise ForgeError(404, "Not Found")
        if sha != current[1]:
            raise ForgeError(409, "sha does not match the current file")
        new_tree = self.update_tree(repo, parsed["tree"], path, None)
        commit_oid = self.make_server_commit(repo, new_tree, [tip], message)
        with self.lock:
            repo.refs[refname] = commit_oid
            repo.bump()
            repo.updated_at = self.clock.now_iso()
        return commit_oid

    # ---------------------------------------------------------------- issues

    def create_issue(self, repo, title, body=None, labels=None, user=None):
        if not isinstance(title, str) or title == "":
            raise ForgeError(422, "title is required")
        label_objs = []
        if labels:
            for name in labels:
                label = repo.labels.get(name)
                if label is None:
                    raise ForgeError(422, "invalid label: %s" % name)
                label_objs.append(label)
        with self.lock:
            number = repo.next_number
            repo.next_number += 1
            now = self.clock.now_iso()
            issue = {
                "id": self.next_id(), "number": number, "title": title,
                "body": body if body is not None else "", "state": "open", "labels": label_objs,
                "user": user or VIEWER_LOGIN,
                "created_at": now, "updated_at": now, "closed_at": None,
                "comments": [], "_seq": number, "_json": None,
            }
            repo.issues[number] = issue
        self.rebuild_issue_json(repo, issue)
        return issue

    def rebuild_issue_json(self, repo, issue):
        from . import serial
        issue["_json"] = serial.issue_json_bytes(self, repo, issue)

    def get_issue(self, repo, number):
        return repo.issues.get(number)

    def list_issues(self, repo, state="open", direction="asc"):
        issues = list(repo.issues.values())
        if state == "open":
            issues = [i for i in issues if i["state"] == "open"]
        elif state == "closed":
            issues = [i for i in issues if i["state"] == "closed"]
        issues.sort(key=lambda i: i["_seq"], reverse=(direction == "desc"))
        return issues

    def patch_issue(self, repo, issue, title=None, body=None, state=None, labels=None):
        if title is not None:
            issue["title"] = title
        if body is not None:
            issue["body"] = body
        if state is not None:
            if state not in ("open", "closed"):
                raise ForgeError(422, "invalid state")
            if state != issue["state"]:
                issue["state"] = state
                issue["closed_at"] = self.clock.now_iso() if state == "closed" else None
        if labels is not None:
            label_objs = []
            for name in labels:
                label = repo.labels.get(name)
                if label is None:
                    raise ForgeError(422, "invalid label: %s" % name)
                label_objs.append(label)
            issue["labels"] = label_objs
        issue["updated_at"] = self.clock.now_iso()
        self.rebuild_issue_json(repo, issue)
        return issue

    def add_comment(self, repo, issue, body, user=None):
        if not isinstance(body, str) or body == "":
            raise ForgeError(422, "body is required")
        now = self.clock.now_iso()
        comment = {
            "id": self.next_id(), "body": body, "user": user or VIEWER_LOGIN,
            "created_at": now, "updated_at": now,
        }
        issue["comments"].append(comment)
        issue["updated_at"] = now
        self.rebuild_issue_json(repo, issue)
        return comment

    # ---------------------------------------------------------------- pulls

    def create_pull(self, repo, title, head, base, body=None, user=None):
        if not isinstance(title, str) or title == "":
            raise ForgeError(422, "title is required")
        head_oid = self.resolve_ref(repo, head)
        base_oid = self.resolve_ref(repo, base)
        if head_oid is None:
            raise ForgeError(422, "invalid head: %s" % head)
        if base_oid is None:
            raise ForgeError(422, "invalid base: %s" % base)
        for pull in repo.pulls.values():
            if (pull["state"] == "open" and pull["head"] == head
                    and pull["base"] == base):
                raise ForgeError(422, "a pull request already exists for %s into %s" % (head, base))
        with self.lock:
            number = repo.next_number
            repo.next_number += 1
            now = self.clock.now_iso()
            pull = {
                "id": self.next_id(), "number": number, "title": title,
                "body": body if body is not None else "", "state": "open", "head": head, "base": base,
                "merged": False, "merged_at": None, "merge_commit_sha": None,
                "user": user or VIEWER_LOGIN,
                "created_at": now, "updated_at": now, "closed_at": None,
                "_seq": number,
            }
            repo.pulls[number] = pull
        return pull

    def get_pull(self, repo, number):
        return repo.pulls.get(number)

    def list_pulls(self, repo, state="open", direction="asc"):
        pulls = list(repo.pulls.values())
        if state == "open":
            pulls = [p for p in pulls if p["state"] == "open"]
        elif state == "closed":
            pulls = [p for p in pulls if p["state"] == "closed"]
        pulls.sort(key=lambda p: p["_seq"], reverse=(direction == "desc"))
        return pulls

    def pull_head_oid(self, repo, pull):
        return self.resolve_ref(repo, pull["head"])

    def pull_base_oid(self, repo, pull):
        return self.resolve_ref(repo, pull["base"])

    def pull_conflicts(self, repo, pull):
        """True when the head and the base both changed the same file, to
        different contents, since their merge-base."""
        head_oid = self.pull_head_oid(repo, pull)
        base_oid = self.pull_base_oid(repo, pull)
        if head_oid is None or base_oid is None:
            return False
        base = self.merge_base(repo, head_oid, base_oid)
        if base is None:
            return False
        base_parsed = self.get_commit(repo, base)
        head_parsed = self.get_commit(repo, head_oid)
        base_tip_parsed = self.get_commit(repo, base_oid)
        if not base_parsed or not head_parsed or not base_tip_parsed:
            return False
        head_changes = self.diff_trees(repo, base_parsed["tree"], head_parsed["tree"])
        base_changes = self.diff_trees(repo, base_parsed["tree"], base_tip_parsed["tree"])
        for path, (_, new_oid, _, _) in head_changes.items():
            if path in base_changes and base_changes[path][1] != new_oid:
                return True
        return False

    def merge_pull(self, repo, pull, method="squash"):
        if method != "squash":
            raise ForgeError(422, "only squash merges are supported")
        if pull["merged"]:
            raise ForgeError(405, "Pull Request is already merged")
        if pull["state"] != "open":
            raise ForgeError(405, "Pull Request is closed")
        if self.pull_conflicts(repo, pull):
            raise ForgeError(405, "Merge conflict")
        head_oid = self.pull_head_oid(repo, pull)
        base_oid = self.pull_base_oid(repo, pull)
        base = self.merge_base(repo, head_oid, base_oid)
        if base is None:
            raise ForgeError(405, "No common ancestor")
        base_parsed = self.get_commit(repo, base)
        head_parsed = self.get_commit(repo, head_oid)
        base_tip_parsed = self.get_commit(repo, base_oid)
        changes = self.diff_trees(repo, base_parsed["tree"], head_parsed["tree"])
        if not changes:
            raise ForgeError(405, "No commits between the branches")
        flat = self.flatten_tree(repo, base_tip_parsed["tree"])
        for path, (_, new_oid, _, new_mode) in changes.items():
            if new_oid is None:
                flat.pop(path, None)
            else:
                flat[path] = (new_mode or "100644", new_oid)
        new_tree = self.build_tree_from_map(repo, flat)
        message = "Merge pull request #%d from %s/%s" % (
            pull["number"], repo.owner, pull["head"])
        commit_oid = self.make_server_commit(repo, new_tree, [base_oid], message)
        with self.lock:
            repo.refs["refs/heads/" + pull["base"]] = commit_oid
            repo.bump()
            now = self.clock.now_iso()
            pull["merged"] = True
            pull["state"] = "closed"
            pull["merged_at"] = now
            pull["closed_at"] = now
            pull["updated_at"] = now
            pull["merge_commit_sha"] = commit_oid
            repo.updated_at = now
        return commit_oid, message

    def pr_files(self, repo, pull):
        """The files changed between the merge-base and the head tip, with
        line counts."""
        head_oid = self.pull_head_oid(repo, pull)
        base_oid = self.pull_base_oid(repo, pull)
        if head_oid is None or base_oid is None:
            return []
        base = self.merge_base(repo, head_oid, base_oid)
        if base is None:
            return []
        base_parsed = self.get_commit(repo, base)
        head_parsed = self.get_commit(repo, head_oid)
        changes = self.diff_trees(repo, base_parsed["tree"], head_parsed["tree"])
        out = []
        for path in sorted(changes):
            old_oid, new_oid, old_mode, new_mode = changes[path]
            if old_oid is None:
                status = "added"
                additions, deletions = self._count_lines(repo, None, new_oid)
            elif new_oid is None:
                status = "removed"
                additions, deletions = self._count_lines(repo, old_oid, None)
            elif old_mode != new_mode:
                status = "changed"
                additions, deletions = self._count_lines(repo, old_oid, new_oid)
            else:
                status = "modified"
                additions, deletions = self._count_lines(repo, old_oid, new_oid)
            out.append({
                "filename": path, "status": status,
                "additions": additions, "deletions": deletions,
                "changes": additions + deletions,
            })
        return out

    def _count_lines(self, repo, old_oid, new_oid):
        def lines(oid):
            if oid is None:
                return []
            entry = repo.objects.get(oid)
            if entry is None:
                return []
            return entry[1].decode("utf-8", "replace").splitlines()
        old_lines = lines(old_oid)
        new_lines = lines(new_oid)
        if old_oid is None:
            return len(new_lines), 0
        if new_oid is None:
            return 0, len(old_lines)
        additions = 0
        deletions = 0
        matcher = difflib.SequenceMatcher(None, old_lines, new_lines, autojunk=False)
        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "insert":
                additions += j2 - j1
            elif tag == "delete":
                deletions += i2 - i1
            elif tag == "replace":
                additions += j2 - j1
                deletions += i2 - i1
        return additions, deletions
