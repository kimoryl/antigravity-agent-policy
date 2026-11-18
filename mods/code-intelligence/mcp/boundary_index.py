"""Static polyglot protocol fact extraction and conservative SCIP/OpenAPI/Proto linking.

Zero target imports, zero network/process I/O. Per-file facts are JSON-serializable
for SQLite incremental caching; cross-boundary linking runs across the workspace.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Any, NamedTuple
from urllib.parse import urlsplit

from scip_protocol import (
    extract_scip_facts,
    grpc_scip_symbol,
    http_scip_symbol,
    parse_protocol_scip_symbol,
)

HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}
CONTRACT_EXTENSIONS = {".json", ".yaml", ".yml", ".proto", ".scip"}


def _facts() -> dict[str, list]:
    return {
        key: []
        for key in (
            "endpoints",
            "requests",
            "rpcs",
            "proto_http",
            "route_contracts",
            "diagnostics",
            "suppress_calls",
            "roots",
            "mounts",
            "scip_symbols",
        )
    }


def _snake(name: str) -> str:
    step = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1_\2", name)
    return re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", step).lower()


def _join_route(prefix: str, sub: str) -> str:
    left, right = prefix.strip(), sub.strip()
    if not left and not right:
        return "/"
    if not left:
        return right if right.startswith("/") else "/" + right
    base = left if left.startswith("/") else "/" + left
    if not right:
        return base
    return base.rstrip("/") + "/" + right.lstrip("/")


def _name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return (_name(node.value) + "." + node.attr).strip(".")
    return ""


def _literal(node: ast.AST | None, env: dict[str, Any]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and isinstance(env.get(node.id), str):
        return env[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _literal(node.left, env), _literal(node.right, env)
        return left + right if left is not None and right is not None else None
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                parts.append(part.value)
            elif isinstance(part, ast.FormattedValue):
                known = _literal(part.value, env)
                parts.append(known if known is not None else "{param}")
            else:
                parts.append("{param}")
        return "".join(parts)
    return None


def _keywords(call: ast.Call) -> dict[str, ast.AST]:
    return {kw.arg: kw.value for kw in call.keywords if kw.arg}


def _py_decorator_endpoints(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
    qualified: str,
    rel: str,
    module: str,
    env: dict[str, Any],
    out: dict[str, list],
) -> None:
    for dec in node.decorator_list:
        if not isinstance(dec, ast.Call) or not isinstance(dec.func, ast.Attribute):
            continue
        method = dec.func.attr.upper()
        receiver = _name(dec.func.value)
        config = env.get(receiver)
        if not isinstance(config, dict) or config.get("kind") != "router":
            continue
        out["suppress_calls"].append([qualified, _name(dec.func)])
        kw = _keywords(dec)
        route = _literal(dec.args[0], env) if dec.args else _literal(kw.get("path") or kw.get("rule"), env)
        methods = [method] if method in HTTP_METHODS else []
        if method in {"ROUTE", "API_ROUTE"}:
            methods = ["GET"]
            if "methods" in kw:
                val = kw["methods"]
                methods = [_literal(x, env) for x in val.elts] if isinstance(val, (ast.List, ast.Tuple)) else []
        if route is None or not methods or config.get("prefix") is None:
            out["diagnostics"].append({"path": rel, "line": dec.lineno, "reason": "dynamic_http_route"})
            continue
        for verb in methods:
            if verb and verb.upper() in HTTP_METHODS:
                out["endpoints"].append({
                    "protocol": "http",
                    "method": verb.upper(),
                    "route": config["prefix"] + route,
                    "handler": qualified,
                    "path": rel,
                    "line": dec.lineno,
                    "router": f"{module}.{receiver}",
                })


def _py_constructor_config(
    call: ast.Call,
    expanded_fn: Any,
    env: dict[str, Any],
    module: str,
    targets: list[ast.expr],
    out: dict[str, list],
) -> Any:
    constructor = expanded_fn(_name(call.func))
    short = constructor.rsplit(".", 1)[-1]
    kw = _keywords(call)
    if short in {"FastAPI", "Flask", "APIRouter", "Blueprint"}:
        key = "url_prefix" if short == "Blueprint" else "prefix"
        if short in {"FastAPI", "Flask"}:
            out["roots"].extend(f"{module}.{_name(t)}" for t in targets if _name(t))
        return {"kind": "router", "prefix": _literal(kw[key], env) if key in kw else ""}
    if short.endswith("Stub"):
        return {"kind": "grpc", "service": short[:-4]}
    if short in {"unary_unary", "unary_stream", "stream_unary", "stream_stream"} and call.args:
        rpc_path = _literal(call.args[0], env)
        if rpc_path and re.fullmatch(r"/[\w.]+/\w+", rpc_path):
            service, method = rpc_path[1:].split("/")
            return {"kind": "rpc_method", "service": service, "method": method}
    if constructor in {"requests.Session", "httpx.Client", "httpx.AsyncClient", "aiohttp.ClientSession"}:
        return {"kind": "http", "base": _literal(kw["base_url"], env) if "base_url" in kw else ""}
    return _literal(call, env)


def _py_django_urlpatterns(
    node: ast.Assign,
    expanded_fn: Any,
    env: dict[str, Any],
    aliases: dict[str, str],
    module: str,
    rel: str,
    out: dict[str, list],
) -> None:
    if not any(_name(t) == "urlpatterns" for t in node.targets) or not isinstance(node.value, (ast.List, ast.Tuple)):
        return
    for elt in node.value.elts:
        if not isinstance(elt, ast.Call) or len(elt.args) < 2:
            continue
        fn_name = expanded_fn(_name(elt.func))
        if fn_name not in {"django.urls.path", "django.urls.re_path"}:
            continue
        raw_route = _literal(elt.args[0], env)
        view_name = _name(elt.args[1])
        if raw_route is None or not view_name:
            continue
        head = view_name.split(".")[0]
        handler = expanded_fn(view_name) if head in aliases else f"{module}.{view_name}"
        route = "/" + raw_route.lstrip("^/").rstrip("$")
        for verb in ("GET", "POST"):
            out["endpoints"].append({"protocol": "http", "method": verb, "route": route,
                                     "handler": handler, "path": rel, "line": elt.lineno})


def _python_facts(source: str, rel: str, module: str) -> dict[str, list]:
    out = _facts()
    try:
        tree = ast.parse(source, filename=rel)
    except SyntaxError:
        return out
    aliases: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            aliases.update({a.asname or a.name: a.name for a in stmt.names})
        elif isinstance(stmt, ast.ImportFrom) and stmt.module:
            aliases.update({a.asname or a.name: f"{stmt.module}.{a.name}" for a in stmt.names})

    def expanded(name: str) -> str:
        head, dot, tail = name.partition(".")
        return aliases.get(head, head) + (dot + tail if dot else "")

    class Visitor(ast.NodeVisitor):
        def __init__(self) -> None:
            self.scope: list[str] = []
            self.env: dict[str, Any] = {}
            self.owner = ""
            self.service = ""
            self.shadowed: set[str] = set()

        def visit_ClassDef(self, node: ast.ClassDef) -> None:
            previous = self.service
            services = [_name(b).rsplit(".", 1)[-1][:-8] for b in node.bases if _name(b).endswith("Servicer")]
            self.service = services[0] if len(services) == 1 else ""
            self.scope.append(node.name)
            for child in node.body:
                self.visit(child)
            self.scope.pop()
            self.service = previous

        def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
            qualified = ".".join([module, *self.scope, node.name])
            if self.service:
                out["endpoints"].append({"protocol": "grpc", "service": self.service, "method": node.name,
                                         "handler": qualified, "path": rel, "line": node.lineno})
            _py_decorator_endpoints(node, qualified, rel, module, self.env, out)
            previous = self.owner, self.env, self.service, self.shadowed
            self.owner, self.env, self.service = qualified, dict(self.env), ""
            params = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            params += [a for a in (node.args.vararg, node.args.kwarg) if a]
            self.shadowed = self.shadowed | {a.arg for a in params}
            for arg in params:
                self.env[arg.arg] = None
            self.scope.append(node.name)
            for child in node.body:
                self.visit(child)
            self.scope.pop()
            self.owner, self.env, self.service, self.shadowed = previous

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Assign(self, node: ast.Assign) -> None:
            val: Any = _literal(node.value, self.env)
            if isinstance(node.value, ast.Call):
                val = _py_constructor_config(node.value, expanded, self.env, module, node.targets, out)
            _py_django_urlpatterns(node, expanded, self.env, aliases, module, rel, out)
            for target in node.targets:
                tname = _name(target)
                if tname:
                    self.env[tname] = val
                    self.shadowed.add(tname)
            self.generic_visit(node)

        def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
            if node.value is not None:
                self.visit_Assign(ast.Assign(targets=[node.target], value=node.value))

        def visit_Call(self, node: ast.Call) -> None:
            self._handle_call(node)
            self.generic_visit(node)

        def _handle_call(self, node: ast.Call) -> None:
            called = _name(node.func)
            receiver, _, verb = called.rpartition(".")
            config = self.env.get(receiver)
            rpc_method = self.env.get(called)
            if self.owner and isinstance(rpc_method, dict) and rpc_method.get("kind") == "rpc_method":
                out["requests"].append({"protocol": "grpc", "service": rpc_method["service"],
                                        "method": rpc_method["method"], "caller": self.owner,
                                        "path": rel, "line": node.lineno})
                out["suppress_calls"].append([self.owner, called])
                return
            if verb in {"include_router", "register_blueprint"} and node.args:
                child = _name(node.args[0])
                kw = _keywords(node)
                pkey = "url_prefix" if verb == "register_blueprint" else "prefix"
                prefix = _literal(kw[pkey], self.env) if pkey in kw else ""
                if child and prefix is not None:
                    resolved_child = expanded(child) if child.split(".")[0] in aliases else f"{module}.{child}"
                    out["mounts"].append({"parent": f"{module}.{receiver}", "child": resolved_child, "prefix": prefix})
                else:
                    out["diagnostics"].append({"path": rel, "line": node.lineno, "reason": "dynamic_router_mount"})
            if self.owner and isinstance(config, dict) and config.get("kind") == "grpc":
                out["requests"].append({"protocol": "grpc", "service": config["service"], "method": verb,
                                        "caller": self.owner, "path": rel, "line": node.lineno})
                out["suppress_calls"].append([self.owner, called])
                return
            is_http = (
                bool(receiver) and receiver not in self.shadowed and expanded(receiver) in {"requests", "httpx"}
                or isinstance(config, dict) and config.get("kind") == "http"
            )
            if self.owner and is_http and verb.upper() in HTTP_METHODS | {"REQUEST"}:
                self._record_py_http_request(node, called, verb.upper(), config)

        def _record_py_http_request(self, node: ast.Call, called: str, method: str, config: Any) -> None:
            kw = _keywords(node)
            args = list(node.args)
            verb: str | None = method
            if verb == "REQUEST":
                verb = _literal(args.pop(0), self.env) if args else _literal(kw.get("method"), self.env)
            url = _literal(args[0], self.env) if args else _literal(kw.get("url"), self.env)
            base = config.get("base", "") if isinstance(config, dict) else ""
            if url is not None and base is not None:
                if base and not url.startswith(("http:", "https:")):
                    url = base.rstrip("/") + "/" + url.lstrip("/")
            else:
                url = None
            out["suppress_calls"].append([self.owner, called])
            if url is None or verb is None:
                out["diagnostics"].append({"path": rel, "line": node.lineno, "reason": "dynamic_http_request"})
            else:
                out["requests"].append({"protocol": "http", "method": verb.upper(), "route": url,
                                        "caller": self.owner, "path": rel, "line": node.lineno})

    ast.NodeVisitor.visit(Visitor(), tree)
    return out


class Token(NamedTuple):
    value: str
    kind: str
    line: int
    start: int


_TOKENS = re.compile(
    r"//[^\n]*|/\*[\s\S]*?\*/|\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|[^\s]"
)


def _tokens(source: str) -> list[Token]:
    result: list[Token] = []
    line, end = 1, 0
    for match in _TOKENS.finditer(source):
        line += source.count("\n", end, match.start())
        value = match.group()
        if not value.startswith(("//", "/*")):
            kind = "string" if value[0] in "\"'`" else "id" if re.fullmatch(r"[A-Za-z_$][\w$]*", value) else "punct"
            result.append(Token(value, kind, line, match.start()))
        line += value.count("\n")
        end = match.end()
    return result


def _arguments(tokens: list[Token], opening: int) -> tuple[list[list[Token]], int]:
    stack, args, current = ["("], [], []
    pairs = {")": "(", "]": "[", "}": "{"}
    for index in range(opening + 1, len(tokens)):
        token = tokens[index]
        value = token.value
        if token.kind != "string":
            if value in {"(", "[", "{"}:
                stack.append(value)
            elif value in pairs:
                if not stack or stack.pop() != pairs[value]:
                    return [], index
                if not stack:
                    return (args + [current] if current or args else []), index
            elif value == "," and len(stack) == 1:
                args.append(current)
                current = []
                continue
        current.append(token)
    return [], len(tokens) - 1


def _string_token(token: Token, consts: dict[str, str] | None = None) -> str | None:
    if token.kind != "string":
        return None
    raw = token.value
    if raw[0] == "`":
        value = re.sub(
            r"\$\{([^{}]*)\}",
            lambda m: consts[m.group(1).strip()] if consts and m.group(1).strip() in consts else "{param}",
            raw[1:-1],
        )
    else:
        value = raw[1:-1]
    return None if "\\" in value else value


def _string(tokens: list[Token], consts: dict[str, str] | None = None) -> str | None:
    if not tokens:
        return None
    if len(tokens) == 1:
        if tokens[0].kind == "string":
            return _string_token(tokens[0], consts)
        if consts and tokens[0].kind == "id" and tokens[0].value in consts:
            return consts[tokens[0].value]
        return None
    # Support pure static string concatenation: CONST_BASE + '/users/'
    parts: list[str] = []
    for idx, tok in enumerate(tokens):
        if idx % 2 == 1:
            if tok.value != "+":
                return None
            continue
        piece = _string([tok], consts)
        if piece is None:
            return None
        parts.append(piece)
    return "".join(parts) if parts else None


def _object_option(tokens: list[Token], key: str, default: str) -> str | None:
    """Read only a top-level string property; spreads/computed keys are unknown."""
    if not tokens or tokens[0].value != "{" or tokens[-1].value != "}":
        return None
    depth, result, dynamic = 0, default, False
    for i, token in enumerate(tokens):
        if token.kind != "string":
            if token.value in {"{", "[", "("}:
                if depth == 1 and token.value == "[":
                    dynamic = True
                depth += 1
            elif token.value in {"}", "]", ")"}:
                depth -= 1
        if depth != 1:
            continue
        if token.value == ".":
            dynamic = True
        if token.value in {key, "'" + key + "'", '"' + key + '"'}:
            result = _string(tokens[i + 2:i + 3]) if i + 1 < len(tokens) and tokens[i + 1].value == ":" else None
    return None if dynamic else result


def _scan_lexical_env(tokens: list[Token], owner_fn: Any, module: str, out: dict[str, list]) -> dict[str, Any]:
    values = [t.value for t in tokens]
    consts: dict[tuple[str, str], str] = {}
    clients: dict[tuple[str, str], str] = {}
    http_clients: dict[str, str] = {"axios": ""}
    http_instances: dict[tuple[str, str], str | None] = {}
    sub_routers: dict[str, str] = {}

    for i in range(len(tokens) - 3):
        if values[i] == "const" and tokens[i + 1].kind == "id" and values[i + 2] == "=":
            lit = _string_token(tokens[i + 3]) if tokens[i + 3].kind == "string" else None
            if lit is not None:
                consts[(owner_fn(tokens[i + 1]), values[i + 1])] = lit
        if values[i] == "import" and tokens[i + 1].kind == "id" and values[i + 2] == "from" and _string(tokens[i + 3:i + 4]) == "axios":
            http_clients[values[i + 1]] = ""
        if values[i + 1:i + 3] == ["=", "axios"] and values[i + 3:i + 5] == [".", "create"] and i + 5 < len(tokens) and values[i + 5] == "(":
            args, _ = _arguments(tokens, i + 5)
            http_instances[(owner_fn(tokens[i]), values[i])] = _object_option(args[0], "baseURL", "") if args else None
        if values[i + 1] == "=" and i + 4 < len(tokens):
            # Express Router(): const r = express.Router() or const r = Router()
            if values[i + 2:i + 5] == ["Router", "(", ")"] or (i + 6 < len(tokens) and values[i + 2:i + 7] == ["express", ".", "Router", "(", ")"]):
                sub_routers[values[i]] = ""
            if values[i + 2:i + 5] == ["express", "(", ")"]:
                out["roots"].append(f"{module}.{values[i]}")
        # Go / TS router group: v1 := r.Group("/api/v1") or const v1 = r.Group("/api/v1")
        if values[i + 1] == "=" or values[i + 1:i + 3] == [":", "="]:
            eq_end = i + 2 if values[i + 1] == "=" else i + 3
            if eq_end + 3 < len(tokens) and values[eq_end + 1:eq_end + 3] == [".", "Group"] and values[eq_end + 3] == "(":
                parent_r = values[eq_end]
                args, _ = _arguments(tokens, eq_end + 3)
                grp_prefix = _string(args[0]) if args else None
                if grp_prefix is not None:
                    base_p = sub_routers.get(parent_r, "")
                    sub_routers[values[i]] = _join_route(base_p, grp_prefix) if base_p else grp_prefix
            # Go gRPC client: client := pb.NewCatalogClient(conn) or NewCatalogClient(conn)
            for off in (eq_end, eq_end + 2):
                if off + 1 < len(tokens) and values[off + 1] == "(" and (off == eq_end or values[eq_end + 1] == "."):
                    m_cli = re.fullmatch(r"New(\w+)Client", values[off])
                    if m_cli:
                        clients[(owner_fn(tokens[i]), values[i])] = m_cli.group(1)
            # Java gRPC stub: stub = CatalogGrpc.newBlockingStub(channel)
            if eq_end + 3 < len(tokens) and values[eq_end + 1] == "." and values[eq_end + 3] == "(":
                if values[eq_end].endswith("Grpc") and re.fullmatch(r"new(?:Blocking|Future|Async)?Stub", values[eq_end + 2]):
                    clients[(owner_fn(tokens[i]), values[i])] = values[eq_end][:-4]
            # Rust Tonic client: let mut client = CatalogClient::connect(...) / CatalogClient::new(...)
            if eq_end + 4 < len(tokens) and values[eq_end + 1:eq_end + 3] == [":", ":"] and values[eq_end + 4] == "(":
                if values[eq_end].endswith("Client") and values[eq_end + 3] in {"connect", "new"}:
                    clients[(owner_fn(tokens[i]), values[i])] = values[eq_end][:-6]

    for i in range(len(tokens) - 4):
        if values[i + 1:i + 3] == ["=", "new"] and values[i + 3].endswith(("Client", "Stub")):
            clients[(owner_fn(tokens[i]), values[i])] = re.sub(r"(?:Client|Stub)$", "", values[i + 3])

    return {
        "consts": consts,
        "clients": clients,
        "http_clients": http_clients,
        "http_instances": http_instances,
        "sub_routers": sub_routers,
    }


def _strip_comments_keep_strings(source: str) -> str:
    return _TOKENS.sub(
        lambda m: "".join("\n" if ch == "\n" else " " for ch in m.group(0))
        if m.group(0).startswith(("//", "/*"))
        else m.group(0),
        source,
    )


def _extract_nestjs_decorators(
    source: str, rel: str, module: str, declarations: list[dict], out: dict[str, list]
) -> None:
    if "@Controller" not in source:
        return
    cleaned = _strip_comments_keep_strings(source)
    if "@Controller" not in cleaned:
        return
    declared = {d["qualified"] for d in declarations}
    ctrl_re = re.compile(
        r"@Controller\s*\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)\s*(?:@\w+(?:\([^)]*\))?\s*)*"
        r"(?:export\s+)?(?:default\s+)?class\s+([A-Za-z_]\w*)[^{;]*\{",
        re.M,
    )
    method_re = re.compile(
        r"@(Get|Post|Put|Patch|Delete|Options|Head)\s*\(\s*(?:['\"]([^'\"]*)['\"])?\s*\)\s*"
        r"(?:@\w+(?:\([^)]*\))?\s*)*(?:(?:public|private|protected|async)\s+)*([A-Za-z_]\w*)\s*\(",
        re.M,
    )
    for cm in ctrl_re.finditer(cleaned):
        ctrl_prefix = cm.group(1) or ""
        cname = cm.group(2)
        brace_open = cm.end() - 1
        depth, close_idx = 0, len(cleaned) - 1
        for idx in range(brace_open, len(cleaned)):
            if cleaned[idx] == "{":
                depth += 1
            elif cleaned[idx] == "}":
                depth -= 1
                if depth == 0:
                    close_idx = idx
                    break
        body = cleaned[brace_open + 1:close_idx]
        for mm in method_re.finditer(body):
            verb = mm.group(1).upper()
            sub = mm.group(2) or ""
            mname = mm.group(3)
            qualified = f"{module}.{cname}.{mname}"
            if qualified not in declared:
                continue
            line = cleaned.count("\n", 0, brace_open + 1 + mm.start()) + 1
            out["endpoints"].append({
                "protocol": "http",
                "method": verb,
                "route": _join_route(ctrl_prefix, sub),
                "handler": qualified,
                "path": rel,
                "line": line,
            })


def _lexical_facts(source: str, rel: str, module: str, ext: str, declarations: list[dict]) -> dict[str, list]:
    out = _facts()
    tokens = _tokens(source)
    if ext in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs"}:
        _extract_nestjs_decorators(source, rel, module, declarations, out)

    def owner(token: Token) -> str:
        candidates = [d for d in declarations if d["kind"] in {"function", "method"}
                      and d.get("body_start", -1) <= token.start < d.get("body_end", -1)]
        return min(candidates, key=lambda d: d["body_end"] - d["body_start"])["qualified"] if candidates else ""

    def parameter_shadows(caller: str, name: str) -> bool:
        declaration = next((d for d in declarations if d["qualified"] == caller), {})
        signature = declaration.get("signature", "")
        arguments = signature.partition("(")[2].rpartition(")")[0]
        return bool(name and re.search(r"(?:^|,)\s*" + re.escape(name) + r"\b", arguments))

    env = _scan_lexical_env(tokens, owner, module, out)
    clients = env["clients"]
    http_clients = env["http_clients"]
    http_instances = env["http_instances"]
    sub_routers = env["sub_routers"]

    def visible_consts(caller: str) -> dict[str, str]:
        merged = {k: v for (scope, k), v in env["consts"].items() if scope == ""}
        if caller:
            merged.update({k: v for (scope, k), v in env["consts"].items() if scope == caller})
        for k in list(merged):
            if parameter_shadows(caller, k):
                merged.pop(k, None)
        return merged

    for i, token in enumerate(tokens):
        if token.kind != "id" or (i >= 2 and tokens[i - 1].value == "." and tokens[i - 2].kind == "id"):
            continue
        j, parts = i + 1, [token.value]
        while j + 1 < len(tokens) and tokens[j].value == "." and tokens[j + 1].kind == "id":
            parts.append(tokens[j + 1].value)
            j += 2
        if j < len(tokens) and tokens[j].value == "<":
            depth = 1
            j += 1
            while j < len(tokens) and depth:
                depth += int(tokens[j].value == "<") - int(tokens[j].value == ">")
                j += 1
        if j >= len(tokens) or tokens[j].value != "(":
            continue
        args, _ = _arguments(tokens, j)
        called, caller = ".".join(parts), owner(token)
        receiver, method = ".".join(parts[:-1]), parts[-1]
        if parameter_shadows(caller, receiver or called):
            continue
        consts = visible_consts(caller)

        # Express app.use('/v1', router) mount
        if method == "use" and receiver and len(args) >= 2:
            mount_prefix = _string(args[0], consts)
            child_name = "".join(t.value for t in args[1])
            if mount_prefix is not None and re.fullmatch(r"[A-Za-z_]\w*", child_name):
                if receiver not in sub_routers:
                    out["roots"].append(f"{module}.{receiver}")
                out["mounts"].append({"parent": f"{module}.{receiver}", "child": f"{module}.{child_name}", "prefix": mount_prefix})

        service = clients.get((caller, receiver), clients.get(("", receiver)))
        if service and caller:
            clean_method = method[:-5] if method.endswith("Async") and len(method) > 5 else method
            out["requests"].append({"protocol": "grpc", "service": service, "method": clean_method,
                                    "caller": caller, "path": rel, "line": token.line})
            out["suppress_calls"].append([caller, called])
            continue

        instance = (caller, receiver) if (caller, receiver) in http_instances else ("", receiver)
        is_go_http = ext == ".go" and receiver == "http" and method in {"Get", "Post", "Head"}
        is_request = (
            called == "fetch"
            or called in http_clients
            or is_go_http
            or (receiver in http_clients or instance in http_instances) and method.upper() in HTTP_METHODS
        )
        if is_request and caller:
            route = _string(args[0], consts) if args else None
            verb: str | None = method.upper() if (receiver in http_clients or instance in http_instances or is_go_http) else "GET"
            if len(args) > 1 and called in {"fetch", *http_clients}:
                verb = _object_option(args[1], "method", "GET")
            base = http_instances.get(instance, http_clients.get(receiver or called, ""))
            if base is None:
                route = None
            elif base and route is not None and not route.startswith(("http:", "https:")):
                route = base.rstrip("/") + "/" + route.lstrip("/")
            out["suppress_calls"].append([caller, called])
            if route is None or verb is None:
                out["diagnostics"].append({"path": rel, "line": token.line, "reason": "dynamic_http_request"})
            else:
                out["requests"].append({"protocol": "http", "method": verb.upper(), "route": route,
                                        "caller": caller, "path": rel, "line": token.line})

        _record_lexical_route_endpoint(
            ext, module, rel, token, receiver, method, called, args, consts, sub_routers, declarations, out
        )
    return out


def _record_lexical_route_endpoint(
    ext: str,
    module: str,
    rel: str,
    token: Token,
    receiver: str,
    method: str,
    called: str,
    args: list[list[Token]],
    consts: dict[str, str],
    sub_routers: dict[str, str],
    declarations: list[dict],
    out: dict[str, list],
) -> None:
    is_router_var = receiver in {"app", "router", "r", "engine", "mux", "e", "srv"} or receiver in sub_routers
    if is_router_var and method.upper() in HTTP_METHODS and args:
        route = _string(args[0], consts)
        handler = "".join(t.value for t in args[-1]) if len(args) > 1 else ""
        if route is not None and handler:
            if not re.fullmatch(r"[\w.]+", handler):
                if not any(t.value in {"=>", "function", "func"} for t in args[-1]) and "=" not in handler:
                    return
                syn_name = f"__http_{method.lower()}_L{token.line}"
                handler = f"{module}.{syn_name}"
                declarations.append({"qualified": handler, "name": syn_name, "kind": "function",
                                     "path": rel, "line": token.line, "signature": f"{method.upper()} {route}"})
            else:
                handler = f"{module}.{handler}"
            grp_prefix = sub_routers.get(receiver, "")
            full_route = _join_route(grp_prefix, route) if grp_prefix else route
            ep: dict[str, Any] = {"protocol": "http", "method": method.upper(), "route": full_route,
                                  "handler": handler, "path": rel, "line": token.line}
            if receiver in sub_routers and grp_prefix == "":
                ep["router"] = f"{module}.{receiver}"
            out["endpoints"].append(ep)

    if called == "http.HandleFunc" and len(args) >= 2:
        route, handler = _string(args[0], consts), "".join(t.value for t in args[1])
        if route is not None and re.fullmatch(r"[\w.]+", handler):
            verb, sep, path = route.partition(" ")
            for item in ([verb] if sep else sorted(HTTP_METHODS)):
                if item in HTTP_METHODS:
                    out["endpoints"].append({"protocol": "http", "method": item,
                                             "route": path if sep else route,
                                             "handler": f"{module}.{handler}", "path": rel, "line": token.line})

    # Rust Axum `.route("/users/:id", get(get_user).post(update_user))`
    if ext == ".rs" and method == "route" and len(args) >= 2:
        route = _string(args[0], consts)
        if route is not None:
            chain_text = "".join(t.value for t in args[1])
            for m_axum in re.finditer(r"(?:^|\.|::)(get|post|put|patch|delete|head|options)\(([A-Za-z_][\w:]*)\)", chain_text):
                verb = m_axum.group(1).upper()
                h_short = m_axum.group(2).split("::")[-1]
                out["endpoints"].append({"protocol": "http", "method": verb, "route": route,
                                         "handler": f"{module}.{h_short}", "path": rel, "line": token.line})


def _header_prefix_before(cleaned: str, pos: int) -> str:
    last_sep = max(cleaned.rfind(";", 0, pos), cleaned.rfind("}", 0, pos), cleaned.rfind("{", 0, pos))
    return cleaned[last_sep + 1 : pos]


def _java_csharp_endpoints(
    source: str, rel: str, module: str, ext: str, declarations: list[dict], out: dict[str, list]
) -> None:
    if ext == ".java" and "Mapping" not in source:
        return
    if ext == ".cs" and "[Http" not in source:
        return
    cleaned = _strip_comments_keep_strings(source)
    declared = {d["qualified"] for d in declarations}
    if ext == ".java":
        if "Mapping" not in cleaned:
            return
        cls_re = re.compile(r"\bclass\s+([A-Za-z_]\w*)[^{;]*\{", re.M)
        m_re = re.compile(
            r"(@(?:Get|Post|Put|Patch|Delete|Request)Mapping\b(?:\s*\([^)]*\))?\s*(?:@\w+(?:\s*\([^)]*\))?\s*)*)"
            r"(?:(?:public|protected|private|static|final|synchronized|abstract|default)\s+)*"
            r"(?:<[^>{};()]*>\s*)?[A-Za-z_][\w.]*(?:\s*<[^{};()]*>)?(?:\s*\[\s*\])*\s+([A-Za-z_]\w*)\s*\(",
            re.M,
        )
        for cm in cls_re.finditer(cleaned):
            cname = cm.group(1)
            anns = _header_prefix_before(cleaned, cm.start())
            p_match = re.search(r'@RequestMapping\s*\(\s*(?:(?:value|path)\s*=\s*)?"([^"]*)"', anns)
            cls_prefix = p_match.group(1) if p_match else ""
            brace_open = cm.end() - 1
            depth, close_idx = 0, len(cleaned) - 1
            for idx in range(brace_open, len(cleaned)):
                depth += int(cleaned[idx] == "{") - int(cleaned[idx] == "}")
                if depth == 0:
                    close_idx = idx
                    break
            body = cleaned[brace_open + 1:close_idx]
            for mm in m_re.finditer(body):
                m_anns, mname = mm.group(1), mm.group(2)
                qualified = f"{module}.{cname}.{mname}"
                if qualified not in declared:
                    continue
                line = cleaned.count("\n", 0, brace_open + 1 + mm.start()) + 1
                for sm in re.finditer(r'@(Get|Post|Put|Patch|Delete)Mapping(?:\s*\(\s*(?:(?:value|path)\s*=\s*)?(?:"([^"]*)")?\s*\))?', m_anns):
                    verb = sm.group(1).upper()
                    sub = sm.group(2) or ""
                    out["endpoints"].append({"protocol": "http", "method": verb,
                                             "route": _join_route(cls_prefix, sub),
                                             "handler": qualified, "path": rel, "line": line})
                for rm in re.finditer(r'@RequestMapping\s*\(([^)]*)\)', m_anns):
                    inside = rm.group(1)
                    path_m = re.search(r'(?:(?:value|path)\s*=\s*)?"([^"]*)"', inside)
                    verb_m = re.search(r'RequestMethod\.(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)', inside)
                    if path_m:
                        verb = verb_m.group(1) if verb_m else "GET"
                        out["endpoints"].append({"protocol": "http", "method": verb,
                                                 "route": _join_route(cls_prefix, path_m.group(1)),
                                                 "handler": qualified, "path": rel, "line": line})
    elif ext == ".cs":
        if "[Http" not in cleaned:
            return
        cls_re = re.compile(r"\bclass\s+([A-Za-z_]\w*)[^{;]*\{", re.M)
        m_re = re.compile(
            r'(\[Http(?:Get|Post|Put|Patch|Delete|Head|Options)\b(?:"(?:\\.|[^"\\])*"|[^\]"])*\]\s*'
            r'(?:\[(?:"(?:\\.|[^"\\])*"|[^\]"])*\]\s*)*)'
            r'(?:(?:public|protected|internal|private|static|async|virtual|override|sealed|new|partial)\s+)*'
            r'[A-Za-z_][\w.]*(?:\s*<[^{};()]*>)?(?:\s*\[\s*\])*\??\s+([A-Za-z_]\w*)\s*\(',
            re.M,
        )
        for cm in cls_re.finditer(cleaned):
            cname = cm.group(1)
            attrs = _header_prefix_before(cleaned, cm.start())
            r_match = re.search(r'\[Route\s*\(\s*"([^"]*)"\s*\)\]', attrs)
            cls_prefix = r_match.group(1) if r_match else ""
            if "[controller]" in cls_prefix.lower():
                ctrl = cname[:-10] if cname.endswith("Controller") and len(cname) > 10 else cname
                cls_prefix = re.sub(r"\[controller\]", ctrl.lower(), cls_prefix, flags=re.I)
            brace_open = cm.end() - 1
            depth, close_idx = 0, len(cleaned) - 1
            for idx in range(brace_open, len(cleaned)):
                depth += int(cleaned[idx] == "{") - int(cleaned[idx] == "}")
                if depth == 0:
                    close_idx = idx
                    break
            body = cleaned[brace_open + 1:close_idx]
            for mm in m_re.finditer(body):
                m_attrs, mname = mm.group(1), mm.group(2)
                qualified = f"{module}.{cname}.{mname}"
                if qualified not in declared:
                    continue
                line = cleaned.count("\n", 0, brace_open + 1 + mm.start()) + 1
                for hm in re.finditer(r'\[Http(Get|Post|Put|Patch|Delete|Head|Options)(?:\s*\(\s*"([^"]*)"\s*\))?\s*\]', m_attrs):
                    verb = hm.group(1).upper()
                    sub = hm.group(2) or ""
                    out["endpoints"].append({"protocol": "http", "method": verb,
                                             "route": _join_route(cls_prefix, sub),
                                             "handler": qualified, "path": rel, "line": line})


def _rust_endpoints(source: str, rel: str, module: str, declarations: list[dict], out: dict[str, list]) -> None:
    if "#[" not in source and "impl" not in source:
        return
    cleaned = _strip_comments_keep_strings(source)
    declared = {d["qualified"] for d in declarations}
    attr_re = re.compile(
        r'#\[(get|post|put|patch|delete|head|options)\s*\(\s*"([^"]+)"\s*\)\]\s*'
        r"(?:#\[[^\];{}]*\]\s*)*(?:pub(?:\([^)]*\))?\s+)?(?:async\s+)?fn\s+([A-Za-z_]\w*)",
        re.M,
    )
    for m in attr_re.finditer(cleaned):
        verb, route, fn_name = m.group(1).upper(), m.group(2), m.group(3)
        qualified = f"{module}.{fn_name}"
        if qualified not in declared:
            continue
        line = cleaned.count("\n", 0, m.start()) + 1
        out["endpoints"].append({"protocol": "http", "method": verb, "route": route,
                                 "handler": qualified, "path": rel, "line": line})

    # Rust Tonic gRPC trait impl: `impl Catalog for CatalogService { ... }` or `impl pb::catalog_server::Catalog for ...`
    impl_re = re.compile(r"\bimpl\s+(?:[A-Za-z0-9_]+::)*([A-Za-z_]\w*)\s+for\s+([A-Za-z_]\w*)\s*\{")
    for im in impl_re.finditer(cleaned):
        service_trait, struct_name = im.group(1), im.group(2)
        for decl in declarations:
            if decl["kind"] == "method" and decl["qualified"].startswith(f"{module}.{struct_name}."):
                out["endpoints"].append({"protocol": "grpc", "service": service_trait, "method": decl["name"],
                                         "handler": decl["qualified"], "path": rel, "line": decl["line"]})


def _grpc_implementations(source: str, rel: str, declarations: list[dict], out: dict[str, list]) -> None:
    """Recognize generated Go/Java/C# service base names, not arbitrary method names."""
    tokens = _tokens(source)
    for i, token in enumerate(tokens):
        if token.value not in {"class", "type"} or token.kind != "id" or i + 1 >= len(tokens):
            continue
        name = tokens[i + 1].value
        end = next((j for j in range(i + 2, len(tokens)) if tokens[j].value == "{"), None)
        if end is None:
            continue
        header = tokens[i + 2:end]
        if token.value == "type":
            if not header or header[0].value != "struct":
                continue
            closing = next((j for j in range(end + 1, len(tokens)) if tokens[j].value == "}"), end)
            header = tokens[end + 1:closing]
        services = set()
        for part in header:
            if part.kind != "id":
                continue
            pattern = r"Unimplemented(\w+)Server$" if token.value == "type" else r"(\w+?)(?:ImplBase|Base)$"
            match = re.fullmatch(pattern, part.value)
            if match and match.group(1) not in {"Controller", "ApiController"}:
                services.add(match.group(1))
        if len(services) != 1:
            continue
        service_name = next(iter(services))
        for declaration in declarations:
            if declaration["kind"] == "method" and declaration["qualified"].rsplit(".", 2)[-2] == name:
                out["endpoints"].append({"protocol": "grpc", "service": service_name, "method": declaration["name"],
                                         "handler": declaration["qualified"], "path": rel, "line": declaration["line"]})


def _yaml_mapping(source: str) -> dict:
    """Read indentation maps/lists and JSON scalars; reject YAML execution features."""
    rows = []
    for raw in source.splitlines():
        if "\t" in raw:
            raise ValueError("unsupported YAML tabs; use OpenAPI JSON")
        clean = re.sub(r'''("(?:\\.|[^"\\])*"|'(?:''|[^'])*')|\s+#.*$''', lambda m: m.group(1) or "", raw).rstrip()
        if clean.strip() and not clean.lstrip().startswith("#") and clean.strip() not in {"---", "..."}:
            rows.append((len(clean) - len(clean.lstrip()), clean.strip()))

    def scalar(value: str) -> Any:
        if not value:
            return {}
        if value[0] in "&*!|>":
            raise ValueError("unsupported YAML syntax; use OpenAPI JSON")
        if value[0] in '[{"' or value in {"true", "false", "null"}:
            return json.loads(value)
        if value.startswith("'"):
            if not value.endswith("'"):
                raise ValueError("unterminated YAML string")
            return value[1:-1].replace("''", "'")
        return value

    def mapping_entry(text: str) -> tuple[str, str]:
        match = re.match(r'''((?:'[^']*'|"(?:\\.|[^"\\])*"|[^:'"])+):(?:\s+(.*))?$''', text)
        if not match:
            raise ValueError("unsupported YAML syntax; use OpenAPI JSON")
        return match.group(1).strip().strip("\"'"), (match.group(2) or "").strip()

    root: dict = {}
    stack: list[tuple[int, Any]] = [(-1, root)]
    for index, (indent, text) in enumerate(rows):
        while stack[-1][0] >= indent:
            stack.pop()
        parent = stack[-1][1]
        if text.startswith("- "):
            if not isinstance(parent, list):
                raise ValueError("invalid YAML sequence")
            text = text[2:].strip()
            if not re.search(r":(?:\s|$)", text):
                parent.append(scalar(text))
                continue
            entry: dict = {}
            parent.append(entry)
            stack.append((indent, entry))
            parent, indent = entry, indent + 2
        if not isinstance(parent, dict):
            raise ValueError("invalid YAML mapping")
        key, value = mapping_entry(text)
        if key in parent:
            raise ValueError("duplicate YAML key")
        next_is_list = index + 1 < len(rows) and rows[index + 1][0] > indent and rows[index + 1][1].startswith("- ")
        parent[key] = scalar(value) if value else [] if next_is_list else {}
        if not value:
            stack.append((indent, parent[key]))
    return root


def _openapi_facts(source: str, rel: str) -> dict[str, list]:
    if rel.lower().endswith(".json") and '"documents"' in source and not re.search(r'"(?:openapi|swagger)"\s*:', source):
        return extract_scip_facts(source, rel)
    out = _facts()
    if not re.search(r'["\']?(?:openapi|swagger)["\']?\s*:', source):
        return out
    try:
        data = json.loads(source) if rel.lower().endswith(".json") else _yaml_mapping(source)
        if not isinstance(data, dict) or not ({"openapi", "swagger"} & data.keys()):
            return out
        paths = data.get("paths", {})
        if not isinstance(paths, dict):
            raise ValueError("paths must be an object")

        def dereference(item: Any) -> Any:
            seen = set()
            while isinstance(item, dict) and "$ref" in item:
                reference = item["$ref"]
                if not isinstance(reference, str) or not reference.startswith("#/") or reference in seen:
                    out["diagnostics"].append({"path": rel, "reason": "unsupported_openapi_ref", "reference": reference})
                    return {}
                seen.add(reference)
                item = data
                for part in reference[2:].split("/"):
                    item = item.get(part.replace("~1", "/").replace("~0", "~")) if isinstance(item, dict) else None
                if not isinstance(item, dict):
                    out["diagnostics"].append({"path": rel, "reason": "unresolved_openapi_ref", "reference": reference})
                    return {}
            return item

        operation_ids = []
        for route, path_item in paths.items():
            path_item = dereference(path_item)
            if not isinstance(path_item, dict) or not route.startswith("/"):
                continue
            for method, operation in path_item.items():
                operation = dereference(operation)
                if method.upper() not in HTTP_METHODS or not isinstance(operation, dict):
                    continue
                servers = operation.get("servers", path_item.get("servers", data.get("servers", [])))
                bases = [s["url"] for s in servers if isinstance(s, dict) and isinstance(s.get("url"), str)] if isinstance(servers, list) else []
                if data.get("swagger") == "2.0":
                    bases = [data.get("basePath", "")]
                    if data.get("host"):
                        bases = [scheme + "://" + data["host"] + bases[0] for scheme in data.get("schemes", ["https"])]
                handler = operation.get("x-codegraph-handler", operation.get("operationId"))
                if operation.get("operationId"):
                    operation_ids.append(operation["operationId"])
                for base in bases or [""]:
                    if "{" in base:
                        out["diagnostics"].append({"path": rel, "reason": "dynamic_openapi_server"})
                        continue
                    if isinstance(handler, str) and handler:
                        out["endpoints"].append({"protocol": "http", "method": method.upper(),
                                                 "route": base.rstrip("/") + route, "handler": handler,
                                                 "path": rel, "line": 1, "contract": True})
                    else:
                        out["route_contracts"].append({"method": method.upper(), "base": base.rstrip("/"),
                                                       "route": route, "path": rel, "line": 1})
        if len(operation_ids) != len(set(operation_ids)):
            raise ValueError("duplicate OpenAPI operationId")
    except (ValueError, TypeError, AttributeError) as exc:
        out["endpoints"] = []
        out["route_contracts"] = []
        out["diagnostics"].append({"path": rel, "reason": "invalid_openapi", "detail": str(exc)})
    return out


def _normalize_proto_http_path(raw: str) -> str:
    # Expand Envoy/gRPC-Gateway `{name=users/*}` templates into `/users/{param}`.
    return re.sub(
        r"\{([A-Za-z_][\w.]*)=([^{}]+)\}",
        lambda m: "{param}"
        if m.group(2) == "*"
        else "/".join("{param}" if seg in {"*", "**"} else seg for seg in m.group(2).split("/")),
        raw,
    )


def _extract_proto_http_bindings(
    rpc_tokens: list[Token], service: str, method: str, rel: str, rpc_line: int, out: dict[str, list]
) -> None:
    joined = "".join(t.value for t in rpc_tokens)
    if "google.api.http" not in joined:
        return
    for i, tok in enumerate(rpc_tokens):
        low = tok.value.lower()
        if low in {"get", "post", "put", "patch", "delete"} and i + 2 < len(rpc_tokens):
            if rpc_tokens[i + 1].value in {":", "="} and rpc_tokens[i + 2].kind == "string":
                raw_path = _string_token(rpc_tokens[i + 2])
                if raw_path:
                    out["proto_http"].append({
                        "service": service,
                        "method": method,
                        "http_method": low.upper(),
                        "route": _normalize_proto_http_path(raw_path),
                        "path": rel,
                        "line": tok.line or rpc_line,
                    })
        elif low == "custom" and i + 1 < len(rpc_tokens):
            b_start = next((idx for idx in range(i + 1, min(i + 4, len(rpc_tokens))) if rpc_tokens[idx].value == "{"), None)
            if b_start is None:
                continue
            depth, b_end = 0, b_start
            for idx in range(b_start, len(rpc_tokens)):
                if rpc_tokens[idx].kind != "string":
                    depth += int(rpc_tokens[idx].value == "{") - int(rpc_tokens[idx].value == "}")
                    if depth == 0:
                        b_end = idx
                        break
            sub = rpc_tokens[b_start + 1:b_end]
            kind_v, path_v = None, None
            for si in range(len(sub) - 2):
                if sub[si].value == "kind" and sub[si + 1].value in {":", "="}:
                    kind_v = _string_token(sub[si + 2])
                elif sub[si].value == "path" and sub[si + 1].value in {":", "="}:
                    path_v = _string_token(sub[si + 2])
            if kind_v and path_v and kind_v.upper() in HTTP_METHODS:
                out["proto_http"].append({
                    "service": service,
                    "method": method,
                    "http_method": kind_v.upper(),
                    "route": _normalize_proto_http_path(path_v),
                    "path": rel,
                    "line": tok.line or rpc_line,
                })


def _proto_facts(source: str, rel: str) -> dict[str, list]:
    out = _facts()
    tokens = _tokens(source)
    values = [t.value for t in tokens]
    package = ""
    for i, token in enumerate(tokens):
        if token.kind == "id" and token.value == "package":
            end = next((k for k in range(i + 1, len(tokens)) if values[k] == ";"), i + 1)
            package = "".join(values[i + 1:end])
    for i in range(len(tokens) - 2):
        if values[i] != "service" or tokens[i].kind != "id" or values[i + 2] != "{":
            continue
        service = (package + "." + values[i + 1]).strip(".")
        depth = 1
        k = i + 3
        while k < len(tokens) - 2 and depth > 0:
            if tokens[k].kind == "string":
                k += 1
                continue
            if values[k] == "{":
                depth += 1
            elif values[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            elif depth == 1 and values[k] == "rpc" and values[k + 2] == "(":
                rpc_method = values[k + 1]
                rpc_line = tokens[k].line
                out["rpcs"].append({"service": service, "method": rpc_method, "path": rel, "line": rpc_line})
                # Scan ahead to either `;` or `{ ... }` block of this rpc
                scan = k + 3
                while scan < len(tokens) and values[scan] not in {";", "{"}:
                    scan += 1
                if scan < len(tokens) and values[scan] == "{":
                    r_depth, r_end = 0, scan
                    for r_idx in range(scan, len(tokens)):
                        if tokens[r_idx].kind != "string":
                            r_depth += int(values[r_idx] == "{") - int(values[r_idx] == "}")
                            if r_depth == 0:
                                r_end = r_idx
                                break
                    _extract_proto_http_bindings(tokens[scan + 1:r_end], service, rpc_method, rel, rpc_line, out)
                    k = r_end + 1
                    continue
            k += 1
    return out


def extract_facts(source: str | bytes, rel: str, module: str, ext: str, declarations: list[dict]) -> dict[str, list]:
    if ext == ".scip" or isinstance(source, bytes):
        return extract_scip_facts(source, rel)
    if ext == ".proto":
        return _proto_facts(source, rel)
    if ext in {".json", ".yaml", ".yml"}:
        return _openapi_facts(source, rel)
    if ext in {".py", ".pyi"}:
        return _python_facts(source, rel, module)
    out = _lexical_facts(source, rel, module, ext, declarations)
    if ext in {".java", ".cs"}:
        _java_csharp_endpoints(source, rel, module, ext, declarations, out)
    if ext == ".rs":
        _rust_endpoints(source, rel, module, declarations, out)
    if ext in {".go", ".java", ".cs"}:
        _grpc_implementations(source, rel, declarations, out)
    return out


def _url(value: str) -> tuple[str, str] | None:
    if not value.startswith(("/", "http://", "https://")):
        return None
    try:
        parsed = urlsplit(value)
    except ValueError:
        return None
    if parsed.username or parsed.password:
        return None
    path = re.sub(r"<(?:(?:[^:<>]+):)?[^<>]+>", "{param}", parsed.path or "/")
    path = re.sub(r"(?<=/):[A-Za-z_]\w*", "{param}", path)
    if any("{" in part and not re.fullmatch(r"\{[^/{}]+\}", part) for part in path.split("/")):
        return None
    authority = parsed.scheme.lower() + "://" + parsed.netloc.lower() if parsed.netloc and parsed.scheme else parsed.netloc.lower()
    return authority, path


def _route_matches(request: str, route: str) -> bool:
    left, right = request.split("/"), route.split("/")
    return len(left) == len(right) and all(
        a == b or bool(a) and bool(b) and (a.startswith("{") or b.startswith("{"))
        for a, b in zip(left, right)
    )


def _rpc_method_matches(canonical: str, value: str) -> bool:
    if value in {canonical, canonical[:1].lower() + canonical[1:]}:
        return True
    return value.islower() and _snake(value) == _snake(canonical)


def _service_matches(proto_service: str, candidate: str) -> bool:
    return proto_service == candidate or proto_service.rsplit(".", 1)[-1] == candidate


def _resolve_handler_candidates(item: dict[str, Any], symbols: dict[str, dict[str, Any]]) -> list[str]:
    handler = item["handler"]
    if handler in symbols:
        return [handler]
    if item.get("from_scip") and item.get("scip_handler") in symbols:
        return [item["scip_handler"]]
    if item.get("contract"):
        exact = [s for s in symbols if s.endswith("." + handler) and symbols[s]["kind"] in {"function", "method"}]
        if exact:
            return exact
        tail = handler.replace("#", ".").replace("/", ".").rsplit(".", 1)[-1]
        norm = _snake(tail)
        return [s for s in symbols if symbols[s]["kind"] in {"function", "method"} and _snake(symbols[s]["name"]) == norm]
    # Support Go/struct receiver method registration like `r.GET("/users/:id", h.GetUser)` within the same file
    parts = handler.split(".")
    if len(parts) >= 3:
        method_short = parts[-1]
        same_file = [
            s for s, info in symbols.items()
            if info["path"] == item["path"] and info["kind"] == "method" and info["name"] == method_short
        ]
        if same_file:
            return same_file
    return []


def _stitch_proto_http_endpoints(
    proto_http: list[dict[str, Any]],
    grpc_endpoints: list[dict[str, Any]],
    endpoints_out: list[dict[str, Any]],
    diagnostics: list[dict[str, Any]],
) -> None:
    for binding in proto_http:
        matched_handlers = sorted({
            ep["handler"]
            for ep in grpc_endpoints
            if _service_matches(binding["service"], ep["service"])
            and _rpc_method_matches(binding["method"], ep["method"])
        })
        if len(matched_handlers) != 1:
            diagnostics.append({
                "path": binding["path"],
                "line": binding["line"],
                "reason": "ambiguous_boundary_handler" if matched_handlers else "unresolved_boundary_handler",
                "handler": f"{binding['service']}.{binding['method']}",
                "candidates": matched_handlers,
            })
            continue
        endpoints_out.append({
            "protocol": "http",
            "method": binding["http_method"],
            "route": binding["route"],
            "handler": matched_handlers[0],
            "path": binding["path"],
            "line": binding["line"],
            "contract": True,
        })


def _stitch_route_contracts(
    route_contracts: list[dict[str, Any]],
    resolved_endpoints: list[dict[str, Any]],
) -> None:
    source_http = [ep for ep in resolved_endpoints if ep["protocol"] == "http" and not ep.get("contract") and not ep["authority"]]
    for rc in route_contracts:
        rel_addr = _url(rc["route"])
        full_addr = _url(rc["base"] + rc["route"])
        if rel_addr is None or full_addr is None:
            continue
        _, rel_path = rel_addr
        full_auth, full_path = full_addr
        matched = [
            ep for ep in source_http
            if ep["method"] == rc["method"]
            and (_route_matches(ep["route_path"], rel_path) or _route_matches(ep["route_path"], full_path))
        ]
        handlers = sorted({ep["handler"] for ep in matched})
        if len(handlers) == 1:
            canonical = re.sub(r"\{[^{}]+\}", "{}", full_path)
            resolved_endpoints.append({
                "protocol": "http",
                "method": rc["method"],
                "route": rc["base"] + rc["route"],
                "authority": full_auth,
                "route_path": full_path,
                "handler": handlers[0],
                "path": rc["path"],
                "line": rc["line"],
                "contract": True,
                "contract_id": f"http:{rc['method']}:{full_auth}{canonical}",
                "scip_symbol": http_scip_symbol(rc["method"], full_auth, canonical),
            })


def link_boundaries(payloads: list[dict], symbols: dict[str, dict[str, Any]]) -> tuple[list[dict], dict[str, Any], list[dict]]:
    """Resolve unique cross-language protocol identities and return (edges, summary, resolved_endpoints)."""
    endpoints, requests, rpcs, proto_http, route_contracts, diagnostics, mounts = [], [], [], [], [], [], []
    roots: set[str] = set()
    for payload in payloads:
        facts = payload.get("boundary", {})
        endpoints.extend(facts.get("endpoints", []))
        requests.extend(facts.get("requests", []))
        rpcs.extend(facts.get("rpcs", []))
        proto_http.extend(facts.get("proto_http", []))
        route_contracts.extend(facts.get("route_contracts", []))
        diagnostics.extend(facts.get("diagnostics", []))
        mounts.extend(facts.get("mounts", []))
        roots.update(facts.get("roots", []))

    def prefixes(router: str, visited: set[str]) -> list[str]:
        if router in visited:
            return []
        result = [""] if router in roots else []
        for mount in mounts:
            if mount["child"] == router:
                result.extend(_join_route(p, mount["prefix"]) if p or mount["prefix"] else ""
                              for p in prefixes(mount["parent"], visited | {router}))
        return result

    expanded_endpoints: list[dict[str, Any]] = []
    for endpoint in endpoints:
        if "router" not in endpoint:
            expanded_endpoints.append(endpoint)
            continue
        paths = prefixes(endpoint["router"], set())
        if not paths:
            diagnostics.append({"path": endpoint["path"], "line": endpoint["line"], "reason": "unmounted_http_router"})
        for prefix in paths:
            joined = _join_route(prefix, endpoint["route"]) if prefix else endpoint["route"]
            expanded_endpoints.append(dict(endpoint, route=joined))

    resolved_endpoints: list[dict[str, Any]] = []
    for endpoint in expanded_endpoints:
        item = dict(endpoint)
        candidates = _resolve_handler_candidates(item, symbols)
        if len(candidates) != 1:
            diagnostics.append({
                "path": item["path"],
                "line": item["line"],
                "reason": "ambiguous_boundary_handler" if candidates else "unresolved_boundary_handler",
                "handler": item["handler"],
                "candidates": sorted(candidates),
            })
            continue
        item["handler"] = candidates[0]
        if item["protocol"] == "http":
            address = _url(item["route"])
            if address is None:
                diagnostics.append({"path": item["path"], "line": item["line"], "reason": "dynamic_http_route"})
                continue
            item["authority"], item["route_path"] = address
            canonical = re.sub(r"\{[^{}]+\}", "{}", item["route_path"])
            item["contract_id"] = f"http:{item['method']}:{item['authority']}{canonical}"
            item["scip_symbol"] = http_scip_symbol(item["method"], item["authority"], canonical)
        else:
            matched_rpcs = [
                r for r in rpcs
                if _service_matches(r["service"], item["service"]) and _rpc_method_matches(r["method"], item["method"])
            ]
            svc = matched_rpcs[0]["service"] if len(matched_rpcs) == 1 else item["service"]
            mtd = matched_rpcs[0]["method"] if len(matched_rpcs) == 1 else item["method"]
            item["contract_id"] = f"grpc:/{svc}/{mtd}"
            item["scip_symbol"] = grpc_scip_symbol(svc, mtd)
        resolved_endpoints.append(item)

    if proto_http:
        grpc_eps = [ep for ep in resolved_endpoints if ep["protocol"] == "grpc"]
        bridged_http: list[dict[str, Any]] = []
        _stitch_proto_http_endpoints(proto_http, grpc_eps, bridged_http, diagnostics)
        for ep in bridged_http:
            addr = _url(ep["route"])
            if addr is None:
                diagnostics.append({"path": ep["path"], "line": ep["line"], "reason": "dynamic_http_route"})
                continue
            ep["authority"], ep["route_path"] = addr
            canonical = re.sub(r"\{[^{}]+\}", "{}", ep["route_path"])
            ep["contract_id"] = f"http:{ep['method']}:{ep['authority']}{canonical}"
            ep["scip_symbol"] = http_scip_symbol(ep["method"], ep["authority"], canonical)
            resolved_endpoints.append(ep)

    if route_contracts:
        _stitch_route_contracts(route_contracts, resolved_endpoints)

    edges: list[dict[str, Any]] = []
    resolved_count = 0
    for request in requests:
        caller = request["caller"]
        if caller not in symbols and request.get("scip_caller") in symbols:
            caller = request["scip_caller"]
        if caller not in symbols:
            continue
        matches = []
        identities: set[str] = set()
        for endpoint in resolved_endpoints:
            if endpoint["protocol"] != request["protocol"]:
                continue
            if request["protocol"] == "http":
                address = _url(request["route"])
                if address is None or endpoint["method"] != request["method"]:
                    continue
                authority, path = address
                if authority and authority != endpoint["authority"]:
                    continue
                if not _route_matches(path, endpoint["route_path"]):
                    continue
                canonical_path = re.sub(r"\{[^{}]+\}", "{}", endpoint["route_path"])
                identity = f"http:{endpoint['method']}:{endpoint['authority']}{canonical_path}"
                scip_id = http_scip_symbol(endpoint["method"], endpoint["authority"], canonical_path)
                evidence_items = [endpoint]
            else:
                contracts = [
                    rpc for rpc in rpcs
                    if _rpc_method_matches(rpc["method"], request["method"])
                    and _rpc_method_matches(rpc["method"], endpoint["method"])
                    and _service_matches(rpc["service"], request["service"])
                    and _service_matches(rpc["service"], endpoint["service"])
                ]
                if not contracts:
                    continue
                for rpc in contracts:
                    identities.add(f"grpc:/{rpc['service']}/{rpc['method']}")
                identity = f"grpc:/{contracts[0]['service']}/{contracts[0]['method']}"
                scip_id = grpc_scip_symbol(contracts[0]["service"], contracts[0]["method"])
                evidence_items = [endpoint, *contracts]
            matches.append((endpoint, identity, scip_id, evidence_items))

        if request["protocol"] == "http" and matches and "{" not in request["route"]:
            maximum = max(sum(not p.startswith("{") for p in e[0]["route_path"].split("/")) for e in matches)
            matches = [e for e in matches if sum(not p.startswith("{") for p in e[0]["route_path"].split("/")) == maximum]

        candidates = sorted({item[0]["handler"] for item in matches})
        reason = "ambiguous_boundary_request" if len(candidates) > 1 or len(identities) > 1 else "unresolved_boundary_request"
        if len(candidates) != 1 or len(identities) > 1:
            diagnostics.append({
                "path": request["path"],
                "line": request["line"],
                "reason": reason,
                "protocol": request["protocol"],
                "caller": caller,
                "candidates": candidates,
            })
            continue

        resolved_count += 1
        target_sym = candidates[0]
        target_info = symbols[target_sym]
        evidence = [{"path": request["path"], "line": request["line"], "role": "request"}]
        if not target_info.get("external"):
            evidence.append({"path": target_info["path"], "line": target_info["line"], "role": "handler"})
        for _, _, _, entries in matches:
            for entry in entries:
                role = "contract" if entry.get("contract") or entry in rpcs else "handler"
                location = {"path": entry["path"], "line": entry["line"], "role": role}
                if location not in evidence:
                    evidence.append(location)
        chosen = sorted((item[1], item[2], item[0].get("scip_target", "")) for item in matches)[0]
        edge_record: dict[str, Any] = {
            "type": "calls",
            "from": caller,
            "to": target_sym,
            "boundary": request["protocol"],
            "contract_id": chosen[0],
            "scip_symbol": chosen[1],
            "resolution": "static_unique",
            "evidence": evidence,
        }
        if target_info.get("external") or symbols[caller].get("external"):
            edge_record["cross_repo"] = True
        if chosen[2] or target_info.get("scip_symbol"):
            edge_record["scip_target"] = chosen[2] or target_info.get("scip_symbol")
        edges.append(edge_record)

    scip_protocols = sorted({ep["scip_symbol"] for ep in resolved_endpoints if ep.get("scip_symbol")})
    summary = {
        "requests": len(requests),
        "resolved": resolved_count,
        "endpoints": len(resolved_endpoints),
        "scip_protocols": len(scip_protocols),
        "diagnostics": diagnostics,
        "complete": not diagnostics,
    }
    return edges, summary, resolved_endpoints


def match_protocol_query(query: str, resolved_endpoints: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Match an HTTP path (`GET /users/7` or `/users/7`), gRPC path, contract_id, or SCIP symbol to endpoints."""
    raw = query.strip()
    if not raw:
        return []
    # 1. Exact contract_id or scip_symbol match
    exact = [ep for ep in resolved_endpoints if raw in {ep.get("contract_id"), ep.get("scip_symbol")}]
    if exact:
        return exact
    # 2. SCIP protocol symbol parse
    parsed_scip = parse_protocol_scip_symbol(raw)
    if parsed_scip:
        if parsed_scip["protocol"] == "http":
            raw = f"{parsed_scip['method']} {parsed_scip['route']}"
        else:
            raw = f"grpc:/{parsed_scip['service']}/{parsed_scip['method']}"
    # 3. gRPC path `/pkg.Service/Method` or `grpc:/pkg.Service/Method`
    grpc_m = re.fullmatch(r"(?:grpc:)?/([\w.]+)/(\w+)", raw)
    if grpc_m:
        svc, mtd = grpc_m.group(1), grpc_m.group(2)
        return [
            ep for ep in resolved_endpoints
            if ep["protocol"] == "grpc"
            and (_service_matches(svc, ep["service"]) or _service_matches(ep["service"], svc))
            and (_rpc_method_matches(mtd, ep["method"]) or _rpc_method_matches(ep["method"], mtd))
        ]
    # 4. HTTP `[METHOD] /path` or `[METHOD] https://host/path`
    verb: str | None = None
    route_part = raw
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].upper() in HTTP_METHODS:
        verb, route_part = parts[0].upper(), parts[1].strip()
    if route_part.startswith(("/", "http://", "https://")):
        norm_query = re.sub(r"\{\}", "{param}", route_part)
        addr = _url(norm_query)
        if addr is None:
            return []
        q_auth, q_path = addr
        matched = [
            ep for ep in resolved_endpoints
            if ep["protocol"] == "http"
            and (verb is None or ep["method"] == verb)
            and (not q_auth or q_auth == ep["authority"])
            and _route_matches(q_path, ep["route_path"])
        ]
        if matched and "{" not in norm_query:
            best = max(sum(not p.startswith("{") for p in ep["route_path"].split("/")) for ep in matched)
            matched = [ep for ep in matched if sum(not p.startswith("{") for p in ep["route_path"].split("/")) == best]
        return matched
    return []
