"""Build and deploy a Genie Agent from a declarative YAML config (idempotent by title).

Usage: python build_space.py <config.yaml> [--dry-run] [--validate-sql]
"""
import argparse
import hashlib
import json
import os
import re
import sys

import yaml

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "setup"))
import config  # noqa: E402
from sqlrun import client, run  # noqa: E402


def sub(text):
    p = config.params()
    return re.sub(r"\$\{(\w+)\}", lambda m: str(p[m.group(1)]), text)


def hid(*parts):
    # Stable 32-hex id from content, so re-deploys keep ids
    return hashlib.md5("|".join(parts).encode()).hexdigest()


def lines(sql):
    sql = sub(sql).strip()
    out = sql.splitlines(keepends=True)
    return out if out else [sql]


def serialize(cfg):
    tables = []
    for t in cfg.get("tables", []):
        entry = {"identifier": sub(t["identifier"])}
        if t.get("description"):
            entry["description"] = [t["description"]]
        cols = []
        for name, c in sorted((t.get("columns") or {}).items()):
            cc = {"column_name": name}
            if c.get("description"):
                cc["description"] = [c["description"]]
            if c.get("synonyms"):
                cc["synonyms"] = c["synonyms"]
            if c.get("entity_matching"):
                cc["enable_entity_matching"] = True
                cc["enable_format_assistance"] = True
            if c.get("exclude"):
                cc["exclude"] = True
            cols.append(cc)
        if cols:
            entry["column_configs"] = cols
        tables.append(entry)
    mvs = [{"identifier": sub(m["identifier"]), **({"description": [m["description"]]} if m.get("description") else {})}
           for m in cfg.get("metric_views", [])]
    joins = [{"id": hid("join", j["left"], j["right"], j["condition"]),
              "left": {"identifier": f"{config.CATALOG}.{config.SCHEMA}.{j['left']}"},
              "right": {"identifier": f"{config.CATALOG}.{config.SCHEMA}.{j['right']}"},
              "sql": [j["condition"], f"--rt=FROM_RELATIONSHIP_TYPE_{j.get('type', 'MANY_TO_ONE')}--"],
              "comment": [j.get("comment", "")]} for j in cfg.get("join_specs", [])]
    snip = cfg.get("sql_snippets", {})
    snippets = {}
    for kind in ("filters", "expressions", "measures"):
        items = [{"id": hid(kind, s["name"]), "sql": [s["sql"]], "display_name": s["name"],
                  **({} if kind == "filters" else {"alias": re.sub(r"\W+", "_", s["name"].lower()).strip("_")})}
                 for s in snip.get(kind, [])]
        if items:
            snippets[kind] = sorted(items, key=lambda x: x["id"])
    instructions = {
        "text_instructions": [{"id": hid("text"), "content": [cfg["text_instruction"].strip()]}],
        "example_question_sqls": sorted([{"id": hid("ex", e["question"]), "question": [e["question"]], "sql": lines(e["sql"])}
                                         for e in cfg.get("example_sqls", [])], key=lambda x: x["id"]),
    }
    if joins:
        instructions["join_specs"] = sorted(joins, key=lambda x: x["id"])
    if snippets:
        instructions["sql_snippets"] = snippets
    if cfg.get("sql_functions"):
        instructions["sql_functions"] = sorted([{"id": hid("fn", f), "identifier": sub(f)} for f in cfg["sql_functions"]],
                                               key=lambda x: x["id"])
    space = {
        "version": 2,
        "config": {"sample_questions": sorted([{"id": hid("sq", q), "question": [q]} for q in cfg.get("sample_questions", [])],
                                              key=lambda x: x["id"])},
        "data_sources": {"tables": sorted(tables, key=lambda x: x["identifier"])},
        "instructions": instructions,
    }
    if mvs:
        space["data_sources"]["metric_views"] = sorted(mvs, key=lambda x: x["identifier"])
    if cfg.get("benchmarks"):
        space["benchmarks"] = {"questions": sorted([{"id": hid("bm", b["question"]), "question": [b["question"]],
                                                     "answer": [{"format": "SQL", "content": lines(b["sql"])}]}
                                                    for b in cfg["benchmarks"]], key=lambda x: x["id"])}
    return space


def validate_sql(cfg):
    w = client()
    ok = True
    for kind, items in (("example", cfg.get("example_sqls", [])), ("benchmark", cfg.get("benchmarks", []))):
        for e in items:
            try:
                rows = run(sub(e["sql"]).strip().rstrip(";") , w)
                print(f"  OK  {kind}: {e['question'][:70]} -> {(rows or [[None]])[0]}")
            except Exception as ex:  # noqa: BLE001
                ok = False
                print(f"  ERR {kind}: {e['question'][:70]} -> {str(ex).splitlines()[0][:200]}")
    return ok


def deploy(cfg, space):
    w = client()
    title = f'{cfg["title"]} ({config.ENV})'  # one space per environment; matched by this exact title
    known = os.environ.get(cfg["resolved_key"]) if cfg.get("resolved_key") else None
    existing = (w.genie.get_space(known) if known else
                next((s for s in (w.genie.list_spaces(page_size=200).spaces or []) if s.title == title), None))
    payload = json.dumps(space)
    if existing:
        w.genie.update_space(existing.space_id, serialized_space=payload, title=title,
                             description=cfg.get("description", ""), warehouse_id=config.WAREHOUSE_ID)
        sid = existing.space_id
        print("updated", sid)
    else:
        sid = w.genie.create_space(warehouse_id=config.WAREHOUSE_ID, serialized_space=payload, title=title,
                                   description=cfg.get("description", "")).space_id
        # Normalize: create injects FK-derived joins; update replaces them with exactly the config
        w.genie.update_space(sid, serialized_space=payload, title=title,
                             description=cfg.get("description", ""), warehouse_id=config.WAREHOUSE_ID)
        print("created", sid)
    print(f"{w.config.host}/genie/rooms/{sid}")
    if cfg.get("resolved_key"):
        config.save_resolved(**{cfg["resolved_key"]: sid})
    return sid


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("config_file")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--validate-sql", action="store_true")
    a = ap.parse_args()
    cfg = yaml.safe_load(open(a.config_file))
    space = serialize(cfg)
    if a.validate_sql and not validate_sql(cfg):
        sys.exit("SQL validation failed")
    if a.dry_run:
        print(json.dumps(space, indent=1)[:4000])
    else:
        deploy(cfg, space)
