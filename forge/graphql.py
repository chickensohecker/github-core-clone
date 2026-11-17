"""GraphQL v4 endpoint: a small parser and executor for the query language
subset the conformance suite sends (queries, mutations, variables, aliases,
inline fragments, input objects, batched requests, __type introspection).

Written from the author's own understanding of the GraphQL language.
"""

import base64
import json

from . import serial
from .store import ForgeError

JSON = "application/json"


class GQLError(Exception):
    pass


# ---------------------------------------------------------------- lexer

class _Token:
    __slots__ = ("kind", "value")

    def __init__(self, kind, value):
        self.kind = kind
        self.value = value

    def __repr__(self):
        return "Token(%r, %r)" % (self.kind, self.value)


_PUNCT = set("!$():=@[]{}|")


def _tokenize(text):
    tokens = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if ch in " \t\r\n,":
            i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == '"':
            if text.startswith('"""', i):
                end = text.find('"""', i + 3)
                if end < 0:
                    raise GQLError("unterminated block string")
                tokens.append(_Token("STRING", text[i + 3:end]))
                i = end + 3
                continue
            j = i + 1
            out = []
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    j += 1
                    if j >= n:
                        raise GQLError("unterminated string")
                    esc = text[j]
                    mapping = {'"': '"', "\\": "\\", "/": "/", "b": "\b",
                               "f": "\f", "n": "\n", "r": "\r", "t": "\t"}
                    if esc == "u":
                        out.append(chr(int(text[j + 1:j + 5], 16)))
                        j += 5
                    elif esc in mapping:
                        out.append(mapping[esc])
                        j += 1
                    else:
                        raise GQLError("bad string escape \\%s" % esc)
                else:
                    out.append(text[j])
                    j += 1
            if j >= n:
                raise GQLError("unterminated string")
            tokens.append(_Token("STRING", "".join(out)))
            i = j + 1
            continue
        if ch == ".":
            if text.startswith("...", i):
                tokens.append(_Token("...", "..."))
                i += 3
                continue
            raise GQLError("unexpected .")
        if ch in _PUNCT:
            tokens.append(_Token(ch, ch))
            i += 1
            continue
        if ch.isdigit() or (ch == "-" and i + 1 < n and text[i + 1].isdigit()):
            j = i
            if text[j] == "-":
                j += 1
            while j < n and text[j].isdigit():
                j += 1
            is_float = False
            if j < n and text[j] == "." and j + 1 < n and text[j + 1].isdigit():
                is_float = True
                j += 1
                while j < n and text[j].isdigit():
                    j += 1
            if j < n and text[j] in "eE":
                is_float = True
                j += 1
                if j < n and text[j] in "+-":
                    j += 1
                while j < n and text[j].isdigit():
                    j += 1
            raw = text[i:j]
            tokens.append(_Token("FLOAT" if is_float else "INT",
                                 float(raw) if is_float else int(raw)))
            i = j
            continue
        if ch == "_" or ch.isalpha():
            j = i
            while j < n and (text[j] == "_" or text[j].isalnum()):
                j += 1
            tokens.append(_Token("NAME", text[i:j]))
            i = j
            continue
        raise GQLError("unexpected character %r" % ch)
    tokens.append(_Token("EOF", None))
    return tokens


# ---------------------------------------------------------------- parser
# AST nodes are plain dicts:
#   operation: {kind: 'operation', op: 'query'|'mutation', name, variables: {name: default},
#               selections: [...]}
#   fragment:  {kind: 'fragment', name, type, selections: [...]}
#   field:     {kind: 'field', name, alias, args: {name: value}, selections: [...]|None}
#   spread:    {kind: 'spread', name}
#   inline:    {kind: 'inline', type: str|None, selections: [...]}
# Values: ('var', name) | ('enum', name) | int | float | str | bool | None |
#         ('list', [...]) | ('object', {name: value})

class _Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self):
        return self.tokens[self.pos]

    def next(self):
        token = self.tokens[self.pos]
        self.pos += 1
        return token

    def expect(self, kind):
        token = self.next()
        if token.kind != kind:
            raise GQLError("expected %s, got %s" % (kind, token.kind))
        return token

    def skip(self, kind):
        if self.peek().kind == kind:
            self.pos += 1
            return True
        return False

    def parse_document(self):
        definitions = []
        while self.peek().kind != "EOF":
            definitions.append(self.parse_definition())
        return definitions

    def parse_definition(self):
        if self.peek().kind == "NAME" and self.peek().value == "fragment":
            return self.parse_fragment()
        return self.parse_operation()

    def parse_fragment(self):
        self.expect("NAME")  # fragment
        name = self.expect("NAME").value
        on = self.expect("NAME")
        if on.value != "on":
            raise GQLError("expected 'on' in fragment")
        type_name = self.expect("NAME").value
        self._skip_directives()
        selections = self.parse_selection_set()
        return {"kind": "fragment", "name": name, "type": type_name,
                "selections": selections}

    def parse_operation(self):
        op = "query"
        name = None
        variables = {}
        if self.peek().kind == "{":
            selections = self.parse_selection_set()
            return {"kind": "operation", "op": op, "name": name,
                    "variables": variables, "selections": selections}
        keyword = self.expect("NAME").value
        if keyword not in ("query", "mutation", "subscription"):
            raise GQLError("unexpected operation %r" % keyword)
        op = keyword
        if self.peek().kind == "NAME":
            name = self.next().value
        if self.peek().kind == "(":
            variables = self.parse_variable_definitions()
        self._skip_directives()
        selections = self.parse_selection_set()
        return {"kind": "operation", "op": op, "name": name,
                "variables": variables, "selections": selections}

    def parse_variable_definitions(self):
        variables = {}
        self.expect("(")
        while not self.skip(")"):
            self.expect("$")
            name = self.expect("NAME").value
            self.expect(":")
            self.parse_type()
            default = None
            if self.skip("="):
                default = self.parse_value()
            variables[name] = default
        return variables

    def parse_type(self):
        if self.skip("["):
            self.parse_type()
            self.expect("]")
        else:
            self.expect("NAME")
        self.skip("!")

    def _skip_directives(self):
        while self.peek().kind == "@":
            self.next()
            self.expect("NAME")
            if self.peek().kind == "(":
                self.parse_arguments()

    def parse_selection_set(self):
        self.expect("{")
        selections = []
        while not self.skip("}"):
            selections.append(self.parse_selection())
        return selections

    def parse_selection(self):
        if self.peek().kind == "...":
            self.next()
            if self.peek().kind == "NAME" and self.peek().value == "on":
                self.next()
                type_name = self.expect("NAME").value
                self._skip_directives()
                return {"kind": "inline", "type": type_name,
                        "selections": self.parse_selection_set()}
            if self.peek().kind == "NAME":
                name = self.next().value
                self._skip_directives()
                return {"kind": "spread", "name": name}
            self._skip_directives()
            return {"kind": "inline", "type": None,
                    "selections": self.parse_selection_set()}
        name = self.expect("NAME").value
        alias = None
        if self.peek().kind == ":":
            self.next()
            alias = name
            name = self.expect("NAME").value
        args = {}
        if self.peek().kind == "(":
            args = self.parse_arguments()
        self._skip_directives()
        selections = None
        if self.peek().kind == "{":
            selections = self.parse_selection_set()
        return {"kind": "field", "name": name, "alias": alias, "args": args,
                "selections": selections}

    def parse_arguments(self):
        args = {}
        self.expect("(")
        while not self.skip(")"):
            name = self.expect("NAME").value
            self.expect(":")
            args[name] = self.parse_value()
        return args

    def parse_value(self):
        token = self.next()
        if token.kind == "$":
            return ("var", self.expect("NAME").value)
        if token.kind == "INT" or token.kind == "FLOAT" or token.kind == "STRING":
            return token.value
        if token.kind == "NAME":
            if token.value == "true":
                return True
            if token.value == "false":
                return False
            if token.value == "null":
                return None
            return ("enum", token.value)
        if token.kind == "[":
            values = []
            while not self.skip("]"):
                values.append(self.parse_value())
            return ("list", values)
        if token.kind == "{":
            fields = {}
            while not self.skip("}"):
                name = self.expect("NAME").value
                self.expect(":")
                fields[name] = self.parse_value()
            return ("object", fields)
        raise GQLError("unexpected value %s" % token.kind)


def _eval_value(value, variables):
    if isinstance(value, tuple):
        if value[0] == "var":
            return variables.get(value[1])
        if value[0] == "enum":
            return value[1]
        if value[0] == "list":
            return [_eval_value(item, variables) for item in value[1]]
        if value[0] == "object":
            return {key: _eval_value(item, variables)
                    for key, item in value[1].items()}
    return value


def _eval_args(args, variables):
    return {key: _eval_value(value, variables) for key, value in args.items()}

# ---------------------------------------------------------------- schema
# Type expressions use GraphQL spelling: "String!", "[Issue!]!", "Repository".
# They only feed __type introspection; the executor resolves by name.

SCHEMA = {
    "Query": {"kind": "OBJECT", "fields": {
        "viewer": "User", "repository": "Repository", "node": "Node",
        "__type": "__Type"}},
    "Mutation": {"kind": "OBJECT", "fields": {
        "createIssue": "CreateIssuePayload", "closeIssue": "CloseIssuePayload",
        "createPullRequest": "CreatePullRequestPayload",
        "mergePullRequest": "MergePullRequestPayload",
        "addComment": "AddCommentPayload"}},
    "Repository": {"kind": "OBJECT", "fields": {
        "id": "ID!", "databaseId": "Int", "name": "String!",
        "nameWithOwner": "String!", "owner": "User!", "description": "String",
        "isPrivate": "Boolean!", "defaultBranchRef": "Ref",
        "viewerPermission": "RepositoryPermission!",
        "hasIssuesEnabled": "Boolean!", "url": "String!",
        "createdAt": "String!", "issue": "Issue",
        "issues": "IssueConnection!", "pullRequest": "PullRequest",
        "pullRequests": "PullRequestConnection!"}},
    "Issue": {"kind": "OBJECT", "fields": {
        "id": "ID!", "databaseId": "Int", "number": "Int!",
        "title": "String!", "body": "String!", "state": "IssueState!",
        "createdAt": "String!", "updatedAt": "String!", "closedAt": "String",
        "author": "User", "url": "String!", "repository": "Repository!",
        "labels": "LabelConnection", "comments": "IssueCommentConnection"}},
    "PullRequest": {"kind": "OBJECT", "fields": {
        "id": "ID!", "databaseId": "Int", "number": "Int!",
        "title": "String!", "body": "String!", "state": "PullRequestState!",
        "headRefName": "String!", "baseRefName": "String!",
        "headRefOid": "String", "baseRefOid": "String",
        "mergeable": "MergeableState!", "merged": "Boolean!",
        "mergedAt": "String", "createdAt": "String!", "updatedAt": "String!",
        "author": "User", "url": "String!", "repository": "Repository!",
        "files": "PullRequestFileConnection"}},
    "User": {"kind": "OBJECT", "fields": {
        "login": "String!", "id": "ID!", "databaseId": "Int",
        "name": "String", "email": "String", "avatarUrl": "String!",
        "url": "String!"}},
    "Ref": {"kind": "OBJECT", "fields": {
        "name": "String!", "prefix": "String!", "target": "GitObject"}},
    "GitObject": {"kind": "OBJECT", "fields": {"oid": "String!"}},
    "Label": {"kind": "OBJECT", "fields": {
        "id": "ID!", "name": "String!", "color": "String!",
        "description": "String"}},
    "IssueComment": {"kind": "OBJECT", "fields": {
        "id": "ID!", "databaseId": "Int", "body": "String!",
        "createdAt": "String!", "updatedAt": "String!", "author": "User"}},
    "PullRequestFile": {"kind": "OBJECT", "fields": {
        "path": "String!", "additions": "Int!", "deletions": "Int!",
        "changeType": "String!"}},
    "IssueConnection": {"kind": "OBJECT", "fields": {
        "nodes": "[Issue!]", "edges": "[IssueEdge!]", "pageInfo": "PageInfo!",
        "totalCount": "Int!"}},
    "IssueEdge": {"kind": "OBJECT", "fields": {
        "node": "Issue", "cursor": "String!"}},
    "PullRequestConnection": {"kind": "OBJECT", "fields": {
        "nodes": "[PullRequest!]", "edges": "[PullRequestEdge!]",
        "pageInfo": "PageInfo!", "totalCount": "Int!"}},
    "PullRequestEdge": {"kind": "OBJECT", "fields": {
        "node": "PullRequest", "cursor": "String!"}},
    "IssueCommentConnection": {"kind": "OBJECT", "fields": {
        "nodes": "[IssueComment!]", "edges": "[IssueCommentEdge!]",
        "pageInfo": "PageInfo!", "totalCount": "Int!"}},
    "IssueCommentEdge": {"kind": "OBJECT", "fields": {
        "node": "IssueComment", "cursor": "String!"}},
    "LabelConnection": {"kind": "OBJECT", "fields": {
        "nodes": "[Label!]", "edges": "[LabelEdge!]", "pageInfo": "PageInfo!",
        "totalCount": "Int!"}},
    "LabelEdge": {"kind": "OBJECT", "fields": {
        "node": "Label", "cursor": "String!"}},
    "PullRequestFileConnection": {"kind": "OBJECT", "fields": {
        "nodes": "[PullRequestFile!]", "edges": "[PullRequestFileEdge!]",
        "pageInfo": "PageInfo!", "totalCount": "Int!"}},
    "PullRequestFileEdge": {"kind": "OBJECT", "fields": {
        "node": "PullRequestFile", "cursor": "String!"}},
    "PageInfo": {"kind": "OBJECT", "fields": {
        "hasNextPage": "Boolean!", "hasPreviousPage": "Boolean!",
        "startCursor": "String", "endCursor": "String"}},
    "CreateIssuePayload": {"kind": "OBJECT", "fields": {
        "issue": "Issue", "clientMutationId": "String"}},
    "CloseIssuePayload": {"kind": "OBJECT", "fields": {
        "issue": "Issue", "clientMutationId": "String"}},
    "CreatePullRequestPayload": {"kind": "OBJECT", "fields": {
        "pullRequest": "PullRequest", "clientMutationId": "String"}},
    "MergePullRequestPayload": {"kind": "OBJECT", "fields": {
        "pullRequest": "PullRequest", "clientMutationId": "String"}},
    "AddCommentPayload": {"kind": "OBJECT", "fields": {
        "commentEdge": "IssueCommentEdge", "clientMutationId": "String"}},
    "__Type": {"kind": "OBJECT", "fields": {
        "name": "String", "kind": "__TypeKind!", "description": "String",
        "fields": "[__Field!]", "enumValues": "[__EnumValue!]",
        "inputFields": "[__InputValue!]", "ofType": "__Type",
        "interfaces": "[__Type!]", "possibleTypes": "[__Type!]"}},
    "__Field": {"kind": "OBJECT", "fields": {
        "name": "String!", "description": "String", "type": "__Type!",
        "args": "[__InputValue!]!"}},
    "__InputValue": {"kind": "OBJECT", "fields": {
        "name": "String!", "description": "String", "type": "__Type!",
        "defaultValue": "String"}},
    "__EnumValue": {"kind": "OBJECT", "fields": {
        "name": "String!", "description": "String"}},
    "Node": {"kind": "INTERFACE", "fields": {"id": "ID!"}},
    "IssueState": {"kind": "ENUM", "values": ["OPEN", "CLOSED"]},
    "PullRequestState": {"kind": "ENUM", "values": ["OPEN", "CLOSED", "MERGED"]},
    "MergeableState": {"kind": "ENUM",
                       "values": ["MERGEABLE", "CONFLICTING", "UNKNOWN"]},
    "RepositoryPermission": {"kind": "ENUM",
                             "values": ["ADMIN", "MAINTAIN", "WRITE", "TRIAGE", "READ"]},
    "SearchType": {"kind": "ENUM", "values": ["ISSUE", "REPOSITORY", "USER"]},
    "IssueOrderField": {"kind": "ENUM",
                        "values": ["CREATED_AT", "UPDATED_AT", "COMMENTS"]},
    "PullRequestOrderField": {"kind": "ENUM",
                              "values": ["CREATED_AT", "UPDATED_AT"]},
    "OrderDirection": {"kind": "ENUM", "values": ["ASC", "DESC"]},
    "PullRequestMergeMethod": {"kind": "ENUM",
                               "values": ["MERGE", "SQUASH", "REBASE"]},
    "__TypeKind": {"kind": "ENUM", "values": [
        "SCALAR", "OBJECT", "INTERFACE", "UNION", "ENUM", "INPUT_OBJECT",
        "LIST", "NON_NULL"]},
    "CreateIssueInput": {"kind": "INPUT_OBJECT", "fields": {
        "repositoryId": "ID!", "title": "String!", "body": "String",
        "clientMutationId": "String"}},
    "CloseIssueInput": {"kind": "INPUT_OBJECT", "fields": {
        "issueId": "ID!", "clientMutationId": "String"}},
    "CreatePullRequestInput": {"kind": "INPUT_OBJECT", "fields": {
        "repositoryId": "ID!", "baseRefName": "String!",
        "headRefName": "String!", "title": "String!", "body": "String",
        "clientMutationId": "String"}},
    "MergePullRequestInput": {"kind": "INPUT_OBJECT", "fields": {
        "pullRequestId": "ID!", "mergeMethod": "PullRequestMergeMethod",
        "clientMutationId": "String"}},
    "AddCommentInput": {"kind": "INPUT_OBJECT", "fields": {
        "subjectId": "ID!", "body": "String!", "clientMutationId": "String"}},
    "IssueOrder": {"kind": "INPUT_OBJECT", "fields": {
        "field": "IssueOrderField!", "direction": "OrderDirection!"}},
    "PullRequestOrder": {"kind": "INPUT_OBJECT", "fields": {
        "field": "PullRequestOrderField!", "direction": "OrderDirection!"}},
    "String": {"kind": "SCALAR"},
    "Int": {"kind": "SCALAR"},
    "Boolean": {"kind": "SCALAR"},
    "ID": {"kind": "SCALAR"},
}

NODE_TYPES = ("Repository", "Issue", "PullRequest", "IssueComment")


class Obj:
    __slots__ = ("type", "data")

    def __init__(self, type_name, data):
        self.type = type_name
        self.data = data


class Ctx:
    def __init__(self, store):
        self.store = store
        self.errors = []


# ---------------------------------------------------------------- helpers

def _cursor(index):
    return base64.b64encode(("cursor:%d" % index).encode()).decode()


def _after_index(after):
    if not isinstance(after, str):
        return 0
    try:
        raw = base64.b64decode(after.encode(), validate=True).decode()
    except Exception:
        return 0
    if not raw.startswith("cursor:"):
        return 0
    try:
        return int(raw[len("cursor:"):]) + 1
    except ValueError:
        return 0


def _connection(items, item_type, first, after):
    """items: ordered list of Obj. Returns Obj(<item_type>Connection, ...)."""
    total = len(items)
    start = _after_index(after)
    if first is None:
        page = items[start:]
    else:
        page = items[start:start + max(0, int(first))]
    end = start + len(page)
    edge_type = item_type + "Edge"
    edges = [Obj(edge_type, {"node": item, "cursor": _cursor(start + i)})
             for i, item in enumerate(page)]
    page_info = Obj("PageInfo", {
        "hasNextPage": end < total,
        "hasPreviousPage": start > 0,
        "startCursor": edges[0].data["cursor"] if edges else None,
        "endCursor": edges[-1].data["cursor"] if edges else None,
    })
    return Obj(item_type + "Connection", {
        "nodes": page, "edges": edges, "pageInfo": page_info,
        "totalCount": total})


def _user_obj(login, user_id=1000, name=None):
    return Obj("User", {"login": login, "id": user_id, "name": name})


def _repo_obj(repo):
    return Obj("Repository", {"repo": repo})


def _issue_obj(repo, issue):
    return Obj("Issue", {"repo": repo, "issue": issue})


def _pull_obj(repo, pull):
    return Obj("PullRequest", {"repo": repo, "pull": pull})


def _find_repo_by_id(store, repo_id):
    for repo in store.repos.values():
        if repo.id == repo_id:
            return repo
    return None


def _resolve_gid(store, value):
    """A global id -> the object it names, or None."""
    if not isinstance(value, str):
        return None
    decoded = serial.decode_gid(value)
    if decoded is None:
        return None
    kind, parts = decoded
    try:
        if kind == "Repository" and len(parts) == 1:
            repo = _find_repo_by_id(store, int(parts[0]))
            return _repo_obj(repo) if repo else None
        if kind == "Issue" and len(parts) == 2:
            repo = _find_repo_by_id(store, int(parts[0]))
            if repo is None:
                return None
            issue = repo.issues.get(int(parts[1]))
            return _issue_obj(repo, issue) if issue else None
        if kind == "PullRequest" and len(parts) == 2:
            repo = _find_repo_by_id(store, int(parts[0]))
            if repo is None:
                return None
            pull = repo.pulls.get(int(parts[1]))
            return _pull_obj(repo, pull) if pull else None
        if kind == "IssueComment" and len(parts) == 3:
            repo = _find_repo_by_id(store, int(parts[0]))
            if repo is None:
                return None
            comment_id = int(parts[2])
            issue = repo.issues.get(int(parts[1]))
            if issue is not None:
                for comment in issue["comments"]:
                    if comment["id"] == comment_id:
                        return Obj("IssueComment", {"repo": repo, "issue": issue,
                                                    "comment": comment})
            pull = repo.pulls.get(int(parts[1]))
            if pull is not None:
                for comment in pull.get("comments", []):
                    if comment["id"] == comment_id:
                        return Obj("IssueComment", {"repo": repo, "pull": pull,
                                                    "comment": comment})
    except (ValueError, IndexError):
        return None
    return None


def _type_obj(type_str):
    """A GraphQL type expression -> an Obj('__Type', ...) introspection node."""
    if type_str.endswith("!"):
        return Obj("__Type", {"name": None, "kind": "NON_NULL",
                              "ofType": _type_obj(type_str[:-1])})
    if type_str.startswith("[") and type_str.endswith("]"):
        return Obj("__Type", {"name": None, "kind": "LIST",
                              "ofType": _type_obj(type_str[1:-1])})
    kind = SCHEMA.get(type_str, {}).get("kind", "SCALAR")
    return Obj("__Type", {"name": type_str, "kind": kind, "ofType": None})


# ---------------------------------------------------------------- resolvers

def _q_viewer(ctx, obj, args):
    viewer = ctx.store.viewer
    return _user_obj(viewer["login"], viewer["id"], viewer["name"])


def _q_repository(ctx, obj, args):
    repo = ctx.store.repo_for(args.get("owner"), args.get("name"))
    return _repo_obj(repo) if repo else None


def _q_node(ctx, obj, args):
    return _resolve_gid(ctx.store, args.get("id"))


def _q_type(ctx, obj, args):
    name = args.get("name")
    if not isinstance(name, str) or name not in SCHEMA:
        return None
    return Obj("__Type", {"name": name, "kind": SCHEMA[name]["kind"],
                          "ofType": None})


def _m_create_issue(ctx, obj, args):
    data = args.get("input") or {}
    target = _resolve_gid(ctx.store, data.get("repositoryId"))
    if target is None or target.type != "Repository":
        raise GQLError("repository not found")
    repo = target.data["repo"]
    issue = ctx.store.create_issue(repo, data.get("title"), data.get("body"))
    return Obj("CreateIssuePayload", {"issue": _issue_obj(repo, issue)})


def _m_close_issue(ctx, obj, args):
    data = args.get("input") or {}
    target = _resolve_gid(ctx.store, data.get("issueId"))
    if target is None or target.type != "Issue":
        raise GQLError("issue not found")
    repo = target.data["repo"]
    issue = target.data["issue"]
    ctx.store.patch_issue(repo, issue, state="closed")
    return Obj("CloseIssuePayload", {"issue": _issue_obj(repo, issue)})


def _m_create_pull(ctx, obj, args):
    data = args.get("input") or {}
    target = _resolve_gid(ctx.store, data.get("repositoryId"))
    if target is None or target.type != "Repository":
        raise GQLError("repository not found")
    repo = target.data["repo"]
    pull = ctx.store.create_pull(repo, data.get("title"), data.get("headRefName"),
                                 data.get("baseRefName"), data.get("body"))
    return Obj("CreatePullRequestPayload", {"pullRequest": _pull_obj(repo, pull)})


def _m_merge_pull(ctx, obj, args):
    data = args.get("input") or {}
    target = _resolve_gid(ctx.store, data.get("pullRequestId"))
    if target is None or target.type != "PullRequest":
        raise GQLError("pull request not found")
    repo = target.data["repo"]
    pull = target.data["pull"]
    method = (data.get("mergeMethod") or "SQUASH")
    if str(method).upper() != "SQUASH":
        raise GQLError("only SQUASH merges are supported")
    ctx.store.merge_pull(repo, pull, "squash")
    return Obj("MergePullRequestPayload", {"pullRequest": _pull_obj(repo, pull)})


def _m_add_comment(ctx, obj, args):
    data = args.get("input") or {}
    target = _resolve_gid(ctx.store, data.get("subjectId"))
    if target is None or target.type not in ("Issue", "PullRequest"):
        raise GQLError("subject not found")
    repo = target.data["repo"]
    body = data.get("body")
    if target.type == "Issue":
        issue = target.data["issue"]
        comment = ctx.store.add_comment(repo, issue, body)
        node = Obj("IssueComment", {"repo": repo, "issue": issue, "comment": comment})
    else:
        pull = target.data["pull"]
        pull.setdefault("comments", [])
        comment = {
            "id": ctx.store.next_id(), "body": body or "",
            "user": "adaiversen",
            "created_at": ctx.store.clock.now_iso(),
            "updated_at": ctx.store.clock.now_iso(),
        }
        pull["comments"].append(comment)
        node = Obj("IssueComment", {"repo": repo, "pull": pull, "comment": comment})
    edge = Obj("IssueCommentEdge", {"node": node, "cursor": _cursor(0)})
    return Obj("AddCommentPayload", {"commentEdge": edge})


def _r_id(ctx, obj, args):
    repo = obj.data["repo"]
    return serial.gid("Repository", repo.id)


def _r_database_id(ctx, obj, args):
    return obj.data["repo"].id


def _r_name(ctx, obj, args):
    return obj.data["repo"].name


def _r_name_with_owner(ctx, obj, args):
    return obj.data["repo"].full_name


def _r_owner(ctx, obj, args):
    return _user_obj(obj.data["repo"].owner, 1000, "ada iversen")


def _r_description(ctx, obj, args):
    return obj.data["repo"].description


def _r_is_private(ctx, obj, args):
    return obj.data["repo"].private


def _r_default_branch(ctx, obj, args):
    repo = obj.data["repo"]
    tip = repo.refs.get("refs/heads/" + repo.default_branch)
    if tip is None:
        return None
    return Obj("Ref", {"repo": repo, "name": repo.default_branch, "oid": tip})


def _r_viewer_permission(ctx, obj, args):
    return "ADMIN"


def _r_has_issues(ctx, obj, args):
    return True


def _r_url(ctx, obj, args):
    return "https://github.com/%s" % obj.data["repo"].full_name


def _r_created_at(ctx, obj, args):
    return obj.data["repo"].created_at


def _r_issue(ctx, obj, args):
    repo = obj.data["repo"]
    issue = repo.issues.get(args.get("number"))
    return _issue_obj(repo, issue) if issue else None


def _r_issues(ctx, obj, args):
    repo = obj.data["repo"]
    states = args.get("states")
    issues = list(repo.issues.values())
    if states:
        wanted = {str(s).lower() for s in states}
        issues = [i for i in issues if i["state"] in wanted]
    order = args.get("orderBy") or {}
    direction = str(order.get("direction") or "ASC").upper()
    field = str(order.get("field") or "CREATED_AT").upper()
    if field == "UPDATED_AT":
        issues.sort(key=lambda i: (i["updated_at"], i["_seq"]))
    else:
        issues.sort(key=lambda i: i["_seq"])
    if direction == "DESC":
        issues.reverse()
    items = [_issue_obj(repo, i) for i in issues]
    return _connection(items, "Issue", args.get("first"), args.get("after"))


def _r_pull_request(ctx, obj, args):
    repo = obj.data["repo"]
    pull = repo.pulls.get(args.get("number"))
    return _pull_obj(repo, pull) if pull else None


def _r_pull_requests(ctx, obj, args):
    repo = obj.data["repo"]
    states = args.get("states")
    pulls = list(repo.pulls.values())
    if states:
        wanted = {str(s).upper() for s in states}
        kept = []
        for pull in pulls:
            state = "MERGED" if pull["merged"] else pull["state"].upper()
            if state in wanted:
                kept.append(pull)
        pulls = kept
    order = args.get("orderBy") or {}
    direction = str(order.get("direction") or "ASC").upper()
    field = str(order.get("field") or "CREATED_AT").upper()
    if field == "UPDATED_AT":
        pulls.sort(key=lambda p: (p["updated_at"], p["_seq"]))
    else:
        pulls.sort(key=lambda p: p["_seq"])
    if direction == "DESC":
        pulls.reverse()
    items = [_pull_obj(repo, p) for p in pulls]
    return _connection(items, "PullRequest", args.get("first"), args.get("after"))


def _i_id(ctx, obj, args):
    issue = obj.data["issue"]
    return serial.gid("Issue", obj.data["repo"].id, issue["number"])


def _i_database_id(ctx, obj, args):
    return obj.data["issue"]["id"]


def _i_number(ctx, obj, args):
    return obj.data["issue"]["number"]


def _i_title(ctx, obj, args):
    return obj.data["issue"]["title"]


def _i_body(ctx, obj, args):
    return obj.data["issue"]["body"] or ""


def _i_state(ctx, obj, args):
    return obj.data["issue"]["state"].upper()


def _i_created_at(ctx, obj, args):
    return obj.data["issue"]["created_at"]


def _i_updated_at(ctx, obj, args):
    return obj.data["issue"]["updated_at"]


def _i_closed_at(ctx, obj, args):
    return obj.data["issue"]["closed_at"]


def _i_author(ctx, obj, args):
    return _user_obj(obj.data["issue"]["user"], 1000)


def _i_url(ctx, obj, args):
    repo = obj.data["repo"]
    return "https://github.com/%s/issues/%d" % (
        repo.full_name, obj.data["issue"]["number"])


def _i_repository(ctx, obj, args):
    return _repo_obj(obj.data["repo"])


def _i_labels(ctx, obj, args):
    repo = obj.data["repo"]
    items = [Obj("Label", {"repo": repo, "label": label})
             for label in obj.data["issue"]["labels"]]
    return _connection(items, "Label", args.get("first"), args.get("after"))


def _i_comments(ctx, obj, args):
    repo = obj.data["repo"]
    issue = obj.data["issue"]
    items = [Obj("IssueComment", {"repo": repo, "issue": issue, "comment": c})
             for c in issue["comments"]]
    return _connection(items, "IssueComment", args.get("first"), args.get("after"))


def _p_id(ctx, obj, args):
    pull = obj.data["pull"]
    return serial.gid("PullRequest", obj.data["repo"].id, pull["number"])


def _p_database_id(ctx, obj, args):
    return obj.data["pull"]["id"]


def _p_number(ctx, obj, args):
    return obj.data["pull"]["number"]


def _p_title(ctx, obj, args):
    return obj.data["pull"]["title"]


def _p_body(ctx, obj, args):
    return obj.data["pull"]["body"] or ""


def _p_state(ctx, obj, args):
    pull = obj.data["pull"]
    if pull["merged"]:
        return "MERGED"
    return pull["state"].upper()


def _p_head_ref(ctx, obj, args):
    return obj.data["pull"]["head"]


def _p_base_ref(ctx, obj, args):
    return obj.data["pull"]["base"]


def _p_head_oid(ctx, obj, args):
    return ctx.store.pull_head_oid(obj.data["repo"], obj.data["pull"])


def _p_base_oid(ctx, obj, args):
    return ctx.store.pull_base_oid(obj.data["repo"], obj.data["pull"])


def _p_mergeable(ctx, obj, args):
    repo = obj.data["repo"]
    pull = obj.data["pull"]
    if pull["state"] != "open":
        return "UNKNOWN"
    return "CONFLICTING" if ctx.store.pull_conflicts(repo, pull) else "MERGEABLE"


def _p_merged(ctx, obj, args):
    return obj.data["pull"]["merged"]


def _p_merged_at(ctx, obj, args):
    return obj.data["pull"]["merged_at"]


def _p_created_at(ctx, obj, args):
    return obj.data["pull"]["created_at"]


def _p_updated_at(ctx, obj, args):
    return obj.data["pull"]["updated_at"]


def _p_author(ctx, obj, args):
    return _user_obj(obj.data["pull"]["user"], 1000)


def _p_url(ctx, obj, args):
    repo = obj.data["repo"]
    return "https://github.com/%s/pull/%d" % (
        repo.full_name, obj.data["pull"]["number"])


def _p_repository(ctx, obj, args):
    return _repo_obj(obj.data["repo"])


def _p_files(ctx, obj, args):
    repo = obj.data["repo"]
    pull = obj.data["pull"]
    items = []
    for entry in ctx.store.pr_files(repo, pull):
        items.append(Obj("PullRequestFile", {
            "path": entry["filename"], "additions": entry["additions"],
            "deletions": entry["deletions"],
            "changeType": entry["status"].upper()}))
    return _connection(items, "PullRequestFile", args.get("first"), args.get("after"))


def _u_login(ctx, obj, args):
    return obj.data["login"]


def _u_id(ctx, obj, args):
    return serial.gid("User", obj.data["login"])


def _u_database_id(ctx, obj, args):
    return obj.data.get("id")


def _u_name(ctx, obj, args):
    return obj.data.get("name")


def _u_email(ctx, obj, args):
    return obj.data.get("email")


def _u_avatar(ctx, obj, args):
    return "https://avatars.githubusercontent.com/u/%d" % (obj.data.get("id") or 1000)


def _u_url(ctx, obj, args):
    return "https://github.com/%s" % obj.data["login"]


def _ref_name(ctx, obj, args):
    return obj.data["name"]


def _ref_prefix(ctx, obj, args):
    return "refs/heads/"


def _ref_target(ctx, obj, args):
    oid = obj.data.get("oid")
    return Obj("GitObject", {"oid": oid}) if oid else None


def _go_oid(ctx, obj, args):
    return obj.data["oid"]


def _label_id(ctx, obj, args):
    label = obj.data["label"]
    return serial.gid("Label", obj.data["repo"].id, label["id"])


def _label_name(ctx, obj, args):
    return obj.data["label"]["name"]


def _label_color(ctx, obj, args):
    return obj.data["label"]["color"]


def _label_description(ctx, obj, args):
    return obj.data["label"].get("description", "")


def _c_id(ctx, obj, args):
    comment = obj.data["comment"]
    repo = obj.data["repo"]
    number = obj.data["issue"]["number"] if "issue" in obj.data else obj.data["pull"]["number"]
    return serial.gid("IssueComment", repo.id, number, comment["id"])


def _c_database_id(ctx, obj, args):
    return obj.data["comment"]["id"]


def _c_body(ctx, obj, args):
    return obj.data["comment"]["body"]


def _c_created_at(ctx, obj, args):
    return obj.data["comment"]["created_at"]


def _c_updated_at(ctx, obj, args):
    return obj.data["comment"]["updated_at"]


def _c_author(ctx, obj, args):
    return _user_obj(obj.data["comment"]["user"], 1000)


def _file_path(ctx, obj, args):
    return obj.data["path"]


def _file_additions(ctx, obj, args):
    return obj.data["additions"]


def _file_deletions(ctx, obj, args):
    return obj.data["deletions"]


def _file_change_type(ctx, obj, args):
    return obj.data["changeType"]


def _conn_nodes(ctx, obj, args):
    return obj.data["nodes"]


def _conn_edges(ctx, obj, args):
    return obj.data["edges"]


def _conn_page_info(ctx, obj, args):
    return obj.data["pageInfo"]


def _conn_total(ctx, obj, args):
    return obj.data["totalCount"]


def _edge_node(ctx, obj, args):
    return obj.data["node"]


def _edge_cursor(ctx, obj, args):
    return obj.data["cursor"]


def _pi_has_next(ctx, obj, args):
    return obj.data["hasNextPage"]


def _pi_has_prev(ctx, obj, args):
    return obj.data["hasPreviousPage"]


def _pi_start(ctx, obj, args):
    return obj.data["startCursor"]


def _pi_end(ctx, obj, args):
    return obj.data["endCursor"]


def _payload_field(key):
    def resolver(ctx, obj, args):
        return obj.data.get(key)
    return resolver


def _t_name(ctx, obj, args):
    return obj.data.get("name")


def _t_kind(ctx, obj, args):
    return obj.data.get("kind")


def _t_description(ctx, obj, args):
    return None


def _t_fields(ctx, obj, args):
    name = obj.data.get("name")
    if name is None:
        return None
    info = SCHEMA.get(name)
    if info is None or info["kind"] not in ("OBJECT", "INTERFACE", "INPUT_OBJECT"):
        return None
    return [Obj("__Field", {"name": field, "type": type_str, "parent": name})
            for field, type_str in info.get("fields", {}).items()]


def _t_enum_values(ctx, obj, args):
    name = obj.data.get("name")
    if name is None:
        return None
    info = SCHEMA.get(name)
    if info is None or info["kind"] != "ENUM":
        return None
    return [Obj("__EnumValue", {"name": value}) for value in info.get("values", [])]


def _t_input_fields(ctx, obj, args):
    name = obj.data.get("name")
    if name is None:
        return None
    info = SCHEMA.get(name)
    if info is None or info["kind"] != "INPUT_OBJECT":
        return None
    return [Obj("__InputValue", {"name": field, "type": type_str})
            for field, type_str in info.get("fields", {}).items()]


def _t_of_type(ctx, obj, args):
    return obj.data.get("ofType")


def _t_interfaces(ctx, obj, args):
    name = obj.data.get("name")
    if name in ("Repository", "Issue", "PullRequest", "IssueComment"):
        return [Obj("__Type", {"name": "Node", "kind": "INTERFACE", "ofType": None})]
    return []


def _t_possible_types(ctx, obj, args):
    if obj.data.get("name") == "Node":
        return [Obj("__Type", {"name": t, "kind": "OBJECT", "ofType": None})
                for t in NODE_TYPES]
    return None


def _f_name(ctx, obj, args):
    return obj.data["name"]


def _f_description(ctx, obj, args):
    return None


def _f_type(ctx, obj, args):
    return _type_obj(obj.data["type"])


def _f_args(ctx, obj, args):
    return []


def _iv_name(ctx, obj, args):
    return obj.data["name"]


def _iv_description(ctx, obj, args):
    return None


def _iv_type(ctx, obj, args):
    return _type_obj(obj.data["type"])


def _iv_default(ctx, obj, args):
    return None


def _ev_name(ctx, obj, args):
    return obj.data["name"]


def _ev_description(ctx, obj, args):
    return None


def _conn_resolvers():
    return {"nodes": _conn_nodes, "edges": _conn_edges,
            "pageInfo": _conn_page_info, "totalCount": _conn_total}


RESOLVERS = {
    "Query": {"viewer": _q_viewer, "repository": _q_repository,
              "node": _q_node, "__type": _q_type},
    "Mutation": {"createIssue": _m_create_issue, "closeIssue": _m_close_issue,
                 "createPullRequest": _m_create_pull,
                 "mergePullRequest": _m_merge_pull, "addComment": _m_add_comment},
    "Repository": {
        "id": _r_id, "databaseId": _r_database_id, "name": _r_name,
        "nameWithOwner": _r_name_with_owner, "owner": _r_owner,
        "description": _r_description, "isPrivate": _r_is_private,
        "defaultBranchRef": _r_default_branch,
        "viewerPermission": _r_viewer_permission,
        "hasIssuesEnabled": _r_has_issues, "url": _r_url,
        "createdAt": _r_created_at, "issue": _r_issue, "issues": _r_issues,
        "pullRequest": _r_pull_request, "pullRequests": _r_pull_requests},
    "Issue": {
        "id": _i_id, "databaseId": _i_database_id, "number": _i_number,
        "title": _i_title, "body": _i_body, "state": _i_state,
        "createdAt": _i_created_at, "updatedAt": _i_updated_at,
        "closedAt": _i_closed_at, "author": _i_author, "url": _i_url,
        "repository": _i_repository, "labels": _i_labels,
        "comments": _i_comments},
    "PullRequest": {
        "id": _p_id, "databaseId": _p_database_id, "number": _p_number,
        "title": _p_title, "body": _p_body, "state": _p_state,
        "headRefName": _p_head_ref, "baseRefName": _p_base_ref,
        "headRefOid": _p_head_oid, "baseRefOid": _p_base_oid,
        "mergeable": _p_mergeable, "merged": _p_merged,
        "mergedAt": _p_merged_at, "createdAt": _p_created_at,
        "updatedAt": _p_updated_at, "author": _p_author, "url": _p_url,
        "repository": _p_repository, "files": _p_files},
    "User": {"login": _u_login, "id": _u_id, "databaseId": _u_database_id,
             "name": _u_name, "email": _u_email, "avatarUrl": _u_avatar,
             "url": _u_url},
    "Ref": {"name": _ref_name, "prefix": _ref_prefix, "target": _ref_target},
    "GitObject": {"oid": _go_oid},
    "Label": {"id": _label_id, "name": _label_name, "color": _label_color,
              "description": _label_description},
    "IssueComment": {"id": _c_id, "databaseId": _c_database_id,
                     "body": _c_body, "createdAt": _c_created_at,
                     "updatedAt": _c_updated_at, "author": _c_author},
    "PullRequestFile": {"path": _file_path, "additions": _file_additions,
                        "deletions": _file_deletions,
                        "changeType": _file_change_type},
    "IssueConnection": _conn_resolvers(),
    "PullRequestConnection": _conn_resolvers(),
    "IssueCommentConnection": _conn_resolvers(),
    "LabelConnection": _conn_resolvers(),
    "PullRequestFileConnection": _conn_resolvers(),
    "IssueEdge": {"node": _edge_node, "cursor": _edge_cursor},
    "PullRequestEdge": {"node": _edge_node, "cursor": _edge_cursor},
    "IssueCommentEdge": {"node": _edge_node, "cursor": _edge_cursor},
    "LabelEdge": {"node": _edge_node, "cursor": _edge_cursor},
    "PullRequestFileEdge": {"node": _edge_node, "cursor": _edge_cursor},
    "PageInfo": {"hasNextPage": _pi_has_next, "hasPreviousPage": _pi_has_prev,
                 "startCursor": _pi_start, "endCursor": _pi_end},
    "CreateIssuePayload": {"issue": _payload_field("issue"),
                           "clientMutationId": _payload_field("clientMutationId")},
    "CloseIssuePayload": {"issue": _payload_field("issue"),
                          "clientMutationId": _payload_field("clientMutationId")},
    "CreatePullRequestPayload": {"pullRequest": _payload_field("pullRequest"),
                                 "clientMutationId": _payload_field("clientMutationId")},
    "MergePullRequestPayload": {"pullRequest": _payload_field("pullRequest"),
                                "clientMutationId": _payload_field("clientMutationId")},
    "AddCommentPayload": {"commentEdge": _payload_field("commentEdge"),
                          "clientMutationId": _payload_field("clientMutationId")},
    "__Type": {"name": _t_name, "kind": _t_kind, "description": _t_description,
               "fields": _t_fields, "enumValues": _t_enum_values,
               "inputFields": _t_input_fields, "ofType": _t_of_type,
               "interfaces": _t_interfaces, "possibleTypes": _t_possible_types},
    "__Field": {"name": _f_name, "description": _f_description,
                "type": _f_type, "args": _f_args},
    "__InputValue": {"name": _iv_name, "description": _iv_description,
                     "type": _iv_type, "defaultValue": _iv_default},
    "__EnumValue": {"name": _ev_name, "description": _ev_description},
}


# ---------------------------------------------------------------- executor

def _type_matches(condition, actual):
    if condition == actual:
        return True
    if condition == "Node" and actual in NODE_TYPES:
        return True
    return False


def _execute(ctx, type_name, obj, selections, variables, fragments):
    out = {}
    for sel in selections:
        if sel["kind"] == "field":
            name = sel["name"]
            key = sel["alias"] or name
            if name == "__typename":
                out[key] = type_name
                continue
            resolver = RESOLVERS.get(type_name, {}).get(name)
            if resolver is None:
                ctx.errors.append({"message": 'Cannot query field "%s" on type "%s".' % (name, type_name)})
                out[key] = None
                continue
            try:
                value = resolver(ctx, obj, _eval_args(sel["args"], variables))
            except ForgeError as exc:
                ctx.errors.append({"message": exc.message})
                out[key] = None
                continue
            except GQLError as exc:
                ctx.errors.append({"message": str(exc)})
                out[key] = None
                continue
            out[key] = _complete(ctx, value, sel["selections"], variables, fragments)
        elif sel["kind"] == "spread":
            frag = fragments.get(sel["name"])
            if frag is None:
                ctx.errors.append({"message": 'Unknown fragment "%s".' % sel["name"]})
                continue
            if _type_matches(frag["type"], type_name):
                out.update(_execute(ctx, type_name, obj, frag["selections"],
                                    variables, fragments))
        elif sel["kind"] == "inline":
            if sel["type"] is None or _type_matches(sel["type"], type_name):
                out.update(_execute(ctx, type_name, obj, sel["selections"],
                                    variables, fragments))
    return out


def _complete(ctx, value, selections, variables, fragments):
    if value is None:
        return None
    if isinstance(value, Obj):
        if not selections:
            return {}
        return _execute(ctx, value.type, value, selections, variables, fragments)
    if isinstance(value, list):
        return [_complete(ctx, item, selections, variables, fragments)
                for item in value]
    return value


def _run_one(store, payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("query"), str):
        return {"errors": [{"message": "a GraphQL query string is required"}]}
    try:
        definitions = _Parser(_tokenize(payload["query"])).parse_document()
    except GQLError as exc:
        return {"errors": [{"message": str(exc)}]}
    operations = [d for d in definitions if d["kind"] == "operation"]
    fragments = {d["name"]: d for d in definitions if d["kind"] == "fragment"}
    if not operations:
        return {"errors": [{"message": "no operation in the document"}]}
    operation_name = payload.get("operationName")
    operation = None
    if operation_name:
        for op in operations:
            if op["name"] == operation_name:
                operation = op
                break
        if operation is None:
            return {"errors": [{"message": 'Unknown operation named "%s".' % operation_name}]}
    else:
        operation = operations[0]
    variables = dict(operation["variables"])
    provided = payload.get("variables")
    if isinstance(provided, dict):
        for key, value in provided.items():
            if value is not None:
                variables[key] = value
    ctx = Ctx(store)
    root_type = "Mutation" if operation["op"] == "mutation" else "Query"
    data = _execute(ctx, root_type, Obj(root_type, {}), operation["selections"],
                    variables, fragments)
    response = {"data": data}
    if ctx.errors:
        response["errors"] = ctx.errors
    return response


def handle(store, body):
    """POST /api/graphql. Returns (status, content_type, body_bytes)."""
    try:
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return 400, JSON, serial.dumps({"message": "malformed JSON body"})
    if isinstance(payload, list):
        results = [_run_one(store, item) for item in payload]
        return 200, JSON, serial.dumps(results)
    return 200, JSON, serial.dumps(_run_one(store, payload))
