"""Cross-language protocol links, false-positive guards and cache lifecycle."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import textwrap
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "mcp"))
import code_intelligence as ci


class CrossBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.root = self.base / "repo"
        self.root.mkdir()
        self.cache = self.base / "state/cache.sqlite3"
        environment = patch.dict(os.environ, {
            "CODE_INTELLIGENCE_CACHE_PATH": str(self.cache),
            "CODE_INTELLIGENCE_BASELINE_PATH": str(self.base / "state/baseline.json"),
            "CODE_INTELLIGENCE_DISABLE_CACHE": "0",
            "CODE_INTELLIGENCE_MAX_FILES": "350",
            "CODE_INTELLIGENCE_PROVIDER": "polyglot",
        })
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, path, source):
        target = self.root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source).lstrip("\n"), encoding="utf-8")
        return target

    def graph(self):
        return ci._CodeGraph(self.root)

    def edges(self, graph=None):
        return [e for e in (graph or self.graph()).edges if "boundary" in e]

    def backend(self):
        self.write("api.py", """
            from fastapi import FastAPI
            app = FastAPI()
            def lookup(): return 42
            @app.get('/users/{user_id}')
            async def get_user(user_id): return lookup()
            @app.post('/users/{user_id}')
            def update_user(user_id): return lookup()
        """)

    def openapi(self, handler="api.get_user", server="", route="/users/{id}"):
        data = {"openapi": "3.0.4", "paths": {route: {"get": {"operationId": handler}}}}
        if server:
            data["servers"] = [{"url": server}]
        self.write("openapi.json", json.dumps(data))

    def grpc_fixture(self):
        self.write("catalog.proto", """
            syntax = "proto3";
            package shop.v1;
            service Catalog {
              rpc Get (GetRequest) returns (GetReply);
              rpc Watch (stream GetRequest) returns (stream GetReply) {}
            }
        """)
        self.write("server.py", """
            import catalog_pb2_grpc
            class CatalogService(catalog_pb2_grpc.CatalogServicer):
                def Get(self, request, context): return 1
                def Watch(self, request, context): return []
        """)

    def test_fetch_to_python_handler_and_evidence(self):
        self.backend()
        self.write("web.ts", """
            export async function load(id: string) {
              return fetch(`/users/${id}?format=json#view`);
            }
        """)
        result = ci.symbol_trace(str(self.root), "web.load", "callees")
        self.assertIn("api.get_user", result["callees"])
        self.assertNotIn("api.update_user", result["callees"])
        edge = next(e for e in result["edges"] if "boundary" in e)
        self.assertEqual(edge["boundary"], "http")
        self.assertEqual(edge["contract_id"], "http:GET:/users/{}")
        self.assertEqual(edge["evidence"][0], {"path": "web.ts", "line": 2, "role": "request"})
        self.assertTrue(any(e["role"] == "handler" and e["path"] == "api.py" for e in edge["evidence"]))
        self.assertEqual(result["analysis"]["cross_boundary"]["resolved"], 1)

    def test_http_method_and_fetch_options(self):
        self.backend()
        self.write("web.js", "function save() { return fetch('/users/7', {method: 'POST'}); }\n")
        self.assertEqual([e["to"] for e in self.edges()], ["api.update_user"])

    def test_python_http_alias_session_and_keyword_url(self):
        self.backend()
        self.write("client.py", """
            import httpx as h
            client = h.Client()
            def load(): return client.request(method='GET', url='/users/7')
        """)
        self.assertEqual([e["to"] for e in self.edges()], ["api.get_user"])

    def test_axios_template_and_expression_arrow(self):
        self.backend()
        self.write("web.ts", "export const load = (id: string) => axios.get(`/users/${id}`);\n")
        self.assertEqual([(e["from"], e["to"]) for e in self.edges()], [("web.load", "api.get_user")])

    def test_transitive_impact_explore_rank_and_architecture(self):
        self.backend()
        self.write("web.ts", "function load() { return fetch('/users/7'); }\n")
        self.write("web.test.ts", "function test_load() { return load(); }\n")
        graph = self.graph()
        impact = graph.impact(symbols=["api.lookup"])
        self.assertIn("web.load", impact["affected_symbols"])
        self.assertIn("web.test.ts", impact["related_tests"])
        self.assertTrue(any(e.get("boundary") == "http" for e in graph.explore("web.load")["relations"]))
        self.assertIn({"from": "web.ts", "to": "api.py", "type": "calls"}, graph.architecture()["edges"])
        self.assertIn("api.get_user", graph._rank_network()["web.load"])
        callers = ci.symbol_trace(str(self.root), "api.get_user", "callers")
        self.assertIn("web.load", callers["callers"])
        self.assertTrue(all(e["to"] == "api.get_user" for e in callers["edges"]))

    def test_cross_language_go_http_handler(self):
        self.write("server.go", """
            package api
            func serve() { http.HandleFunc("GET /users/{id}", getUser) }
            func getUser(w http.ResponseWriter, r *http.Request) { lookup() }
            func lookup() {}
        """)
        self.write("web.ts", "function load() { return fetch('/users/7'); }\n")
        self.assertEqual([e["to"] for e in self.edges()], ["server.getUser"])

    def test_express_named_and_inline_handlers(self):
        self.write("api.js", """
            function named(req, res) { return 1; }
            app.get('/named', named);
            app.post('/inline', (req, res) => { return 2; });
        """)
        self.write("client.py", """
            import requests
            def load(): return requests.get('/named')
            def save(): return requests.post('/inline')
        """)
        edges = self.edges()
        self.assertEqual(len(edges), 2)
        self.assertIn("api.named", [e["to"] for e in edges])
        self.assertTrue(any(e["to"].startswith("api.__http_post_") for e in edges))

    def test_mounted_fastapi_prefix(self):
        self.write("api.py", """
            from fastapi import FastAPI, APIRouter
            app = FastAPI()
            router = APIRouter(prefix='/users')
            @router.get('/{id}')
            def get_user(id): return id
            app.include_router(router, prefix='/v1')
        """)
        self.write("web.ts", "function load() { return fetch('/v1/users/7'); }\n")
        self.assertEqual([e["to"] for e in self.edges()], ["api.get_user"])

    def test_cross_file_fastapi_router(self):
        self.write("routes.py", """
            from fastapi import APIRouter
            router = APIRouter(prefix='/users')
            @router.get('/{id}')
            def get_user(id): return id
        """)
        self.write("app.py", """
            from fastapi import FastAPI
            from routes import router as users
            app = FastAPI()
            app.include_router(users, prefix='/v1')
        """)
        self.write("web.ts", "function load() { return fetch('/v1/users/7'); }\n")
        self.assertEqual([e["to"] for e in self.edges()], ["routes.get_user"])

    def test_openapi_json_origin_and_base_path(self):
        self.backend()
        self.openapi(server="https://api.example/v1")
        self.write("web.ts", "function load() { return fetch('https://api.example/v1/users/7'); }\n")
        edge = self.edges()[0]
        self.assertEqual(edge["to"], "api.get_user")
        self.assertTrue(any(e["path"] == "openapi.json" and e["role"] == "contract" for e in edge["evidence"]))

    def test_openapi_yaml_servers_and_operation_id(self):
        self.write("api.py", "def get_user(): return 1\n")
        self.write("api.yaml", """
            openapi: 3.0.4
            servers:
              - url: https://api.example/v1
            paths:
              /users/{id}:
                get:
                  operationId: get_user
        """)
        self.write("web.ts", "function load() { return fetch('https://api.example/v1/users/7'); }\n")
        self.assertEqual([e["to"] for e in self.edges()], ["api.get_user"])

    def test_explicit_handler_mapping(self):
        self.write("api.py", "def implementation(): return 1\n")
        self.write("openapi.json", json.dumps({"openapi": "3.1.0", "paths": {"/users": {"get": {
            "operationId": "generatedOperation", "x-codegraph-handler": "api.implementation"}}}}))
        self.write("web.ts", "function load() { return fetch('/users'); }\n")
        self.assertEqual([e["to"] for e in self.edges()], ["api.implementation"])

    def test_origin_mismatch_and_absolute_url_without_host_evidence(self):
        self.backend()
        self.openapi(server="https://one.example")
        self.write("web.ts", """
            function wrong() { return fetch('https://two.example/users/7'); }
            function unknown() { return fetch('https://unknown.example/users/7'); }
        """)
        self.assertEqual(self.edges(), [])

    def test_duplicate_routes_are_ambiguous(self):
        self.backend()
        self.write("other.py", """
            from flask import Flask
            app = Flask(__name__)
            @app.get('/users/<int:id>')
            def another(id): return id
        """)
        self.write("web.ts", "function load() { return fetch('/users/7'); }\n")
        graph = self.graph()
        self.assertEqual(self.edges(graph), [])
        diag = graph.boundary_analysis["diagnostics"]
        self.assertTrue(any(d["reason"] == "ambiguous_boundary_request" for d in diag))

    def test_concrete_route_precedence_and_dynamic_ambiguity(self):
        self.backend()
        self.write("fixed.py", """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get('/users/me')
            def current(): return 1
        """)
        self.write("web.ts", """
            function concrete() { return fetch('/users/me'); }
            function dynamic(id) { return fetch(`/users/${id}`); }
        """)
        self.assertEqual([(e["from"], e["to"]) for e in self.edges()], [("web.concrete", "fixed.current")])

    def test_dynamic_url_and_method_never_guessed(self):
        self.backend()
        self.write("web.ts", """
            function dynamic(url) { return fetch(url); }
            function method(verb) { return fetch('/users/7', {method: verb}); }
            function partial(id) { return fetch(`/users/user-${id}`); }
        """)
        graph = self.graph()
        self.assertEqual(self.edges(graph), [])
        self.assertFalse(graph.boundary_analysis["complete"])

    def test_comments_and_strings_never_create_protocol_facts(self):
        self.backend()
        self.write("web.ts", """
            // function ghost() { fetch('/users/7'); }
            const text = "fetch('/users/7')";
            function safe() { return "axios.get('/users/7')"; }
        """)
        self.assertEqual(self.edges(), [])

    def test_openapi_duplicate_operation_id_never_picks_first_handler(self):
        self.write("one.py", "def get_user(): return 1\n")
        self.write("two.py", "def get_user(): return 2\n")
        self.openapi(handler="get_user")
        self.write("web.ts", "function load() { return fetch('/users/7'); }\n")
        graph = self.graph()
        self.assertEqual(self.edges(graph), [])
        self.assertTrue(any(d["reason"] == "ambiguous_boundary_handler" for d in graph.boundary_analysis["diagnostics"]))

    def test_proto_ts_client_to_python_servicer(self):
        self.grpc_fixture()
        self.write("web.ts", """
            const client = new CatalogClient('https://api.example');
            function load() { return client.Get({}); }
        """)
        edge = self.edges()[0]
        self.assertEqual(edge["to"], "server.CatalogService.Get")
        self.assertEqual(edge["contract_id"], "grpc:/shop.v1.Catalog/Get")
        self.assertTrue(any(e["path"] == "catalog.proto" for e in edge["evidence"]))

    def test_python_grpc_stub_and_streaming_method(self):
        self.grpc_fixture()
        self.write("client.py", """
            import catalog_pb2_grpc as rpc
            stub = rpc.CatalogStub(channel)
            def watch(): return stub.Watch(request)
        """)
        self.assertEqual([e["to"] for e in self.edges()], ["server.CatalogService.Watch"])

    def test_grpc_same_method_different_service_not_linked(self):
        self.grpc_fixture()
        self.write("other.proto", "package shop.v1; service Other { rpc Get (A) returns (B); }\n")
        self.write("client.py", """
            import other_pb2_grpc
            stub = other_pb2_grpc.OtherStub(channel)
            def load(): return stub.Get(request)
        """)
        graph = self.graph()
        self.assertEqual(self.edges(graph), [])
        self.assertNotIn("server.CatalogService.Get", graph.trace("client.load")["callees"])

    def test_grpc_package_collision_is_ambiguous(self):
        self.grpc_fixture()
        self.write("other.proto", "package other.v1; service Catalog { rpc Get (A) returns (B); }\n")
        self.write("client.py", """
            import catalog_pb2_grpc
            stub = catalog_pb2_grpc.CatalogStub(channel)
            def load(): return stub.Get(request)
        """)
        self.assertEqual(self.edges(), [])

    def test_grpc_contract_required_and_comment_ignored(self):
        self.grpc_fixture()
        self.write("catalog.proto", '// service Catalog { rpc Get (A) returns (B); }\n')
        self.write("client.py", """
            import catalog_pb2_grpc
            stub = catalog_pb2_grpc.CatalogStub(channel)
            def load(): return stub.Get(request)
        """)
        self.assertEqual(self.edges(), [])

    def test_contract_cache_hit_modify_and_delete_relinks(self):
        self.write("api.py", "def first(): pass\ndef second(): pass\n")
        self.openapi(handler="api.first")
        self.write("web.ts", "function load() { return fetch('/users/7'); }\n")
        self.assertEqual(self.edges()[0]["to"], "api.first")
        hit = self.graph()
        self.assertEqual(hit.cache_stats["re_parsed"], 0)
        self.assertEqual(self.edges(hit)[0]["to"], "api.first")
        self.openapi(handler="api.second")
        changed = self.graph()
        self.assertEqual(changed.cache_stats["re_parsed"], 1)
        self.assertEqual(self.edges(changed)[0]["to"], "api.second")
        (self.root / "openapi.json").unlink()
        self.assertEqual(self.edges(), [])
        from contextlib import closing, nullcontext
        with closing(sqlite3.connect(self.cache)) as connection:
            self.assertNotIn(("openapi.json",), connection.execute("SELECT rel_path FROM file_ast_cache").fetchall())

    def test_contract_only_cache_invalidation_never_executes_handlers(self):
        marker = self.base / "danger"
        self.write("api.py", f"from pathlib import Path\nPath({str(marker)!r}).touch()\ndef get_user(): pass\n")
        self.openapi()
        self.write("web.ts", "function load() { return fetch('/users/7'); }\n")
        self.assertEqual(len(self.edges()), 1)
        self.assertFalse(marker.exists())

    def test_contract_links_and_dependency_directories_are_pruned(self):
        from contextlib import nullcontext
        outside = self.base / "outside.proto"
        outside.write_text("service Hidden { rpc Get (A) returns (B); }", encoding="utf-8")
        hidden = self.root / "hidden.proto"
        try:
            hidden.symlink_to(outside)
            mocked = nullcontext()
        except OSError:
            hidden.write_text("service Hidden { rpc Get (A) returns (B); }", encoding="utf-8")
            orig_is_symlink = Path.is_symlink
            mocked = patch.object(Path, "is_symlink", lambda p: p == hidden or orig_is_symlink(p))
        self.write("node_modules/openapi.json", '{"openapi":"3.0.4", "paths":{}}')
        with mocked:
            self.assertEqual(self.graph().files, {})

    def test_malformed_yaml_and_external_ref_are_diagnostic(self):
        self.write("invalid.yaml", "openapi: 3.0.4\npaths: !unsafe object\n")
        self.write("ref.json", json.dumps({"openapi": "3.0.4", "paths": {"/users": {"$ref": "https://example/spec"}}}))
        graph = self.graph()
        reasons = {d["reason"] for d in graph.boundary_analysis["diagnostics"]}
        self.assertIn("invalid_openapi", reasons)
        self.assertIn("unsupported_openapi_ref", reasons)
        self.assertEqual(self.edges(graph), [])

    def test_legacy_python_provider_has_no_boundary_edges(self):
        self.backend()
        self.write("client.py", "import requests\ndef load(): return requests.get('/users/7')\n")
        self.assertFalse(any("boundary" in e for e in ci._PythonGraph(self.root).edges))

    def test_axios_instance_base_url_alias_and_generic(self):
        self.backend()
        self.openapi(server="https://api.example/v1")
        self.write("web.ts", """
            import http from 'axios';
            const api = axios.create({baseURL: 'https://api.example/v1'});
            function load() { return api.get<User>('/users/7'); }
            function relative() { return http.get('/users/7'); }
        """)
        self.assertEqual(len(self.edges()), 2)

    def test_fetch_options_spread_and_nested_method(self):
        self.backend()
        self.write("web.ts", """
            function unknown(opts) { return fetch('/users/7', {method: 'GET', ...opts}); }
            function headers() { return fetch('/users/7', {headers: {method: 'POST'}}); }
        """)
        self.assertEqual([(e["from"], e["to"]) for e in self.edges()], [("web.headers", "api.get_user")])

    def test_parameter_shadowing_clients_does_not_create_boundary_edges(self):
        self.backend()
        self.write("web.ts", "function load(fetch) { return fetch('/users/7'); }\n")
        self.write("client.py", """
            import requests
            client = requests.Session()
            def load(client): return client.get('/users/7')
            def shadow(requests): return requests.get('/users/7')
        """)
        self.assertEqual(self.edges(), [])

    def test_multiple_functions_same_line_have_exact_request_owner(self):
        self.backend()
        self.write("web.ts", "function first() { return 1; } function load() { return fetch('/users/7'); }\n")
        self.assertEqual([e["from"] for e in self.edges()], ["web.load"])

    def test_openapi_local_pointer_and_invalid_url(self):
        self.write("api.py", "def get_user(): return 1\n")
        self.write("openapi.json", json.dumps({"openapi": "3.0.4", "paths": {"/users": {
            "$ref": "#/components/pathItems/Users"}}, "components": {"pathItems": {"Users": {
                "get": {"operationId": "api.get_user"}}}}}))
        self.write("web.ts", """
            function load() { return fetch('/users'); }
            function invalid() { return fetch('http://[bad/users'); }
        """)
        self.assertEqual([e["from"] for e in self.edges()], ["web.load"])

    def test_grpc_go_handler_and_lower_camel_client(self):
        self.write("catalog.proto", "package shop; service Catalog { rpc GetUser (A) returns (B); }\n")
        self.write("server.go", """
            package api
            type Service struct { pb.UnimplementedCatalogServer }
            func (s *Service) GetUser(ctx context.Context, req *pb.A) (*pb.B, error) { return nil, nil }
        """)
        self.write("web.ts", """
            const client = new CatalogClient('https://api.example');
            function load() { return client.getUser({}); }
        """)
        self.assertEqual([e["to"] for e in self.edges()], ["server.Service.GetUser"])

    def test_grpc_java_impl_base(self):
        self.write("catalog.proto", "package shop; service Catalog { rpc GetUser (A) returns (B); }\n")
        self.write("Server.java", """
            class Service extends CatalogGrpc.CatalogImplBase {
                public void getUser(Request req, Observer response) { response.onCompleted(); }
            }
        """)
        self.write("client.py", """
            import catalog_pb2_grpc
            stub = catalog_pb2_grpc.CatalogStub(channel)
            def load(): return stub.GetUser(request)
        """)
        self.assertEqual([e["to"] for e in self.edges()], ["Server.Service.getUser"])

    def test_full_grpc_path_selects_package(self):
        self.grpc_fixture()
        self.write("other.proto", "package other.v1; service Catalog { rpc Get (A) returns (B); }\n")
        self.write("client.py", """
            get = channel.unary_unary('/shop.v1.Catalog/Get')
            def load(): return get(request)
        """)
        self.assertEqual(self.edges()[0]["contract_id"], "grpc:/shop.v1.Catalog/Get")

    def test_scip_binary_cross_repo_http_and_impact_stitching(self):
        backend = self.base / "user_service_repo"
        backend.mkdir()
        (backend / "api.py").write_text(textwrap.dedent("""
            from fastapi import FastAPI
            app = FastAPI()
            @app.get('/users/{user_id}')
            def get_user(user_id: str): return user_id
        """).lstrip("\n"), encoding="utf-8")
        scip_bytes = ci.export_scip_protocol_index(str(backend), project_name="user-service", as_bytes=True)
        self.assertIsInstance(scip_bytes, bytes)
        decoded = ci.decode_scip_index(scip_bytes)
        self.assertEqual(decoded["metadata"]["project_root"], "user-service")

        scip_file = self.root / "contracts/user-service.scip"
        scip_file.parent.mkdir(parents=True, exist_ok=True)
        scip_file.write_bytes(scip_bytes)
        self.write("web.ts", "export function load(id: string) { return fetch(`/users/${id}`); }\n")
        self.write("web.test.ts", "function test_load() { return load('7'); }\n")

        trace = ci.symbol_trace(str(self.root), "web.load", "callees")
        self.assertEqual(trace["callees"], ["user_service.api.get_user"])
        edge = trace["cross_boundary_edges"][0]
        self.assertTrue(edge["cross_repo"])
        self.assertEqual(edge["contract_id"], "http:GET:/users/{}")
        self.assertEqual(edge["scip_symbol"], "scip-http http local _ users/{}#GET().")
        self.assertTrue(any(ev["path"] == "contracts/user-service.scip" and ev["role"] == "contract" for ev in edge["evidence"]))

        impact = ci.impact_analysis(str(self.root), paths=["contracts/user-service.scip"])
        self.assertIn("web.load", impact["affected_symbols"])
        self.assertIn("web.test.ts", impact["related_tests"])
        health = ci.code_health_audit(str(self.root))
        self.assertFalse(any(d["symbol"].startswith("user_service.") for d in health["dead_code"]))

    def test_scip_binary_cross_repo_grpc_and_monorepo_dedup(self):
        backend = self.base / "catalog_repo"
        backend.mkdir()
        (backend / "catalog.proto").write_text("package shop.v1; service Catalog { rpc Get (A) returns (B); }\n", encoding="utf-8")
        (backend / "server.py").write_text(textwrap.dedent("""
            import catalog_pb2_grpc
            class CatalogService(catalog_pb2_grpc.CatalogServicer):
                def Get(self, request, context): return 1
        """).lstrip("\n"), encoding="utf-8")
        scip_bytes = ci.export_scip_protocol_index(str(backend), project_name="catalog-svc", as_bytes=True)

        # Case 1: Remote client repo with only .scip binary index (no backend source or .proto)
        (self.root / "catalog.scip").write_bytes(scip_bytes)
        self.write("client.go", """
            package client
            func Fetch(conn GrpcConn) {
                cli := pb.NewCatalogClient(conn)
                cli.Get(ctx, req)
            }
        """)
        edges = self.edges()
        self.assertEqual(len(edges), 1)
        self.assertEqual(edges[0]["to"], "catalog_svc.server.CatalogService.Get")
        self.assertEqual(edges[0]["scip_symbol"], "scip-grpc proto shop.v1 _ Catalog#Get().")
        self.assertTrue(edges[0]["cross_repo"])

        # Case 2: Monorepo with both local server.py + catalog.proto AND exported .scip must deduplicate cleanly
        self.grpc_fixture()
        mono_edges = self.edges()
        self.assertEqual(len(mono_edges), 1)
        self.assertEqual(mono_edges[0]["to"], "server.CatalogService.Get")
        self.assertNotIn("cross_repo", mono_edges[0])

    def test_scip_json_directive_and_corrupt_wire_diagnostic(self):
        scip_json = {
            "metadata": {"project_root": "order-svc"},
            "documents": [{
                "language": "go",
                "relative_path": "orders.go",
                "occurrences": [{"range": [9, 0, 9, 8], "symbol": "scip-go gomod order-svc v1 orders/GetOrder().", "symbol_roles": 1}],
                "symbols": [{
                    "symbol": "scip-go gomod order-svc v1 orders/GetOrder().",
                    "display_name": "orders.GetOrder",
                    "documentation": ["@route GET /orders/{id}"],
                    "relationships": [],
                }],
            }],
        }
        self.write("orders.scip.json", json.dumps(scip_json))
        (self.root / "corrupt.scip").write_bytes(b"\x0a\x99\x01")
        self.write("web.ts", "export function fetchOrder(id: string) { return fetch(`/orders/${id}`); }\n")
        graph = self.graph()
        self.assertEqual([(e["from"], e["to"]) for e in self.edges(graph)], [("web.fetchOrder", "order_svc.orders.GetOrder")])
        self.assertTrue(any(d["reason"] == "invalid_scip_index" and d["path"] == "corrupt.scip"
                            for d in graph.boundary_analysis["diagnostics"]))

    def test_proto_google_api_http_gateway_transcoding(self):
        self.write("users.proto", """
            syntax = "proto3";
            package identity.v1;
            service UserDirectory {
              rpc GetUser (GetUserRequest) returns (User) {
                option (google.api.http) = {
                  get: "/v1/{name=users/*}"
                  additional_bindings {
                    get: "/users/{user_id}"
                  }
                };
              }
              rpc ArchiveUser (ArchiveRequest) returns (Empty) {
                option (google.api.http) = {
                  custom: { kind: "DELETE" path: "/v1/users/{user_id}" }
                };
              }
            }
        """)
        self.write("server.py", """
            import users_pb2_grpc
            def db_fetch(): return 1
            class DirectoryImpl(users_pb2_grpc.UserDirectoryServicer):
                def GetUser(self, request, context): return db_fetch()
                def ArchiveUser(self, request, context): return None
        """)
        self.write("web.ts", """
            export function byResource(id: string) { return fetch(`/v1/users/${id}`); }
            export function byAlias(id: string) { return fetch(`/users/${id}`); }
            export function removeUser(id: string) { return fetch(`/v1/users/${id}`, {method: 'DELETE'}); }
        """)
        edges = {(e["from"], e["to"], e["contract_id"]): e for e in self.edges()}
        self.assertIn(("web.byResource", "server.DirectoryImpl.GetUser", "http:GET:/v1/users/{}"), edges)
        self.assertIn(("web.byAlias", "server.DirectoryImpl.GetUser", "http:GET:/users/{}"), edges)
        self.assertIn(("web.removeUser", "server.DirectoryImpl.ArchiveUser", "http:DELETE:/v1/users/{}"), edges)
        ev = edges[("web.byResource", "server.DirectoryImpl.GetUser", "http:GET:/v1/users/{}")]["evidence"]
        self.assertTrue(any(item["path"] == "users.proto" and item["role"] == "contract" for item in ev))

    def test_openapi_camel_case_operation_id_and_operationless_route_bridge(self):
        self.write("api.py", """
            from fastapi import FastAPI
            app = FastAPI()
            @app.get('/accounts/{account_id}')
            def fetch_account(account_id: str): return account_id
            def create_invoice(): return 1
        """)
        spec = {
            "openapi": "3.1.0",
            "servers": [{"url": "https://billing.example/api"}],
            "paths": {
                "/accounts/{id}": {
                    "get": {"summary": "No operationId; bridges server origin onto @app.get('/accounts/{account_id}')"}
                },
                "/invoices": {
                    "post": {"operationId": "BillingService.createInvoice"}
                },
            },
        }
        self.write("openapi.json", json.dumps(spec))
        self.write("web.ts", """
            export function loadAccount(id: string) { return fetch(`https://billing.example/api/accounts/${id}`); }
            export function makeInvoice() { return fetch('https://billing.example/api/invoices', {method: 'POST'}); }
        """)
        pairs = sorted((e["from"], e["to"]) for e in self.edges())
        self.assertEqual(pairs, [("web.loadAccount", "api.fetch_account"), ("web.makeInvoice", "api.create_invoice")])

    def test_route_and_scip_first_class_symbol_trace_explore_and_search(self):
        self.backend()
        self.write("web.ts", """
            export function reader(id: string) { return fetch(`/users/${id}`); }
            export function writer(id: string) { return fetch(`/users/${id}`, {method: 'POST'}); }
        """)
        get_trace = ci.symbol_trace(str(self.root), "GET /users/42", "both")
        self.assertEqual(get_trace["resolved"], ["api.get_user"])
        self.assertEqual(get_trace["callers"], ["web.reader"])
        self.assertEqual(get_trace["callees"], ["api.lookup"])
        self.assertIn("[http:GET:/users/{}]", get_trace["compact_trace_markdown"])

        scip_trace = ci.symbol_trace(str(self.root), "scip-http http local _ users/{}#POST().", "callers")
        self.assertEqual(scip_trace["resolved"], ["api.update_user"])
        self.assertEqual(scip_trace["callers"], ["web.writer"])

        ep_search = ci.code_search(str(self.root), "/users", kind="endpoint")
        self.assertEqual({item["contract_id"] for item in ep_search["results"]},
                         {"http:GET:/users/{}", "http:POST:/users/{}"})
        explored = ci.codegraph_explore(str(self.root), "GET /users/42")
        self.assertIn("api.get_user", explored["seeds"])
        impact = ci.impact_analysis(str(self.root), symbols=["GET /users/{user_id}"])
        self.assertIn("web.reader", impact["affected_symbols"])
        self.assertNotIn("web.writer", impact["affected_symbols"])

    def test_polyglot_spring_boot_aspnet_nestjs_actix_axum_and_gin_groups(self):
        self.write("ItemController.java", """
            /* @GetMapping("/ghost") public void ghost() {} */
            @RestController
            @RequestMapping("/api")
            public class ItemController {
                @GetMapping("/items/{id}")
                public Item getItem(@PathVariable("id") String id) throws Exception {
                    return findItem(id);
                }
                private Item findItem(String id) { return null; }
            }
        """)
        self.write("OrdersController.cs", """
            [ApiController]
            [Route("api/[controller]")]
            public class OrdersController : ControllerBase {
                [HttpPost("{id}")]
                public Order SubmitOrder(string id) { return Save(id); }
                private Order Save(string id) { return null; }
            }
        """)
        self.write("profiles.controller.ts", """
            @Controller('v1/profiles')
            export class ProfilesController {
                @Get(':id')
                async getProfile(@Param('id') id: string) { return id; }
            }
        """)
        self.write("express_api.js", """
            const app = express();
            const router = express.Router();
            function listTeams(req, res) { return []; }
            router.get('/teams', listTeams);
            app.use('/v2', router);
        """)
        self.write("accounts.go", """
            package accounts
            type Handler struct {}
            func (h *Handler) GetAccount(c Context) {}
            func Register(r Router, h *Handler) {
                v1 := r.Group("/v1")
                v1.GET("/accounts/:id", h.GetAccount)
            }
        """)
        self.write("service.rs", """
            // #[get("/ghost")] fn ghost() {}
            #[get("/rust/health")]
            pub async fn health_check() -> i32 { 1 }
            pub async fn get_task() -> i32 { 2 }
            pub fn router() {
                App::new().route("/rust/tasks/:id", get(get_task));
            }
        """)
        self.write("frontend.ts", """
            const API_PREFIX = '/api';
            export function callJava(id: string) { return fetch(`${API_PREFIX}/items/${id}`); }
            export function callCSharp(id: string) { return axios.post(API_PREFIX + '/orders/99'); }
            export function callNest(id: string) { return fetch(`/v1/profiles/${id}`); }
            export function callExpress() { return fetch('/v2/teams'); }
            export function callGo(id: string) { return fetch(`/v1/accounts/${id}`); }
            export function callActix() { return fetch('/rust/health'); }
            export function callAxum(id: string) { return fetch(`/rust/tasks/${id}`); }
        """)
        pairs = {(e["from"], e["to"]) for e in self.edges()}
        self.assertEqual(pairs, {
            ("frontend.callJava", "ItemController.ItemController.getItem"),
            ("frontend.callCSharp", "OrdersController.OrdersController.SubmitOrder"),
            ("frontend.callNest", "profiles.controller.ProfilesController.getProfile"),
            ("frontend.callExpress", "express_api.listTeams"),
            ("frontend.callGo", "accounts.Handler.GetAccount"),
            ("frontend.callActix", "service.health_check"),
            ("frontend.callAxum", "service.get_task"),
        })

    def test_rust_tonic_and_java_blocking_stub_grpc(self):
        self.write("catalog.proto", "package shop.v1; service Catalog { rpc GetItem (Req) returns (Resp); }\n")
        self.write("server.rs", """
            pub struct CatalogImpl {}
            impl shop::catalog_server::Catalog for CatalogImpl {
                async fn get_item(&self, req: Req) -> Resp { 1 }
            }
        """)
        self.write("Client.java", """
            public class Client {
                public void fetch(Channel channel) {
                    stub = CatalogGrpc.newBlockingStub(channel);
                    stub.getItem(req);
                }
            }
        """)
        self.write("rust_client.rs", """
            pub async fn load() {
                let mut client = CatalogClient::connect("http://127.0.0.1:50051");
                client.get_item(req);
            }
        """)
        pairs = {(e["from"], e["to"]) for e in self.edges()}
        self.assertEqual(pairs, {
            ("Client.Client.fetch", "server.CatalogImpl.get_item"),
            ("rust_client.load", "server.CatalogImpl.get_item"),
        })

    def test_flask_blueprint_cross_file_mount_and_django_urls(self):
        self.write("bp.py", """
            from flask import Blueprint
            users_bp = Blueprint('users', __name__, url_prefix='/users')
            @users_bp.get('/<int:user_id>')
            def show_user(user_id): return user_id
        """)
        self.write("app.py", """
            from flask import Flask
            from bp import users_bp
            app = Flask(__name__)
            app.register_blueprint(users_bp, url_prefix='/v1')
        """)
        self.write("urls.py", """
            from django.urls import path
            def article_detail(request): return 1
            urlpatterns = [path('articles/<int:pk>', article_detail)]
        """)
        self.write("web.ts", """
            export function getUser() { return fetch('/v1/users/7'); }
            export function getArticle() { return fetch('/articles/3'); }
        """)
        pairs = {(e["from"], e["to"]) for e in self.edges()}
        self.assertEqual(pairs, {
            ("web.getUser", "bp.show_user"),
            ("web.getArticle", "urls.article_detail"),
        })


if __name__ == "__main__":
    unittest.main()
