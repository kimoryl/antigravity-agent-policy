"""Sourcegraph SCIP protocol symbols and zero-dependency .scip / .scip.json index codec.

Implements:
1. Canonical 5-part SCIP symbol grammar (`<scheme> <manager> <package> <version> <descriptor>`)
   for language-neutral HTTP routes, gRPC RPCs, and source declarations.
2. Pure-stdlib Protobuf wire-format decoder/encoder for official `scip.Index` (`index.scip`)
   covering Metadata, Document, Occurrence (packed & unpacked varint ranges, symbol_roles),
   SymbolInformation, and Relationship messages without external protobuf packages.
3. Cross-repository protocol endpoint/request extraction and SCIP index export.
"""
from __future__ import annotations

import json
import re
from pathlib import PurePosixPath
from typing import Any

__all__ = [
    "code_scip_symbol",
    "decode_scip_index",
    "encode_scip_index",
    "extract_scip_facts",
    "grpc_scip_symbol",
    "http_scip_symbol",
    "parse_protocol_scip_symbol",
    "build_scip_protocol_index",
]

DEFINITION_ROLE = 0x1
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}


def _clean_token(value: str, fallback: str = "local") -> str:
    cleaned = value.strip().replace(" ", "%20")
    return cleaned if cleaned else fallback


def http_scip_symbol(method: str, authority: str, canonical_path: str) -> str:
    """Format a canonical SCIP symbol for an HTTP protocol endpoint."""
    pkg = _clean_token(authority, "local")
    route = canonical_path if canonical_path == "/" else canonical_path.lstrip("/")
    route = route.replace(" ", "%20") or "/"
    return f"scip-http http {pkg} _ {route}#{method.upper()}()."


def grpc_scip_symbol(full_service: str, method: str) -> str:
    """Format a canonical SCIP symbol for a gRPC service method."""
    pkg, dot, svc = full_service.strip("/").rpartition(".")
    package = _clean_token(pkg if dot else "local", "local")
    service = _clean_token(svc or full_service, "Service")
    return f"scip-grpc proto {package} _ {service}#{method}()."


def code_scip_symbol(language: str, project_name: str, qualified: str, kind: str = "function") -> str:
    """Format a canonical SCIP symbol for a source declaration."""
    lang = _clean_token(language or "polyglot", "polyglot")
    pkg = _clean_token(project_name or "workspace", "workspace")
    parts = [p for p in qualified.split(".") if p]
    if not parts:
        return f"scip-codegraph {lang} {pkg} HEAD root."
    if kind == "class":
        descriptor = "/".join(parts[:-1] + [parts[-1] + "#"]) if len(parts) > 1 else parts[0] + "#"
    elif kind == "method" and len(parts) >= 2:
        prefix = "/".join(parts[:-2])
        head = f"{prefix}/{parts[-2]}" if prefix else parts[-2]
        descriptor = f"{head}#{parts[-1]}()."
    else:
        prefix = "/".join(parts[:-1])
        descriptor = f"{prefix}/{parts[-1]}()." if prefix else f"{parts[-1]}()."
    return f"scip-codegraph {lang} {pkg} HEAD {descriptor}"


def parse_protocol_scip_symbol(symbol: str) -> dict[str, str] | None:
    """Parse a `scip-http` or `scip-grpc` symbol into normalized protocol coordinates."""
    parts = symbol.strip().split(" ", 4)
    if len(parts) != 5:
        return None
    scheme, _manager, package, _version, descriptor = parts
    if not descriptor.endswith("().") or "#" not in descriptor:
        return None
    stem, target = descriptor[:-3].rsplit("#", 1)
    if scheme == "scip-http" and target.upper() in _HTTP_METHODS:
        authority = "" if package in {"local", ".", "_"} else package.replace("%20", " ")
        raw_path = "/" if stem == "/" else "/" + stem.replace("%20", " ")
        route_path = re.sub(r"\{\}", "{param}", raw_path)
        return {"protocol": "http", "method": target.upper(), "authority": authority, "route": authority + route_path}
    if scheme == "scip-grpc" and stem and target:
        pkg = "" if package in {"local", ".", "_"} else package
        service = f"{pkg}.{stem}" if pkg else stem
        return {"protocol": "grpc", "service": service, "method": target}
    return None


def _encode_varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64
    out = bytearray()
    while value > 0x7F:
        out.append((value & 0x7F) | 0x80)
        value >>= 7
    out.append(value & 0x7F)
    return bytes(out)


def _decode_varint(buf: bytes, pos: int, limit: int) -> tuple[int, int]:
    result, shift = 0, 0
    while pos < limit and shift < 70:
        byte = buf[pos]
        pos += 1
        result |= (byte & 0x7F) << shift
        if not (byte & 0x80):
            return (result - (1 << 64) if result >= (1 << 63) else result), pos
        shift += 7
    raise ValueError("truncated or malformed protobuf varint")


def _iter_fields(buf: bytes, limit: int | None = None):
    pos, end = 0, len(buf) if limit is None else limit
    while pos < end:
        tag, pos = _decode_varint(buf, pos, end)
        field_no, wire_type = tag >> 3, tag & 0x7
        if tag <= 0 or field_no <= 0:
            raise ValueError("invalid protobuf tag")
        if wire_type == 0:
            val, pos = _decode_varint(buf, pos, end)
            yield field_no, wire_type, val
        elif wire_type == 1:
            if pos + 8 > end:
                raise ValueError("truncated fixed64")
            yield field_no, wire_type, buf[pos:pos + 8]
            pos += 8
        elif wire_type == 2:
            length, pos = _decode_varint(buf, pos, end)
            if length < 0 or pos + length > end:
                raise ValueError("invalid length-delimited field")
            yield field_no, wire_type, buf[pos:pos + length]
            pos += length
        elif wire_type == 5:
            if pos + 4 > end:
                raise ValueError("truncated fixed32")
            yield field_no, wire_type, buf[pos:pos + 4]
            pos += 4
        else:
            raise ValueError(f"unsupported wire type {wire_type}")


def _decode_packed_ints(raw: bytes) -> list[int]:
    values: list[int] = []
    pos, end = 0, len(raw)
    while pos < end:
        val, pos = _decode_varint(raw, pos, end)
        values.append(int(val))
    return values


def _decode_relationship(raw: bytes) -> dict[str, Any]:
    rel: dict[str, Any] = {"symbol": "", "is_reference": False, "is_implementation": False,
                           "is_type_definition": False, "is_definition": False}
    bool_fields = {2: "is_reference", 3: "is_implementation", 4: "is_type_definition", 5: "is_definition"}
    for field_no, wire_type, val in _iter_fields(raw):
        if field_no == 1 and wire_type == 2:
            rel["symbol"] = val.decode("utf-8")
        elif field_no in bool_fields and wire_type == 0:
            rel[bool_fields[field_no]] = bool(val)
    return rel


def _decode_symbol_info(raw: bytes) -> dict[str, Any]:
    info: dict[str, Any] = {"symbol": "", "documentation": [], "relationships": [],
                            "kind": 0, "display_name": "", "enclosing_symbol": ""}
    for field_no, wire_type, val in _iter_fields(raw):
        if field_no == 1 and wire_type == 2:
            info["symbol"] = val.decode("utf-8")
        elif field_no == 2 and wire_type == 2:
            info["documentation"].append(val.decode("utf-8"))
        elif field_no == 3 and wire_type == 2:
            info["relationships"].append(_decode_relationship(val))
        elif field_no == 4 and wire_type == 0:
            info["kind"] = int(val)
        elif field_no == 5 and wire_type == 2:
            info["display_name"] = val.decode("utf-8")
        elif field_no == 7 and wire_type == 2:
            info["enclosing_symbol"] = val.decode("utf-8")
    return info


def _decode_occurrence(raw: bytes) -> dict[str, Any]:
    occ: dict[str, Any] = {"range": [], "symbol": "", "symbol_roles": 0, "enclosing_range": []}
    for field_no, wire_type, val in _iter_fields(raw):
        if field_no == 1:
            occ["range"].extend(_decode_packed_ints(val) if wire_type == 2 else [int(val)])
        elif field_no == 2 and wire_type == 2:
            occ["symbol"] = val.decode("utf-8")
        elif field_no == 3 and wire_type == 0:
            occ["symbol_roles"] = int(val)
        elif field_no == 7:
            occ["enclosing_range"].extend(_decode_packed_ints(val) if wire_type == 2 else [int(val)])
    return occ


def _decode_document(raw: bytes) -> dict[str, Any]:
    doc: dict[str, Any] = {"language": "", "relative_path": "", "occurrences": [], "symbols": []}
    for field_no, wire_type, val in _iter_fields(raw):
        if field_no == 1 and wire_type == 2:
            doc["language"] = val.decode("utf-8")
        elif field_no == 2 and wire_type == 2:
            doc["relative_path"] = val.decode("utf-8")
        elif field_no == 3 and wire_type == 2:
            doc["occurrences"].append(_decode_occurrence(val))
        elif field_no == 4 and wire_type == 2:
            doc["symbols"].append(_decode_symbol_info(val))
    return doc


def _decode_metadata(raw: bytes) -> dict[str, Any]:
    meta: dict[str, Any] = {"version": 0, "project_root": "", "tool_name": "", "tool_version": ""}
    for field_no, wire_type, val in _iter_fields(raw):
        if field_no == 1 and wire_type == 0:
            meta["version"] = int(val)
        elif field_no == 2 and wire_type == 2:
            for tf, tw, tv in _iter_fields(val):
                if tf == 1 and tw == 2:
                    meta["tool_name"] = tv.decode("utf-8")
                elif tf == 2 and tw == 2:
                    meta["tool_version"] = tv.decode("utf-8")
        elif field_no == 3 and wire_type == 2:
            meta["project_root"] = val.decode("utf-8")
    return meta


def decode_scip_index(raw: bytes) -> dict[str, Any]:
    """Decode an official `scip.Index` protobuf wire-format byte stream."""
    if not raw:
        raise ValueError("empty SCIP index")
    index: dict[str, Any] = {"metadata": {}, "documents": [], "external_symbols": []}
    seen_known = False
    for field_no, wire_type, val in _iter_fields(raw):
        if field_no == 1 and wire_type == 2:
            index["metadata"] = _decode_metadata(val)
            seen_known = True
        elif field_no == 2 and wire_type == 2:
            index["documents"].append(_decode_document(val))
            seen_known = True
        elif field_no == 3 and wire_type == 2:
            index["external_symbols"].append(_decode_symbol_info(val))
            seen_known = True
    if not seen_known:
        raise ValueError("unrecognized SCIP index payload")
    return index


def _bytes_field(field_no: int, payload: bytes) -> bytes:
    if not payload:
        return b""
    return _encode_varint((field_no << 3) | 2) + _encode_varint(len(payload)) + payload


def _str_field(field_no: int, text: str) -> bytes:
    return _bytes_field(field_no, text.encode("utf-8")) if text else b""


def _varint_field(field_no: int, value: int) -> bytes:
    return _encode_varint((field_no << 3) | 0) + _encode_varint(value) if value else b""


def _encode_symbol_info(info: dict[str, Any]) -> bytes:
    buf = bytearray(_str_field(1, info.get("symbol", "")))
    for doc in info.get("documentation", []):
        buf.extend(_str_field(2, doc))
    for rel in info.get("relationships", []):
        rb = bytearray(_str_field(1, rel.get("symbol", "")))
        for num, key in ((2, "is_reference"), (3, "is_implementation"), (4, "is_type_definition"), (5, "is_definition")):
            if rel.get(key):
                rb.extend(_varint_field(num, 1))
        buf.extend(_bytes_field(3, bytes(rb)))
    buf.extend(_varint_field(4, int(info.get("kind", 0))))
    buf.extend(_str_field(5, info.get("display_name", "")))
    buf.extend(_str_field(7, info.get("enclosing_symbol", "")))
    return bytes(buf)


def encode_scip_index(index_data: dict[str, Any]) -> bytes:
    """Encode a structured SCIP index dictionary into official `scip.Index` protobuf wire bytes."""
    out = bytearray()
    meta = index_data.get("metadata") or {}
    tool = _str_field(1, meta.get("tool_name", "code-intelligence")) + _str_field(2, meta.get("tool_version", "2.1.0"))
    meta_bytes = _varint_field(1, int(meta.get("version", 0))) + _bytes_field(2, tool) + _str_field(3, meta.get("project_root", "workspace"))
    out.extend(_bytes_field(1, meta_bytes))
    for doc in index_data.get("documents", []):
        db = bytearray(_str_field(1, doc.get("language", "")) + _str_field(2, doc.get("relative_path", "")))
        for occ in doc.get("occurrences", []):
            packed = b"".join(_encode_varint(int(v)) for v in occ.get("range", []))
            enclosing = b"".join(_encode_varint(int(v)) for v in occ.get("enclosing_range", []))
            ob = _bytes_field(1, packed) + _str_field(2, occ.get("symbol", "")) + _varint_field(3, int(occ.get("symbol_roles", 0))) + _bytes_field(7, enclosing)
            db.extend(_bytes_field(3, ob))
        for sym in doc.get("symbols", []):
            db.extend(_bytes_field(4, _encode_symbol_info(sym)))
        out.extend(_bytes_field(2, bytes(db)))
    for ext in index_data.get("external_symbols", []):
        out.extend(_bytes_field(3, _encode_symbol_info(ext)))
    return bytes(out)


def _display_from_scip_symbol(symbol: str, doc_path: str) -> str:
    parts = symbol.strip().split(" ", 4)
    desc = parts[4] if len(parts) == 5 else symbol
    cleaned = desc.replace("().", "").replace("#", ".").replace("/", ".").strip(".")
    if cleaned:
        return cleaned
    stem = PurePosixPath(doc_path or "external").with_suffix("").as_posix().replace("/", ".")
    return stem or "external.handler"


def _repo_slug(project_root: str) -> str:
    if not project_root:
        return ""
    cleaned = project_root.rstrip("/\\")
    tail = re.split(r"[/\\]", cleaned)[-1]
    slug = re.sub(r"[^A-Za-z0-9_]", "_", tail).strip("_")
    return "" if slug in {"", "workspace", "local"} else slug


def _doc_protocol_directives(docs: list[str]) -> list[tuple[str, dict[str, str]]]:
    directives: list[tuple[str, dict[str, str]]] = []
    for text in docs:
        for line in str(text).splitlines():
            stripped = line.strip().lstrip("@")
            parsed = parse_protocol_scip_symbol(stripped)
            if parsed:
                directives.append(("implements", parsed))
                continue
            http_m = re.match(r"^(?:route|http)\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\s+(\S+)$", stripped, re.I)
            if http_m:
                directives.append(("implements", {"protocol": "http", "method": http_m.group(1).upper(),
                                                  "authority": "", "route": http_m.group(2)}))
            grpc_m = re.match(r"^(?:rpc|grpc)\s+/?([\w.]+)/(\w+)$", stripped, re.I)
            if grpc_m:
                directives.append(("implements", {"protocol": "grpc", "service": grpc_m.group(1), "method": grpc_m.group(2)}))
    return directives


def _extract_from_index_dict(data: dict[str, Any], rel: str) -> dict[str, list]:
    out: dict[str, list] = {k: [] for k in ("endpoints", "requests", "rpcs", "diagnostics", "suppress_calls", "roots", "mounts", "scip_symbols")}
    meta = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
    repo_root = str(meta.get("project_root", ""))
    slug = _repo_slug(repo_root)
    documents = data.get("documents")
    if not isinstance(documents, list):
        raise ValueError("SCIP documents must be a list")

    for doc in documents:
        if not isinstance(doc, dict):
            continue
        doc_path = str(doc.get("relative_path", rel))
        def_lines: dict[str, int] = {}
        def_spans: list[tuple[int, int, str]] = []
        for occ in doc.get("occurrences", []):
            if not isinstance(occ, dict) or not isinstance(occ.get("symbol"), str):
                continue
            rng = occ.get("range") or [0]
            line = int(rng[0]) + 1 if rng else 1
            sym = occ["symbol"]
            roles = int(occ.get("symbol_roles", 0))
            if roles & DEFINITION_ROLE and not parse_protocol_scip_symbol(sym):
                def_lines.setdefault(sym, line)
                enc = occ.get("enclosing_range") or rng
                end_line = (int(enc[2]) + 1 if len(enc) >= 3 else line)
                def_spans.append((line, max(line, end_line), sym))

        sym_names: dict[str, str] = {}
        for sym_info in doc.get("symbols", []):
            if not isinstance(sym_info, dict) or not isinstance(sym_info.get("symbol"), str):
                continue
            raw_sym = sym_info["symbol"]
            if parse_protocol_scip_symbol(raw_sym):
                continue
            local_name = str(sym_info.get("display_name") or _display_from_scip_symbol(raw_sym, doc_path))
            ext_name = f"{slug}.{local_name}" if slug and not local_name.startswith(slug + ".") else local_name
            line = def_lines.get(raw_sym, 1)
            sym_names[raw_sym] = ext_name
            out["scip_symbols"].append({
                "scip_symbol": raw_sym,
                "local_candidate": local_name,
                "external_qualified": ext_name,
                "name": local_name.rsplit(".", 1)[-1],
                "kind": "method" if "#" in raw_sym.split(" ")[-1] else "function",
                "path": rel,
                "line": line,
                "external_repo": repo_root,
                "external_path": doc_path,
            })
            relations: list[tuple[str, dict[str, str]]] = []
            for rel_item in sym_info.get("relationships", []):
                if not isinstance(rel_item, dict) or not isinstance(rel_item.get("symbol"), str):
                    continue
                proto = parse_protocol_scip_symbol(rel_item["symbol"])
                if not proto:
                    continue
                if rel_item.get("is_implementation") or rel_item.get("is_definition"):
                    relations.append(("implements", proto))
                elif rel_item.get("is_reference"):
                    relations.append(("references", proto))
            relations.extend(_doc_protocol_directives(sym_info.get("documentation") or []))

            for role, proto in relations:
                if role == "implements":
                    if proto["protocol"] == "http":
                        out["endpoints"].append({"protocol": "http", "method": proto["method"], "route": proto["route"],
                                                 "handler": local_name, "scip_handler": ext_name, "scip_target": raw_sym,
                                                 "path": rel, "line": line, "contract": True, "from_scip": True})
                    else:
                        out["rpcs"].append({"service": proto["service"], "method": proto["method"], "path": rel, "line": line})
                        out["endpoints"].append({"protocol": "grpc", "service": proto["service"], "method": proto["method"],
                                                 "handler": local_name, "scip_handler": ext_name, "scip_target": raw_sym,
                                                 "path": rel, "line": line, "contract": True, "from_scip": True})
                elif role == "references":
                    req_base = {"caller": local_name, "scip_caller": ext_name, "path": rel, "line": line, "from_scip": True}
                    if proto["protocol"] == "http":
                        out["requests"].append({"protocol": "http", "method": proto["method"], "route": proto["route"], **req_base})
                    else:
                        out["requests"].append({"protocol": "grpc", "service": proto["service"], "method": proto["method"], **req_base})

        for occ in doc.get("occurrences", []):
            if not isinstance(occ, dict) or not isinstance(occ.get("symbol"), str):
                continue
            proto = parse_protocol_scip_symbol(occ["symbol"])
            if not proto or (int(occ.get("symbol_roles", 0)) & DEFINITION_ROLE):
                continue
            rng = occ.get("range") or [0]
            line = int(rng[0]) + 1 if rng else 1
            enclosing = [sym for start, end, sym in def_spans if start <= line <= end]
            if not enclosing:
                continue
            owner_sym = enclosing[-1]
            caller_name = sym_names.get(owner_sym) or _display_from_scip_symbol(owner_sym, doc_path)
            req_base = {"caller": caller_name, "scip_caller": caller_name, "path": rel, "line": line, "from_scip": True}
            if proto["protocol"] == "http":
                out["requests"].append({"protocol": "http", "method": proto["method"], "route": proto["route"], **req_base})
            else:
                out["requests"].append({"protocol": "grpc", "service": proto["service"], "method": proto["method"], **req_base})
    return out


def extract_scip_facts(raw: bytes | str, rel: str) -> dict[str, list]:
    """Parse binary `.scip` or JSON `.scip.json` into protocol facts and external symbols."""
    empty = {k: [] for k in ("endpoints", "requests", "rpcs", "diagnostics", "suppress_calls", "roots", "mounts", "scip_symbols")}
    try:
        if isinstance(raw, bytes):
            data = decode_scip_index(raw)
        else:
            parsed = json.loads(raw)
            if not isinstance(parsed, dict) or "documents" not in parsed:
                return empty
            data = parsed
        return _extract_from_index_dict(data, rel)
    except (ValueError, UnicodeDecodeError, TypeError) as exc:
        empty["diagnostics"].append({"path": rel, "reason": "invalid_scip_index", "detail": str(exc)})
        return empty


def build_scip_protocol_index(
    project_name: str,
    files: dict[str, dict[str, Any]],
    symbols: dict[str, dict[str, Any]],
    resolved_endpoints: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> dict[str, Any]:
    """Export workspace declarations and HTTP/gRPC contracts as a standard SCIP Index dict."""
    by_file_eps: dict[str, list[dict[str, Any]]] = {}
    for ep in resolved_endpoints:
        handler_info = symbols.get(ep["handler"])
        if handler_info and not handler_info.get("external"):
            by_file_eps.setdefault(handler_info["path"], []).append(ep)

    by_file_reqs: dict[str, list[dict[str, Any]]] = {}
    for edge in edges:
        if "boundary" in edge and edge.get("from") in symbols:
            caller_info = symbols[edge["from"]]
            if not caller_info.get("external"):
                by_file_reqs.setdefault(caller_info["path"], []).append(edge)

    documents: list[dict[str, Any]] = []
    for rel, record in sorted(files.items()):
        if record.get("language") in {"scip", "json", "yaml", "yml", "proto"}:
            continue
        file_syms = [q for q in record.get("symbols", []) if not symbols.get(q, {}).get("external")]
        if not file_syms:
            continue
        lang = record.get("language", "polyglot")
        occurrences: list[dict[str, Any]] = []
        sym_infos: list[dict[str, Any]] = []
        eps = by_file_eps.get(rel, [])
        reqs = by_file_reqs.get(rel, [])

        for qname in file_syms:
            info = symbols[qname]
            scip_sym = code_scip_symbol(lang, project_name, qname, info.get("kind", "function"))
            line0 = max(0, int(info.get("line", 1)) - 1)
            end_line0 = line0 + max(0, int(info.get("loc", 1)) - 1)
            occurrences.append({"range": [line0, 0, line0, len(info["name"])], "symbol": scip_sym,
                                "symbol_roles": DEFINITION_ROLE, "enclosing_range": [line0, 0, end_line0, 0]})
            rels: list[dict[str, Any]] = []
            for ep in eps:
                if ep["handler"] == qname and ep.get("scip_symbol"):
                    rels.append({"symbol": ep["scip_symbol"], "is_implementation": True})
                    occurrences.append({"range": [line0, 0, line0, len(info["name"])],
                                        "symbol": ep["scip_symbol"], "symbol_roles": DEFINITION_ROLE})
            for req_edge in reqs:
                if req_edge["from"] == qname and req_edge.get("scip_symbol"):
                    rels.append({"symbol": req_edge["scip_symbol"], "is_reference": True})
                    req_line = next((int(ev["line"]) - 1 for ev in req_edge.get("evidence", [])
                                     if ev.get("role") == "request" and ev.get("path") == rel), line0)
                    occurrences.append({"range": [max(0, req_line), 0, max(0, req_line), 1],
                                        "symbol": req_edge["scip_symbol"], "symbol_roles": 0x8})
            sym_infos.append({"symbol": scip_sym, "display_name": qname, "relationships": rels, "documentation": []})
        documents.append({"language": lang, "relative_path": rel, "occurrences": occurrences, "symbols": sym_infos})

    return {
        "metadata": {"version": 0, "project_root": project_name or "workspace",
                     "tool_name": "code-intelligence", "tool_version": "2.1.0"},
        "documents": documents,
        "external_symbols": [],
    }
