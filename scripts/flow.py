#!/usr/bin/env python3
"""Validate and execute a STEAM graph. stdout is one JSON object per invocation."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import struct
import sys
import tempfile
from contextlib import contextmanager
import zlib


sys.dont_write_bytecode = True  # A project-local skill must not generate cache files there.
ROOT = Path(__file__).resolve().parents[1]
STEP_FIELDS = {"id", "route", "brief", "needs", "access", "kind", "outputs", "refs"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
# Only these DOS device names can pass the ASCII step ID syntax above.
WINDOWS_DEVICE_ID = re.compile(r"CON|PRN|AUX|NUL|(?:COM|LPT)[1-9]", re.I)
TERMINAL = {"success", "failed", "interrupted"}


class Invalid(ValueError):
    pass


class Busy(Exception):
    pass


def require(condition, message):
    if not condition:
        raise Invalid(message)


def object_pairs(pairs):
    obj = {}
    for key, value in pairs:
        require(key not in obj, f"Duplicate JSON key: {key}")
        obj[key] = value
    return obj


def parse_json(text):
    return json.loads(text.removeprefix("\ufeff"), object_pairs_hook=object_pairs)


def read_json(path):
    return parse_json(Path(path).read_text(encoding="utf-8"))


def write_json(path, value):
    # Replace atomically, including on Windows. state.json is the recovery journal.
    temp = path.with_suffix(".tmp")
    with temp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)


def nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def string_list(value, field):
    require(isinstance(value, list) and all(nonempty(v) for v in value),
            f"{field} must be an array of non-empty strings")
    require(len(set(value)) == len(value), f"Duplicate value in {field}")
    return value


def allowed_keys(value, allowed, field):
    require(isinstance(value, dict), f"{field} must be an object")
    extra = set(value) - set(allowed)
    require(not extra, f"{field}: unknown fields {sorted(extra)}")


def config():
    data = read_json(ROOT / "config.json")
    require(isinstance(data, dict) and type(data.get("version")) is int
            and data["version"] == 1, "Expected config version 1")
    allowed_keys(data, ("version","supervisor","agents","routes","limits"), "config")
    agents = data.get("agents")
    routes = data.get("routes")
    limits = data.get("limits")
    require(isinstance(agents, dict) and agents, "config.agents must be a non-empty object")
    require(isinstance(routes, dict) and routes, "config.routes must be a non-empty object")
    allowed_keys(limits, ("max_steps","max_calls"), "config.limits")
    for name, agent in agents.items():
        require(nonempty(name) and isinstance(agent, dict), f"Invalid agent: {name}")
        allowed_keys(agent, ("adapter","executable","enabled","models","timeout_seconds"), f"agent {name}")
        require(agent.get("executable") is None or nonempty(agent["executable"]),
                f"{name}.executable must be null or a non-empty string")
        require(type(agent.get("timeout_seconds", 1800)) is int and agent.get("timeout_seconds", 1800) > 0,
                f"{name}.timeout_seconds must be a positive integer")
        require(agent.get("adapter") in ("claude", "codex", "antigravity"),
                f"{name}: unrecognized adapter")
        require(isinstance(agent.get("enabled", True), bool), f"{name}.enabled must be boolean")
        models = agent.get("models")
        require(isinstance(models, dict) and models, f"{name}.models must be a non-empty object")
        for model, levels in models.items():
            require(nonempty(model) and string_list(levels, f"{name}.models.{model}"),
                    f"{name}.{model} needs at least one level")

    def binding(name, value):
        require(isinstance(value, dict), f"{name} must be an object")
        a, m, level = (value.get(k) for k in ("agent", "model", "level"))
        require(all(nonempty(v) for v in (a, m, level)), f"{name} requires agent, model and level")
        require(a in agents and m in agents[a]["models"] and level in agents[a]["models"][m],
                f"{name}: unconfigured agent/model/level: {a} / {m} / {level}")

    supervisor = data.get("supervisor")
    allowed_keys(supervisor, ("agent","model","level"), "supervisor")
    binding("supervisor", supervisor)
    primary = agents[supervisor["agent"]]
    require(primary["adapter"] == "claude" and primary.get("enabled", True),
            "supervisor must use an enabled Claude agent")
    for name, route in routes.items():
        allowed_keys(route, ("description","agent","model","level"), f"route {name}")
        require(nonempty(name) and isinstance(route.get("description", ""), str),
                f"route {name}: invalid name or description")
        binding(f"route {name}", route)
    require(type(limits.get("max_steps")) is int and limits["max_steps"] > 0,
            "limits.max_steps must be a positive integer")
    caps = limits.get("max_calls", {})
    require(isinstance(caps, dict) and all(a in agents and type(n) is int and n >= 0
                                          for a, n in caps.items()),
            "limits.max_calls must map agent IDs to non-negative integers")
    return data


def workspace_path(value):
    p = Path(value).expanduser().resolve(strict=True)
    require(p.is_dir(), f"Workspace must be a directory: {p}")
    return p


def artifact_path(workspace, value):
    # Keep path spelling canonical so output ownership and dependencies agree.
    require(nonempty(value) and "\x00" not in value and "\\" not in value and
            not PureWindowsPath(value).drive and not Path(value).is_absolute()
            and all(part not in ("", ".", "..") for part in value.split("/")),
            f"Expected a workspace-relative slash-separated file path: {value!r}")
    current = workspace
    for part in value.split("/"):
        current = current / part
        require(not current.is_symlink(), f"Symlink in project artifact path: {value}")
        if current != workspace / value:
            require(not current.exists() or current.is_dir(),
                    f"Artifact parent is not a directory: {value}")
    require(current.resolve().is_relative_to(workspace), f"Artifact outside workspace: {value}")
    require(not current.exists() or current.is_file(), f"Artifact is not a regular file: {value}")
    return current


def validate(spec, cfg, workspace):
    require(isinstance(spec, dict) and set(spec) == {"steps"}, "Spec must contain only steps")
    rows = spec["steps"]
    limit = cfg["limits"]["max_steps"]
    require(isinstance(rows, list) and 0 < len(rows) <= limit, f"Expected 1..{limit} steps")
    by_id, counts = {}, {}
    directory_ids = set()
    for index, row in enumerate(rows, 1):
        require(isinstance(row, dict), f"Step {index} must be an object")
        unknown = set(row) - STEP_FIELDS
        require(not unknown, f"Step {index}: unknown fields {sorted(unknown)}")
        sid, route_id, brief = (row.get(k) for k in ("id", "route", "brief"))
        require(isinstance(sid, str) and ID.fullmatch(sid), f"Step {index}: invalid id {sid!r}")
        directory_id = os.path.normcase(sid)
        require(directory_id not in directory_ids,
                f"Step {index}: id {sid!r} conflicts with another step directory")
        require(os.name != "nt" or not WINDOWS_DEVICE_ID.fullmatch(sid),
                f"Step {index}: id {sid!r} is a reserved Windows filename")
        directory_ids.add(directory_id)
        require(nonempty(route_id) and route_id in cfg["routes"], f"{sid}: unknown route {route_id!r}")
        require(nonempty(brief), f"{sid}: brief must be non-empty")
        route = cfg["routes"][route_id]
        agent = route["agent"]
        require(cfg["agents"][agent].get("enabled", True), f"{sid}: route {route_id} uses disabled agent {agent}")
        counts[agent] = counts.get(agent, 0) + 1
        cap = cfg["limits"].get("max_calls", {}).get(agent)
        require(cap is None or counts[agent] <= cap, f"{sid}: {agent} exceeds max_calls={cap}")
        task = dict(row)
        for key in ("needs", "refs", "outputs"):
            task[key] = string_list(row.get(key, []), f"{sid}.{key}")
        task["access"] = row.get("access", "read")
        task["kind"] = row.get("kind", "text")
        require(task["access"] in ("read", "write"), f"{sid}: invalid access")
        require(task["kind"] in ("text", "files", "image"), f"{sid}: invalid kind")
        require(task["access"] != "read" or not task["outputs"], f"{sid}: read step cannot declare outputs")
        require(bool(task["outputs"]) == (task["kind"] != "text"),
                f"{sid}: files/image require outputs, text requires an empty outputs list")
        for path in task["refs"] + task["outputs"]:
            artifact_path(workspace, path)
        if task["kind"] == "image":
            require(cfg["agents"][agent]["adapter"] == "codex",
                    f"{sid}: image generation requires a route using the Codex image-capable adapter")
            require(all(p.lower().endswith(".png") for p in task["outputs"]),
                    f"{sid}: image outputs must be PNG files")
        by_id[sid] = task
    for sid, task in by_id.items():
        missing = set(task["needs"]) - set(by_id)
        require(not missing, f"{sid}: unknown dependencies {sorted(missing)}")
    order, ancestors = [], {}
    while len(order) < len(rows):
        sid = next((s for s, t in by_id.items() if s not in ancestors and
                    all(d in ancestors for d in t["needs"])), None)
        require(sid is not None, "Dependency cycle in spec")
        deps = by_id[sid]["needs"]
        ancestors[sid] = set(deps).union(*(ancestors[d] for d in deps))
        order.append(sid)
    writers = {}
    for sid in order:
        task = by_id[sid]
        for path in task["outputs"]:
            previous = writers.get(path)
            require(previous is None or previous in ancestors[sid],
                    f"{sid}: shared output {path} needs an explicit dependency on {previous}")
            writers[path] = sid
    for sid, task in by_id.items():
        for path in task["refs"]:
            exists = artifact_path(workspace, path).is_file()
            upstream = any(path in by_id[d]["outputs"] for d in ancestors[sid])
            require(exists or upstream, f"{sid}: missing ref {path}; declare a producing dependency")
    return [by_id[s] for s in order]


def digest(path):
    if not path.exists():
        return None
    require(path.is_file(), f"Expected a regular file: {path}")
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def check_png(path):
    """Check PNG chunk integrity, header dimensions and decompressed scanline length."""
    import binascii
    def ok(cond):
        require(cond, f"Invalid PNG output: {path.name}")

    with path.open("rb") as f:
        ok(f.read(8) == b"\x89PNG\r\n\x1a\n")
        start = True
        decoded = expected = 0
        data_seen = data_ended = palette_seen = False
        layouts = []
        pass_index = pass_bytes = 0
        inflater = zlib.decompressobj()
        while True:
            prefix = f.read(8)
            ok(len(prefix) == 8)
            length, kind = struct.unpack(">I4s", prefix)
            if start:
                ok(kind == b"IHDR" and length == 13)
            else:
                ok(kind != b"IHDR")
            crc, remaining = binascii.crc32(kind), length
            ok(re.fullmatch(rb"[A-Za-z]{4}", kind) and
               (kind[0] & 32 or kind in (b"IHDR", b"PLTE", b"IDAT", b"IEND")))
            if kind == b"PLTE":
                ok(not palette_seen and not data_seen and color not in (0, 4)
                   and 0 < length <= 768 and length % 3 == 0)
                if color == 3:
                    ok(length // 3 <= 2**bits)
                palette_seen = True
            if kind == b"IDAT":
                ok(not data_ended and (color != 3 or palette_seen))
                data_seen = True
            elif data_seen:
                data_ended = True
            while remaining:
                block = f.read(min(remaining, 65536))
                ok(bool(block))
                remaining -= len(block)
                crc = binascii.crc32(block, crc)
                if kind == b"IHDR":
                    w, h, bits, color, comp, filt, interlace = struct.unpack(">IIBBBBB", block)
                    valid = {0:(1,2,4,8,16), 2:(8,16), 3:(1,2,4,8), 4:(8,16), 6:(8,16)}
                    ok(0 < w < 2**31 and 0 < h < 2**31 and bits in valid.get(color, ())
                       and (comp, filt) == (0, 0) and interlace in (0, 1))
                    bpp = bits * {0:1, 2:3, 3:1, 4:2, 6:4}[color]
                    passes = ((0,0,1,1),) if not interlace else (
                        (0,0,8,8),(4,0,8,8),(0,4,4,8),(2,0,4,4),(0,2,2,4),(1,0,2,2),(0,1,1,2))
                    for x, y, dx, dy in passes:
                        cols, rows = (w-x+dx-1)//dx, (h-y+dy-1)//dy
                        if cols > 0 and rows > 0:
                            stride = 1 + (cols*bpp+7)//8
                            layouts.append((stride, stride*rows))
                            expected += stride*rows
                elif kind == b"IDAT":
                    try:
                        while block:
                            pixels = inflater.decompress(block, 1024*1024)
                            decoded += len(pixels)
                            ok(decoded <= expected and not inflater.unused_data)
                            offset = 0
                            while offset < len(pixels):
                                stride, size = layouts[pass_index]
                                take = min(size - pass_bytes, len(pixels) - offset)
                                first_filter = offset + (-pass_bytes) % stride
                                ok(max(pixels[first_filter:offset + take:stride], default=0) <= 4)
                                offset += take
                                pass_bytes += take
                                if pass_bytes == size:
                                    pass_index += 1
                                    pass_bytes = 0
                            block = inflater.unconsumed_tail
                    except zlib.error:
                        ok(False)
            stored = f.read(4)
            ok(len(stored) == 4 and struct.unpack(">I", stored)[0] == crc & 0xffffffff)
            start = False
            if kind == b"IEND":
                ok(length == 0 and data_seen and inflater.eof and decoded == expected and not f.read(1))
                return


def finish(run, state, step, report):
    sid = step["id"]
    if not isinstance(report, dict) or report.get("status") not in ("success", "failed"):
        report = {"status":"failed", "summary":"Worker returned an invalid result"}
    if not nonempty(report.get("summary")):
        report = dict(report, status="failed", summary="Worker returned an empty summary")
    verified = []
    if report["status"] == "success":
        try:
            for name in step["outputs"]:
                path = artifact_path(Path(state["workspace"]), name)
                fingerprint = digest(path)
                require(fingerprint is not None and path.stat().st_size > 0, f"Missing or empty output: {name}")
                require(fingerprint != step["before"][name], f"Output bytes were unchanged: {name}")
                if step["kind"] == "image":
                    check_png(path)
                verified.append(name)
        except (OSError, ValueError, RuntimeError) as e:
            report = dict(report, status="failed", worker_summary=report["summary"], summary=str(e))
    status = report["status"]
    result = dict(report, id=sid, outputs=verified)
    write_json(run / "steps" / sid / "result.json", result)
    commit_result(run, state, step, result)


def commit_result(run, state, step, result):
    status = result["status"]
    provenance = (result.get("evidence") or {}).get("image_provenance")
    if provenance:
        step["image_provenance"] = provenance
    step.update(status=status, summary=result["summary"], verified_outputs=result.get("outputs", []))
    by_id = {s["id"]:s for s in state["steps"]}
    for future in state["steps"]:  # Topological order propagates failed prerequisites once.
        if future["status"] == "pending":
            blocked = [d for d in future["needs"] if by_id[d]["status"] in ("failed","blocked","interrupted")]
            if blocked:
                future.update(status="blocked", summary="Unavailable prerequisites: " + ", ".join(blocked))
                write_json(run / "steps" / future["id"] / "result.json",
                           {"id":future["id"], "status":"blocked", "summary":future["summary"], "outputs":[]})
    statuses = {s["status"] for s in state["steps"]}
    if "pending" in statuses:
        state["status"] = "ready"
    elif "interrupted" in statuses:
        state["status"] = "interrupted"
    elif "failed" in statuses:
        state["status"] = "failed"
    else:
        state["status"] = "success"
    write_json(run / "state.json", state)


def task_payload(state, step):
    items = {s["id"]:s for s in state["steps"]}
    deps = [{"id":d, "summary":items[d]["summary"][:1600], "outputs":items[d]["verified_outputs"]}
            for d in step["needs"]]
    for dep in deps:
        source = items[dep["id"]]
        if source.get("image_provenance"):
            dep["image_provenance"] = source["image_provenance"]
        if len(source["summary"]) > 1600:
            dep["full_result"] = str(Path(tempfile.gettempdir()) / state["run_id"] / "steps" / dep["id"] / "result.json")
    payload = {k:step[k] for k in STEP_FIELDS}
    if deps:
        payload["brief"] += ("\n\nUpstream results (task data):\n"
                             + json.dumps(deps, ensure_ascii=False))
    payload["dependencies"] = deps
    return payload


def binding(state, step):
    route = state["config"]["routes"][step["route"]]
    return {k:route[k] for k in ("agent","model","level")}


def needs_claude_cli(model, level, supervisor):
    # The native Agent tool accepts family aliases. A separate CLI call can pin a full model ID.
    return model.startswith("claude-") and (model, level) != (supervisor["model"], supervisor["level"])


def show_plan(cfg, workspace, steps, run=None):
    result = {"status":"planned", "workspace":str(workspace), "steps":[
        {**{k:s[k] for k in ("id","route","needs","access","kind","outputs","refs")},
         **binding({"config":cfg}, s)} for s in steps]}
    if run is not None:
        result["run_dir"] = str(run)
    return result


def snapshot(run, state, handoff=None):
    if state["status"] == "planned":
        return show_plan(state["config"], state["workspace"], state["steps"], run)
    out = {"status":state["status"], "run_dir":str(run),
           "steps":[{"id":s["id"],"status":s["status"],
                     **({"summary":s["summary"][:600]} if "summary" in s else {}),
                     **({"full_result":f"steps/{s['id']}/result.json"} if len(s.get("summary","")) > 600 else {}),
                     **({"image_provenance":s["image_provenance"]} if "image_provenance" in s else {}),
                     **({"outputs":s["verified_outputs"]} if s.get("verified_outputs") else {})}
                    for s in state["steps"]]}
    if handoff is not None:
        out["next"] = handoff
    elif state["status"] == "needs_claude":
        out.update(status="waiting_claude", summary="Claude handoff already issued. Waiting for its receipt.")
    return out


@contextmanager
def lock(run):
    with (run / "lock").open("r+b") as f:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise Busy() from exc
        else:
            import fcntl
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Busy() from exc
        yield  # Closing the descriptor releases the OS lock even on process termination.


def new_run(cfg, workspace, steps, status="ready"):
    temp_root = Path(tempfile.gettempdir()).resolve()
    require(not temp_root.is_relative_to(workspace), "System temp is inside workspace; choose a project subdirectory or set TMPDIR outside it")
    run = Path(tempfile.mkdtemp(prefix="steam-run-", dir=temp_root))
    (run / "lock").write_bytes(b"0")
    for step in steps:
        (run / "steps" / step["id"]).mkdir(parents=True)
        step["status"] = "pending"
    state = {"version":1, "run_id":run.name, "workspace":str(workspace), "status":status, "config":cfg, "steps":steps}
    write_json(run / "state.json", state)
    return run


def activate_plan(run, state):
    try:
        cfg = config()
        require(cfg == state["config"], "Configuration changed after this preview")
        from sync_supervisor import check_synced
        check_synced(ROOT, cfg)
    except (OSError, ValueError) as exc:
        raise Invalid(f"{exc}. Update the configuration if needed and preview the plan again.") from exc
    workspace = workspace_path(state["workspace"])
    require(str(workspace) == state["workspace"], "Workspace path changed since the preview")
    spec = {"steps":[{k:s[k] for k in STEP_FIELDS} for s in state["steps"]]}
    steps = validate(spec, cfg, workspace)
    state["steps"] = [dict(s, status="pending") for s in steps]
    state["status"] = "ready"
    write_json(run / "state.json", state)


def advance(run, state):
    if state["status"] == "planned":
        activate_plan(run, state)
    if state["status"] in TERMINAL or state["status"] == "needs_claude":
        return
    if state["status"] == "running":
        # An active peer holds the lock. Reaching here implies an abandoned launch.
        step = next(s for s in state["steps"] if s["status"] == "running")
        message = "Previous launch ended without a committed result. Inspect this step's logs before starting any new run."
        journal = run / "steps" / step["id"] / "result.json"
        if journal.is_file():
            # Crash between writing the verified result and committing state. No relaunch.
            commit_result(run, state, step, read_json(journal))
            if state["status"] in TERMINAL:
                return
        else:
            result = {"id":step["id"], "status":"interrupted", "summary":message}
            write_json(journal, result)
            commit_result(run, state, step, result)
            if state["status"] in TERMINAL:
                return
    workspace = Path(state["workspace"])
    for step in state["steps"]:
        if step["status"] != "pending":
            continue
        route = binding(state, step)
        agent_id = route["agent"]
        agent = state["config"]["agents"][agent_id]
        step_dir = run / "steps" / step["id"]
        try:
            require(workspace.is_dir() and workspace.resolve() == workspace,
                    f"Workspace missing or redirected: {workspace}")
            for ref in step["refs"]:
                require(artifact_path(workspace, ref).is_file(), f"Missing input: {ref}")
            step["before"] = {n:digest(artifact_path(workspace, n)) for n in step["outputs"]}
            task = task_payload(state, step)
            native = agent["adapter"] == "claude" and not needs_claude_cli(
                route["model"], route["level"], state["config"]["supervisor"])
            write_json(step_dir / "task.json", {**task, **route})
            step["status"] = state["status"] = "needs_claude" if native else "running"
            if native:
                step["claim"] = secrets.token_hex(16)
            write_json(run / "state.json", state)
            if native:
                # Only the invocation that committed this handoff receives an executable brief.
                return {**task, **route, "workspace":str(workspace), "step_dir":str(step_dir),
                        "claim":step["claim"]}
            from workers import run_worker
            report = run_worker(agent_id, agent, route["model"], route["level"], task, workspace, step_dir)
        except Exception as e:
            report = {"status":"failed", "summary":f"{type(e).__name__}: {e}"}
        finish(run, state, step, report)
        if state["status"] == "failed":
            return


def native_report(file, step_dir):
    original = sys.stdin.buffer.read().decode("utf-8") if file == "-" else Path(file).read_text(encoding="utf-8")
    text = original.removeprefix("\ufeff").strip()
    require(text, "Claude reply file is empty")
    report = parse_json(text)
    allowed_keys(report, ("status", "summary"), "Claude reply")
    require(report.get("status") in ("success", "failed") and nonempty(report.get("summary")),
            "Claude reply requires status success/failed and a non-empty summary")
    (step_dir / "reply.txt").write_text(original, encoding="utf-8")
    return dict(report, transcript="reply.txt")


def show_catalog(cfg, short=False):
    if short:
        return {"status":"ok", "supervisor":cfg["supervisor"], "limits":cfg["limits"],
                "routes":{k:{"agent":r["agent"], "description":r.get("description","")}
                          for k,r in cfg["routes"].items() if cfg["agents"][r["agent"]].get("enabled",True)}}
    return {"status":"ok", "supervisor":cfg["supervisor"], "limits":cfg["limits"],
            "agents":{a:{"adapter":v["adapter"], "enabled":v.get("enabled", True), "models":v["models"]}
                      for a,v in cfg["agents"].items()},
            "routes":{k:{**r, "enabled":cfg["agents"][r["agent"]].get("enabled", True)}
                      for k,r in cfg["routes"].items()}}


def check(cfg, workspace):
    from workers import check_worker
    results = {}
    for name, agent in cfg["agents"].items():
        if not agent.get("enabled", True):
            results[name] = {"status":"disabled", "summary":"Disabled in config"}
            continue
        models = agent["models"]
        if agent["adapter"] == "claude":
            models = {m: [lv for lv in levels if needs_claude_cli(m, lv, cfg["supervisor"])]
                      for m, levels in models.items()}
            models = {m: levels for m, levels in models.items() if levels}
            if not models:
                results[name] = {"status":"native", "summary":"Handled by the parent Claude session; availability not probed"}
                continue
        try:
            results[name] = check_worker(name, agent, models, workspace)
        except Exception as e:
            results[name] = {"status":"failed", "summary":f"{type(e).__name__}: {e}"}
    return {"status":"ok" if all(r["status"] in ("ok","success","native","disabled","ready")
                                 for r in results.values()) else "attention", "agents":results}


def owned_run(value):
    # A checkout can contain an untrusted state.json with executable settings.
    # Only load private runs directly under the system temp directory.
    original = Path(value).expanduser()
    run = original.resolve(strict=True)
    require(not original.is_symlink() and run.is_dir() and run.parent == Path(tempfile.gettempdir()).resolve()
            and re.fullmatch(r"steam-run-[a-z0-9_]{8}", run.name),
            "Expected a private STEAM run directory directly under system temp")
    if os.name == "posix":
        info = run.stat()
        require(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
                "Run directory must be private and owned by the current user")
    for path in (run / "state.json", run / "lock"):
        require(path.is_file() and not path.is_symlink(), "Invalid run directory")
    return run


def read_state(run):
    state = read_json(run / "state.json")
    require(isinstance(state, dict) and state.get("version") == 1 and state.get("run_id") == run.name,
            "Invalid STEAM run state")
    return state


def cli():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("catalog").add_argument("--short", action="store_true")
    for name in ("check", "plan", "run"):
        c = sub.add_parser(name)
        c.add_argument("--workspace", required=True)
        if name == "plan":
            c.add_argument("--save", action="store_true", help="Save the preview for an exact later continuation")
        if name != "check":
            c.add_argument("--spec", required=True, help="UTF-8 JSON file, or - for stdin")
    for name in ("continue", "result", "complete"):
        c = sub.add_parser(name)
        c.add_argument("--run-dir", required=True)
        if name == "complete":
            c.add_argument("--step", required=True)
            c.add_argument("--claim", help="Claim from the original Claude handoff")
            c.add_argument("--reply", required=True)
    args = p.parse_args()
    if args.cmd in ("catalog", "check", "plan", "run"):
        cfg = config()
        from sync_supervisor import check_synced
        check_synced(ROOT, cfg)
        if args.cmd == "catalog":
            return show_catalog(cfg, args.short)
        workspace = workspace_path(args.workspace)
        if args.cmd == "check":
            return check(cfg, workspace)
        spec = parse_json(sys.stdin.buffer.read().decode("utf-8-sig")) if args.spec == "-" else read_json(args.spec)
        steps = validate(spec, cfg, workspace)
        if args.cmd == "plan":
            run = new_run(cfg, workspace, steps, status="planned") if args.save else None
            return show_plan(cfg, workspace, steps, run)
        run = new_run(cfg, workspace, steps)
        print(f"run_dir={run}", file=sys.stderr, flush=True)
    else:
        run = owned_run(args.run_dir)
    if args.cmd == "result":
        return snapshot(run, read_state(run))
    try:
        with lock(run):
            state = read_state(run)
            handoff = None
            if state["status"] == "needs_claude":
                waiting = next(s for s in state["steps"] if s["status"] == "needs_claude")
                journal = run / "steps" / waiting["id"] / "result.json"
                if journal.is_file():
                    commit_result(run, state, waiting, read_json(journal))
            if args.cmd == "complete":
                step = next((s for s in state["steps"] if s["id"] == args.step), None)
                require(step is not None, f"Unknown step: {args.step}")
                require(step["status"] == "needs_claude", f"{args.step} is {step['status']}; no pending Claude handoff")
                # Handoffs issued before claims were introduced are completed without a token.
                require(step.get("claim") == args.claim, "Use the claim from the original Claude handoff")
                finish(run, state, step, native_report(args.reply, run / "steps" / step["id"]))
            else:
                handoff = advance(run, state)
            return snapshot(run, state, handoff)
    except Busy:
        return {"status":"busy", "run_dir":str(run), "summary":"Another invocation holds this run lock"}


if __name__ == "__main__":
    try:
        response = cli()
    except Exception as exc:
        response = {"status":"error", "summary":f"{type(exc).__name__}: {exc}"}
    sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False, separators=(",",":")) + "\n").encode("utf-8"))
    sys.exit(1 if response["status"] in ("error","failed","interrupted","attention") else 0)
