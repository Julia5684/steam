#!/usr/bin/env python3
"""Validate and execute a STEAM graph. stdout is one JSON object per invocation."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path, PureWindowsPath
import re
import secrets
import struct
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import zlib


sys.dont_write_bytecode = True  # A project-local skill must not generate cache files there.
ROOT = Path(__file__).resolve().parents[1]
WORKBENCH = ".workbench"
RUN_ID = re.compile(r"steam-run-[a-z0-9_]{8}")
STEP_FIELDS = {"id", "route", "brief", "needs", "access", "kind", "outputs", "refs",
               "agent", "model", "level"}
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
# Only these DOS device names can pass the ASCII step ID syntax above.
WINDOWS_DEVICE_ID = re.compile(r"CON|PRN|AUX|NUL|(?:COM|LPT)[1-9]", re.I)
TERMINAL = {"success", "failed", "interrupted"}
STEP_TERMINAL = TERMINAL | {"blocked"}


class Invalid(ValueError):
    pass


class Busy(Exception):
    pass


@dataclass(frozen=True)
class Run:
    path: Path
    legacy: bool = False

    @property
    def state_path(self):
        return self.path / ("state.json" if self.legacy else "run-state.json")

    @property
    def lock_path(self):
        return self.path / ("lock" if self.legacy else "run.lock")

    @property
    def result_name(self):
        return "result.json" if self.legacy else "step-result.json"

    def step_dir(self, step_id):
        path = self.path / "steps" / step_id
        require(self.legacy or (path.is_dir() and path.resolve() == path),
                f"Missing or redirected STEAM step directory: {path}")
        return path

    def result_path(self, step_id):
        return self.step_dir(step_id) / self.result_name


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


def atomic_write(path, text):
    # Exclusive creation protects the temporary file in the worker's workspace.
    temp = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", newline="", suffix=".tmp",
                                         dir=path.parent, delete=False) as f:
            temp = Path(f.name)
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        if temp is not None:
            temp.unlink(missing_ok=True)


def write_json(path, value):
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def timing(record):
    result = {key:record[key] for key in ("created_at", "started_at", "finished_at", "interrupted_at")
              if key in record}
    ended_at = record.get("finished_at") or record.get("interrupted_at")
    if "started_at" in record and (ended_at or record.get("status") not in STEP_TERMINAL):
        started = datetime.fromisoformat(record["started_at"])
        ended = datetime.fromisoformat(ended_at) if ended_at else datetime.now(timezone.utc)
        result["elapsed_seconds"] = max(0, int((ended - started).total_seconds()))
    return result


def progress(steps):
    counts = dict.fromkeys(("pending", "running", "needs_claude", "success", "failed", "interrupted", "blocked"), 0)
    for step in steps:
        counts[step.get("status", "pending")] += 1
    completed = sum(counts[status] for status in STEP_TERMINAL)
    counts["waiting_claude"] = counts.pop("needs_claude")
    return {"total":len(steps), "completed":completed, "remaining":len(steps) - completed,
            "all_settled":completed == len(steps), **counts}


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


def validate(spec, cfg, workspace, *, legacy=False, check_refs=True, concurrent=True):
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
        chosen = {key for key in ("agent", "model", "level") if key in row}
        require(not chosen or len(chosen) == 3, f"{sid}: provide agent, model and level together")
        route = binding(cfg, row)
        agent = route["agent"]
        require(agent in cfg["agents"], f"{sid}: unknown agent {agent}")
        require(route["model"] in cfg["agents"][agent]["models"] and
                route["level"] in cfg["agents"][agent]["models"][route["model"]],
                f"{sid}: unconfigured agent/model/level")
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
        if not legacy:
            for path in task["outputs"]:
                parts = os.path.normcase(path).replace("\\", "/").split("/")
                require(parts[0] != WORKBENCH or len(parts) > 1 and not RUN_ID.fullmatch(parts[1]),
                        f"{sid}: output conflicts with STEAM records: {path}")
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
            key = os.path.normcase(path).replace("\\", "/")
            require(not any(key.startswith(other + "/") or other.startswith(key + "/") for other in writers),
                    f"{sid}: output file conflicts with another output's parent directory: {path}")
            previous = writers.get(key)
            require(previous is None or previous in ancestors[sid],
                    f"{sid}: shared output {path} needs an explicit dependency on {previous}")
            writers[key] = sid
    if concurrent and not legacy:
        for reader_id, reader in by_id.items():
            refs = {os.path.normcase(path).replace("\\", "/") for path in reader["refs"]}
            for writer_id, writer in by_id.items():
                if writer_id == reader_id:
                    continue
                outputs = {os.path.normcase(path).replace("\\", "/") for path in writer["outputs"]}
                require(not refs & outputs or writer_id in ancestors[reader_id] or reader_id in ancestors[writer_id],
                        f"{reader_id}: shared input/output with {writer_id} needs an explicit dependency")
    if check_refs:
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
    result = dict(report, id=sid, outputs=verified, finished_at=timestamp())
    write_json(run.result_path(sid), result)
    commit_result(run, state, step, result)


def commit_result(run, state, step, result):
    status = result["status"]
    provenance = (result.get("evidence") or {}).get("image_provenance")
    if provenance:
        step["image_provenance"] = provenance
    step.update(status=status, summary=result["summary"], verified_outputs=result.get("outputs", []))
    step.update({key:result[key] for key in ("finished_at", "interrupted_at") if key in result})
    by_id = {s["id"]:s for s in state["steps"]}
    for future in state["steps"]:  # Topological order propagates failed prerequisites once.
        if future["status"] == "pending":
            blocked = [d for d in future["needs"] if by_id[d]["status"] in ("failed","blocked","interrupted")]
            if blocked:
                future.update(status="blocked", summary="Unavailable prerequisites: " + ", ".join(blocked),
                              finished_at=timestamp())
                write_json(run.result_path(future["id"]),
                           {"id":future["id"], "status":"blocked", "summary":future["summary"], "outputs":[],
                            "finished_at":future["finished_at"]})
    refresh_status(state)
    write_json(run.state_path, state)

def refresh_status(state):
    statuses = {s["status"] for s in state["steps"]}
    if "running" in statuses:
        state["status"] = "running"
    elif "needs_claude" in statuses:
        state["status"] = "needs_claude"
    elif "pending" in statuses:
        state["status"] = "ready"
    elif "interrupted" in statuses:
        state["status"] = "interrupted"
    elif statuses & {"failed", "blocked"}:
        state["status"] = "failed"
    else:
        state["status"] = "success"
    if state["status"] in TERMINAL:
        state["finished_at"] = timestamp()
    else:
        state.pop("finished_at", None)


def task_payload(run, state, step):
    items = {s["id"]:s for s in state["steps"]}
    deps = [{"id":d, "summary":items[d]["summary"][:1600], "outputs":items[d]["verified_outputs"]}
            for d in step["needs"]]
    for dep in deps:
        source = items[dep["id"]]
        if source.get("image_provenance"):
            dep["image_provenance"] = source["image_provenance"]
        if len(source["summary"]) > 1600:
            dep["full_result"] = str(run.result_path(dep["id"]))
    payload = {k:step[k] for k in STEP_FIELDS if k in step}
    if deps:
        payload["brief"] += ("\n\nUpstream results (task data):\n"
                             + json.dumps(deps, ensure_ascii=False))
    payload["dependencies"] = deps
    return payload


def binding(cfg, step):
    route = cfg["routes"][step["route"]]
    return {k:step.get(k, route[k]) for k in ("agent","model","level")}


def needs_claude_cli(model, level, supervisor):
    # The native Agent tool accepts family aliases. A separate CLI call can pin a full model ID.
    return model.startswith("claude-") and (model, level) != (supervisor["model"], supervisor["level"])


def native_step(cfg, route):
    return cfg["agents"][route["agent"]]["adapter"] == "claude" and not needs_claude_cli(
        route["model"], route["level"], cfg["supervisor"])


def describe_step(cfg, step):
    route = binding(cfg, step)
    return {**{k:step[k] for k in ("id","route","needs","access","kind","refs")},
            "brief":step["brief"][:600], "status":step.get("status", "pending"),
            "dispatch":"native" if native_step(cfg, route) else "cli", **route, **timing(step)}


def step_activity(run, step):
    from workers import read_progress
    activity = read_progress(run.step_dir(step["id"]))
    if not activity:
        return {}
    return {"activity":{"updated_at":activity.get("updated_at"), "latest":activity.get("latest"),
                        "history":f"steps/{step['id']}/progress.jsonl"}}


def stream_event(step, route, event):
    stamp = event.get("time")
    try:
        stamp = datetime.fromisoformat(stamp).astimezone().strftime("%H:%M:%S")
    except (TypeError, ValueError):
        stamp = datetime.now().strftime("%H:%M:%S")
    message = str(event.get("message", "")).strip()
    if message:
        line = f"[{stamp}] {step['id']} · {step['route']} · {route['agent']}  {message}\n"
        try:
            binary = getattr(sys.stderr, "buffer", None)
            if binary is not None:
                binary.write(line.encode("utf-8", errors="replace"))
                binary.flush()
            else:
                sys.stderr.write(line)
                sys.stderr.flush()
        except (OSError, ValueError, UnicodeError):
            pass  # A closed live-output view must not change the persisted worker result.


def show_plan(cfg, workspace, steps, run=None):
    result = {"status":"planned", "workspace":str(workspace), "steps":[
        {**describe_step(cfg, s), "outputs":s["outputs"]} for s in steps],
        "progress":progress(steps)}
    if run is not None:
        result["run_dir"] = str(run.path)
    return result


def snapshot(run, state, handoff=None):
    if state["status"] == "planned":
        return {**show_plan(state["config"], state["workspace"], state["steps"], run), **timing(state)}
    succeeded = {s["id"] for s in state["steps"] if s["status"] == "success"}
    out = {"status":state["status"], "run_dir":str(run.path),
           "workspace":state["workspace"], "progress":progress(state["steps"]), **timing(state),
           "ready_steps":[s["id"] for s in state["steps"] if s["status"] == "pending" and
                          set(s["needs"]) <= succeeded],
           "steps":[{**describe_step(state["config"], s),
                     **step_activity(run, s),
                     "declared_outputs":s["outputs"],
                     **({"summary":s["summary"][:600]} if "summary" in s else {}),
                     **({"full_result":f"steps/{s['id']}/{run.result_name}"} if len(s.get("summary","")) > 600 else {}),
                     **({"image_provenance":s["image_provenance"]} if "image_provenance" in s else {}),
                     **({"outputs":s["verified_outputs"]} if s.get("verified_outputs") else {})}
                    for s in state["steps"]]}
    if handoff is not None:
        out["next"] = handoff
    elif state["status"] == "needs_claude":
        out.update(status="waiting_claude", summary="Claude handoff already issued. Waiting for its receipt.")
    return out


def query_result(run):
    if (run.path / "revision.json").is_file():
        with state_lock(run):
            pass
    state = read_state(run)
    if state.get("concurrent"):
        active, recover = [], []
        for step in state["steps"]:
            if step["status"] == "running":
                try:
                    with lock(run, probe=True, step_id=step["id"]):
                        recover.append(step["id"])
                except Busy:
                    active.append(step["id"])
            elif step["status"] == "needs_claude" and run.result_path(step["id"]).is_file():
                recover.append(step["id"])
        out = snapshot(run, state)
        return {**out, "execution_active":bool(active), "active_steps":active,
                "recovery_required":bool(recover), "recovery_steps":recover}
    active = recover = False
    if state["status"] == "running":
        # An external launch holds the lock until its receipt is committed.
        try:
            with lock(run, probe=True):
                state = read_state(run)
                recover = state["status"] == "running"
        except Busy:
            state = read_state(run)
            active = state["status"] == "running"
    if state["status"] == "needs_claude":
        waiting = next(s for s in state["steps"] if s["status"] == "needs_claude")
        recover = run.result_path(waiting["id"]).is_file()
    out = snapshot(run, state)
    if recover:
        out["summary"] = "Use continue --one-step to reconcile the saved step result."
        if state["status"] == "running":
            out["summary"] = "The previous launch stopped. Use continue --one-step to check its receipt."
            for step in out["steps"]:
                if step["status"] == "running":
                    step.pop("elapsed_seconds", None)
    return {**out, "execution_active":active, "recovery_required":recover}


def wait_result(run, seconds):
    result = query_result(run)
    signature = [(s["id"], s["status"], s.get("activity")) for s in result["steps"]]
    deadline = time.monotonic() + seconds
    while result["execution_active"] and time.monotonic() < deadline:
        time.sleep(min(0.25, max(0, deadline - time.monotonic())))
        result = query_result(run)
        current = [(s["id"], s["status"], s.get("activity")) for s in result["steps"]]
        if current != signature:
            break
    return result


@contextmanager
def lock(run, *, probe=False, step_id=None):
    path = run.step_dir(step_id) / "step.lock" if step_id else run.lock_path
    with path.open("rb" if probe else "r+b") as f:
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(f.fileno(), msvcrt.LK_NBRLCK if probe else msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise Busy() from exc
        else:
            import fcntl
            try:
                mode = fcntl.LOCK_SH if probe else fcntl.LOCK_EX
                fcntl.flock(f, mode | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise Busy() from exc
        yield  # Closing the descriptor releases the OS lock even on process termination.


@contextmanager
def state_lock(run):
    deadline = time.monotonic() + 10
    while True:
        entered = False
        try:
            with lock(run):
                entered = True
                recover_revision(run)
                yield
            return
        except Busy:
            if entered or time.monotonic() >= deadline:
                raise
            time.sleep(0.025)


def new_run(cfg, workspace, steps, status="ready"):
    workbench = workspace / WORKBENCH
    require(not workbench.is_symlink() and workbench.resolve() == workbench,
            f"Redirected STEAM workbench: {workbench}")
    workbench.mkdir(exist_ok=True)
    # Inherit the workspace ACL on Windows so a sandboxed caller retains access.
    while True:
        path = workbench / ("steam-run-" + secrets.token_hex(4))
        try:
            path.mkdir(mode=0o777 if os.name == "nt" else 0o700)
            break
        except FileExistsError:
            continue
    run = Run(path)
    run.lock_path.write_bytes(b"0")
    for step in steps:
        (run.path / "steps" / step["id"]).mkdir(parents=True)
        (run.path / "steps" / step["id"] / "step.lock").write_bytes(b"0")
        step["status"] = "pending"
    state = {"version":2, "run_id":run.path.name, "workspace":str(workspace), "status":status,
             "config":cfg, "steps":steps, "created_at":timestamp(), "concurrent":True}
    write_json(run.state_path, state)
    return run


def saved_spec(state):
    return {"steps":[{k:s[k] for k in STEP_FIELDS if k in s} for s in state["steps"]]}


def compatible_config(state):
    current = config()
    saved = state["config"]
    if current == saved:
        return current
    # Only migrate the two historical AGY defaults, and only when none of this
    # run's actual bindings change. All other configuration drift is rejected.
    normalized = copy.deepcopy(saved)
    models = normalized.get("agents", {}).get("antigravity", {}).get("models", {})
    for model, old_level in (("gemini-pro-orchestrator", "medium"),
                             ("gemini-pro-orchestrator-neon", "high")):
        if models.get(model) == [old_level]:
            models[model] = ["default"]
    for route_id, model, old_level in (("vfx", "gemini-pro-orchestrator", "medium"),
                                       ("perf_analysis", "gemini-pro-orchestrator-neon", "high")):
        route = normalized.get("routes", {}).get(route_id, {})
        if (route.get("agent"), route.get("model"), route.get("level")) == ("antigravity", model, old_level):
            route["level"] = "default"
    require(normalized == current and
            all(binding(saved, step) == binding(current, step) for step in state["steps"]),
            "Configuration changed since this run was saved; preview the affected plan again")
    return current


def activate_plan(run, state):
    try:
        cfg = compatible_config(state)
        from sync_supervisor import check_synced
        check_synced(ROOT, cfg)
    except (OSError, ValueError) as exc:
        raise Invalid(f"{exc}. Update the configuration if needed and preview the plan again.") from exc
    workspace = workspace_path(state["workspace"])
    require(str(workspace) == state["workspace"], "Workspace path changed since the preview")
    steps = validate(saved_spec(state), cfg, workspace, legacy=run.legacy,
                     concurrent=state.get("concurrent", False))
    state["config"] = cfg
    state["steps"] = [dict(s, status="pending") for s in steps]
    state["status"] = "ready"
    write_json(run.state_path, state)


def read_journal(run, step):
    path = run.result_path(step["id"])
    require(run.legacy or not path.is_symlink(), f"Redirected STEAM result: {path}")
    return read_json(path) if path.is_file() else None


def advance(run, state, *, one_step=False, step_id=None, stream=False):
    if step_id is not None:
        selected = next((s for s in state["steps"] if s["id"] == step_id), None)
        require(selected is not None, f"Unknown step: {step_id}")
        if selected["status"] in STEP_TERMINAL:
            return  # Retrying a route wrapper cannot dispatch another step.
        current = next((s for s in state["steps"] if s["status"] in ("running", "needs_claude")), None)
        current = current or next((s for s in state["steps"] if s["status"] == "pending"), None)
        require(current is not None and current["id"] == step_id,
                f"{step_id} cannot run yet; current step is {current['id'] if current else 'none'}")
        one_step = True
    activating = state["status"] == "planned"
    if activating:
        activate_plan(run, state)
    if state["status"] in TERMINAL or state["status"] == "needs_claude":
        return
    if state["status"] == "running":
        # An active peer holds the lock. Reaching here implies an abandoned launch.
        step = next(s for s in state["steps"] if s["status"] == "running")
        message = "Previous launch ended without a committed result. Inspect this step's logs before starting any new run."
        result = read_journal(run, step)
        if result is not None:
            # Crash between writing the verified result and committing state. No relaunch.
            commit_result(run, state, step, result)
            if one_step or state["status"] in TERMINAL:
                return
        else:
            result = {"id":step["id"], "status":"interrupted", "summary":message,
                      "interrupted_at":timestamp()}
            write_json(run.result_path(step["id"]), result)
            commit_result(run, state, step, result)
            if one_step or state["status"] in TERMINAL:
                return
    workspace = Path(state["workspace"])
    if not run.legacy and not activating and any(s["status"] == "pending" for s in state["steps"]):
        cfg = compatible_config(state)
        from sync_supervisor import check_synced
        check_synced(ROOT, cfg)
        steps = validate(saved_spec(state), cfg, workspace, check_refs=False, concurrent=False)
        require([s["id"] for s in steps] == [s["id"] for s in state["steps"]],
                "Saved steps are out of dependency order")
        state["config"] = cfg
    for step in state["steps"]:
        if step["status"] != "pending":
            continue
        route = binding(state["config"], step)
        agent_id = route["agent"]
        agent = state["config"]["agents"][agent_id]
        step["started_at"] = timestamp()
        if "started_at" not in state and all(s["status"] == "pending" for s in state["steps"]):
            state["started_at"] = step["started_at"]
        try:
            step_dir = run.step_dir(step["id"])
            require(workspace.is_dir() and workspace.resolve() == workspace,
                    f"Workspace missing or redirected: {workspace}")
            for ref in step["refs"]:
                require(artifact_path(workspace, ref).is_file(), f"Missing input: {ref}")
            step["before"] = {n:digest(artifact_path(workspace, n)) for n in step["outputs"]}
            task = task_payload(run, state, step)
            native = native_step(state["config"], route)
            write_json(step_dir / ("task.json" if run.legacy else "step-input.json"), {**task, **route})
            step["status"] = state["status"] = "needs_claude" if native else "running"
            if native:
                step["claim"] = secrets.token_hex(16)
            write_json(run.state_path, state)
            if native:
                # Only the invocation that committed this handoff receives an executable brief.
                return {**task, **route, "workspace":str(workspace), "step_dir":str(step_dir),
                        "claim":step["claim"]}
            from workers import run_worker
            on_progress = (lambda event: stream_event(step, route, event)) if stream else None
            report = run_worker(agent_id, agent, route["model"], route["level"], task, workspace, step_dir,
                                on_progress=on_progress)
        except Exception as e:
            report = {"status":"failed", "summary":f"{type(e).__name__}: {e}"}
        finish(run, state, step, report)
        if stream:
            stream_event(step, route, {"time":step.get("finished_at"),
                         "message":f"回执已保存 · {step['status']} · {run.result_path(step['id'])}\n{step['summary'][:600]}"})
        if one_step or state["status"] == "failed":
            return


def concurrent_advance(run, *, one_step=False, step_id=None, stream=False, expected_agent=None):
    """Hold one step lease while workers run; serialize only state transactions."""
    while True:
        with state_lock(run):
            state = read_state(run)
            if state["status"] == "planned":
                activate_plan(run, state)
            if step_id:
                step = next((s for s in state["steps"] if s["id"] == step_id), None)
                require(step is not None, f"Unknown step: {step_id}")
            else:
                completed = {s["id"] for s in state["steps"] if s["status"] == "success"}
                step = next((s for s in state["steps"] if s["status"] == "pending" and
                             set(s["needs"]) <= completed), None)
            if step is None:
                return snapshot(run, state)
            route = binding(state["config"], step)
            require(expected_agent is None or route["agent"] == expected_agent,
                    f"{step['id']}: expected agent {expected_agent}, saved agent is {route['agent']}")
            if step["status"] in STEP_TERMINAL:
                return snapshot(run, state)
            lease = lock(run, step_id=step["id"])
            lease.__enter__()
        try:
            with state_lock(run):
                state = read_state(run)
                step = next(s for s in state["steps"] if s["id"] == step["id"])
                if step["status"] in ("running", "needs_claude"):
                    journal = read_journal(run, step)
                    if journal:
                        commit_result(run, state, step, journal)
                    elif step["status"] == "running":
                        commit_result(run, state, step, {
                            "id":step["id"], "status":"interrupted", "outputs":[],
                            "summary":"Previous launch ended without a committed result. Inspect its logs before retrying.",
                            "interrupted_at":timestamp()})
                    return snapshot(run, state)
                if step["status"] in STEP_TERMINAL:
                    return snapshot(run, state)
                ready = {s["id"] for s in state["steps"] if s["status"] == "success"}
                require(set(step["needs"]) <= ready, f"{step['id']}: dependencies have not succeeded")
                writers = [s for s in state["steps"] if s["id"] != step["id"] and
                           s["status"] in {"running", "needs_claude"} and s["access"] == "write"]
                require(step["access"] != "write" or not writers or
                        step["outputs"] and all(s["outputs"] for s in writers),
                        f"{step['id']}: serialize writes when either step has no declared output files")
                state["config"] = compatible_config(state)
                from sync_supervisor import check_synced
                check_synced(ROOT, state["config"])
                workspace = workspace_path(state["workspace"])
                validate(saved_spec(state), state["config"], workspace, check_refs=False)
                route = binding(state["config"], step)
                require(expected_agent is None or route["agent"] == expected_agent,
                        f"{step['id']}: saved agent changed before dispatch")
                step["started_at"] = timestamp()
                state.setdefault("started_at", step["started_at"])
                step["status"] = "running"
                refresh_status(state)
                write_json(run.state_path, state)
                sid = step["id"]
                try:
                    for ref in step["refs"]:
                        require(artifact_path(workspace, ref).is_file(), f"Missing input: {ref}")
                    step["before"] = {n:digest(artifact_path(workspace, n)) for n in step["outputs"]}
                    task = task_payload(run, state, step)
                    step_dir = run.step_dir(sid)
                    write_json(step_dir / "step-input.json", {**task, **route})
                    if native_step(state["config"], route):
                        step.update(status="needs_claude", claim=secrets.token_hex(16))
                        refresh_status(state)
                        write_json(run.state_path, state)
                        return snapshot(run, state, {**task, **route, "workspace":str(workspace),
                                        "step_dir":str(step_dir), "claim":step["claim"]})
                    write_json(run.state_path, state)
                    setup_error = None
                except Exception as exc:
                    setup_error = {"status":"failed", "summary":f"{type(exc).__name__}: {exc}"}
                cfg = state["config"]
            if setup_error is None:
                try:
                    from workers import run_worker
                    on_progress = (lambda event: stream_event(step, route, event)) if stream else None
                    report = run_worker(route["agent"], cfg["agents"][route["agent"]],
                                        route["model"], route["level"], task, workspace, step_dir,
                                        on_progress=on_progress)
                except KeyboardInterrupt:
                    with state_lock(run):
                        current = read_state(run)
                        selected = next(s for s in current["steps"] if s["id"] == sid)
                        result = {"id":sid, "status":"interrupted", "outputs":[],
                                  "summary":"Execution cancelled by the user.", "interrupted_at":timestamp()}
                        write_json(run.result_path(sid), result)
                        commit_result(run, current, selected, result)
                        return snapshot(run, current)
                except Exception as exc:
                    report = {"status":"failed", "summary":f"{type(exc).__name__}: {exc}"}
            else:
                report = setup_error
            with state_lock(run):
                state = read_state(run)
                step = next(s for s in state["steps"] if s["id"] == sid)
                finish(run, state, step, report)
                if stream:
                    stream_event(step, route, {"time":step.get("finished_at"),
                                 "message":f"回执已保存 · {step['status']} · {run.result_path(sid)}\n{step['summary'][:600]}"})
                result = snapshot(run, state)
            if one_step or step_id or state["status"] in TERMINAL:
                return result
        finally:
            lease.__exit__(None, None, None)


def revise(run, spec):
    """Revise future work, explicitly retrying settled failures with archived receipts."""
    require(not run.legacy, "Legacy runs must continue with their saved plan")
    allowed_keys(spec, ("steps", "retry"), "revision")
    retry = set(string_list(spec.get("retry", []), "retry"))
    with state_lock(run):
        state = read_state(run)
        require(state.get("concurrent"), "This older run supports continuation with its saved plan")
        cfg = compatible_config(state)
        rows = validate({"steps":spec.get("steps")}, cfg, Path(state["workspace"]), check_refs=False)
        state["config"] = cfg
        previous = {s["id"]:s for s in state["steps"]}
        incoming = {s["id"]:s for s in rows}
        require(retry <= previous.keys(), "Retry names an unknown step")
        immutable = [s for s in state["steps"] if s["status"] != "pending" and s["id"] not in retry]
        for step in immutable:
            require(step["id"] in incoming and
                    {k:step[k] for k in STEP_FIELDS if k in step} == incoming[step["id"]],
                    f"{step['id']}: started steps are immutable; retry failures explicitly")
        for sid in retry:
            require(sid in incoming and previous[sid]["status"] in {"failed", "interrupted", "blocked"},
                    f"{sid}: only a settled failed/interrupted/blocked step can be retried")
        for old in state["steps"]:
            if old["status"] == "pending" or old["id"] in retry:
                with lock(run, probe=True, step_id=old["id"]):
                    pass
        # Completed or active work may already depend on a retry candidate.
        for step in immutable:
            require(not (retry & set(step["needs"])),
                    f"{step['id']}: cannot retry a dependency of started work")
        history = state.setdefault("plan_history", [])
        history.append({"revised_at":timestamp(), "steps":state["steps"], "retry":sorted(retry)})
        updated = []
        resets = {}
        for row in rows:
            sid = row["id"]
            old = previous.get(sid)
            if old and old["status"] != "pending" and sid not in retry:
                updated.append(old)
                continue
            directory = run.path / "steps" / sid
            if not directory.exists():
                directory.mkdir()
                (directory / "step.lock").write_bytes(b"0")
            require(directory.resolve() == directory and not directory.is_symlink(),
                    f"Redirected STEAM step directory: {directory}")
            if sid in retry:
                import shutil
                archive = directory / "attempts" / str(len(history))
                archive.mkdir(parents=True, exist_ok=True)
                require(archive.resolve() == archive and not archive.is_symlink(),
                        f"Redirected retry archive: {archive}")
                resets[sid] = []
                for entry in directory.iterdir():
                    if entry.name not in {"step.lock", "attempts"} and entry.is_file():
                        require(not entry.is_symlink(), f"Redirected retry record: {entry}")
                        shutil.copy2(entry, archive / entry.name)
                        resets[sid].append(entry.name)
            updated.append(dict(row, status="pending"))
        state["steps"] = updated
        if state["status"] != "planned":
            refresh_status(state)
        state["revision_id"] = secrets.token_hex(16)
        # Commit the intent after all copies exist. Any later interruption is
        # replayed under run.lock before a step is allowed to dispatch.
        write_json(run.path / "revision.json", {"state":state, "resets":resets})
        recover_revision(run)
        return snapshot(run, state)


def recover_revision(run):
    journal = run.path / "revision.json"
    if not journal.exists():
        return
    require(not journal.is_symlink(), "Redirected revision journal")
    transaction = read_json(journal)
    replacement = transaction["state"]
    require(replacement["run_id"] == run.path.name and
            replacement["workspace"] == str(run.path.parent.parent),
            "Revision belongs to another run")
    current = read_json(run.state_path)
    resets = transaction.get("resets")
    require(isinstance(resets, dict), "Invalid revision cleanup steps")
    current_ids = {step["id"] for step in current.get("steps", [])}
    replacement_ids = {step["id"] for step in replacement.get("steps", [])}
    for sid, names in resets.items():
        require(isinstance(sid, str) and ID.fullmatch(sid) and
                (os.name != "nt" or not WINDOWS_DEVICE_ID.fullmatch(sid)) and
                sid in current_ids and sid in replacement_ids,
                "Invalid revision cleanup step ID")
        require(isinstance(names, list) and all(isinstance(name, str) and Path(name).name == name and
                name not in {"step.lock", "attempts"} for name in names), "Invalid revision cleanup record")
    if current.get("revision_id") != replacement["revision_id"]:
        for sid, names in resets.items():
            directory = run.step_dir(sid)
            for name in names:
                path = directory / name
                require(not path.is_symlink(), f"Redirected retry record: {path}")
                path.unlink(missing_ok=True)
        write_json(run.state_path, replacement)
    journal.unlink()


def concurrent_complete(run, step_id, claim, reply):
    with lock(run, step_id=step_id):
        with state_lock(run):
            state = read_state(run)
            step = next((s for s in state["steps"] if s["id"] == step_id), None)
            require(step is not None, f"Unknown step: {step_id}")
            require(step.get("claim") == claim, "Use the claim from the original Claude handoff")
            journal = read_journal(run, step)
            if step["status"] == "needs_claude" and journal is not None:
                commit_result(run, state, step, journal)
            elif step["status"] == "needs_claude":
                finish(run, state, step, native_report(reply, run, step_id))
            else:
                require(step["status"] in STEP_TERMINAL, f"{step_id}: no pending Claude handoff")
            return snapshot(run, state)


def native_report(file, run, step_id):
    original = (sys.stdin.buffer.read() if file == "-" else Path(file).read_bytes()).decode("utf-8")
    text = original.removeprefix("\ufeff").strip()
    require(text, "Claude reply file is empty")
    report = parse_json(text)
    allowed_keys(report, ("status", "summary"), "Claude reply")
    require(report.get("status") in ("success", "failed") and nonempty(report.get("summary")),
            "Claude reply requires status success/failed and a non-empty summary")
    name = "reply.txt" if run.legacy else "claude-receipt.json"
    atomic_write(run.step_dir(step_id) / name, original)
    return dict(report, transcript=name)


def show_catalog(cfg, short=False):
    if short:
        return {"status":"ok", "supervisor":cfg["supervisor"], "limits":cfg["limits"],
                "agents":{a:{"adapter":v["adapter"], "models":v["models"]}
                          for a,v in cfg["agents"].items() if v.get("enabled", True)},
                "routes":{k:r for k,r in cfg["routes"].items()
                          if cfg["agents"][r["agent"]].get("enabled",True)}}
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
    original = Path(value).expanduser()
    path = original.resolve(strict=True)
    require(not original.is_symlink() and path.is_dir() and RUN_ID.fullmatch(path.name),
            "Expected a STEAM run directory")
    workbench = os.path.normcase(path.parent.name) == WORKBENCH
    legacy = path.parent == Path(tempfile.gettempdir()).resolve() and (
        not workbench or (path / "state.json").is_file())
    if legacy and os.name == "posix":
        info = path.stat()
        require(info.st_uid == os.getuid() and info.st_mode & 0o077 == 0,
                "Run directory must be private and owned by the current user")
    if not legacy:
        require(workbench and Path(os.path.abspath(original)) == path,
                "Expected a STEAM run directly under the project's .workbench")
    run = Run(path, legacy=legacy)
    for record in (run.state_path, run.lock_path):
        require(record.is_file() and not record.is_symlink(), "Invalid run directory")
    return run


def read_state(run):
    state = read_json(run.state_path)
    version = 1 if run.legacy else 2
    require(isinstance(state, dict) and state.get("version") == version and state.get("run_id") == run.path.name,
            "Invalid STEAM run state")
    if not run.legacy:
        require(state.get("workspace") == str(run.path.parent.parent), "Run belongs to a different workspace")
        steps = state.get("steps")
        require(isinstance(steps, list) and steps and all(
            isinstance(step, dict) and isinstance(step.get("id"), str) and ID.fullmatch(step["id"])
            and (os.name != "nt" or not WINDOWS_DEVICE_ID.fullmatch(step["id"])) for step in steps),
            "Invalid step IDs in STEAM run state")
    return state


def input_path(workspace):
    directory = workspace / WORKBENCH / "steam-inputs"
    require(directory.resolve() == directory, f"Redirected STEAM input directory: {directory}")
    directory.mkdir(parents=True, exist_ok=True)
    return str(directory / (secrets.token_hex(16) + ".json"))


def cli():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    catalog = sub.add_parser("catalog")
    catalog.add_argument("--short", action="store_true")
    catalog.add_argument("--workspace", help="Also prepare a fresh JSON input path in this project")
    sub.add_parser("input-path", help="Prepare a fresh path for a JSON plan or receipt").add_argument(
        "--workspace", required=True)
    for name in ("check", "plan", "run"):
        c = sub.add_parser(name)
        c.add_argument("--workspace", required=True)
        if name == "plan":
            c.add_argument("--save", action="store_true", help="Save the preview for an exact later continuation")
        if name != "check":
            c.add_argument("--spec", required=True, help="UTF-8 JSON file, or - for stdin")
    for name in ("continue", "result", "complete", "revise"):
        c = sub.add_parser(name)
        c.add_argument("--run-dir", required=True)
        if name == "continue":
            c.add_argument("--one-step", action="store_true", help="Return after one step or recovered result")
            c.add_argument("--step", help="Execute only this exact next step; settled steps are never relaunched")
            c.add_argument("--stream", action="store_true", help="Stream readable worker activity to stderr")
            c.add_argument("--agent", help="Refuse dispatch if the saved step uses another agent")
        if name == "revise":
            c.add_argument("--spec", required=True, help="Replacement plan with optional retry IDs")
        if name == "result":
            c.add_argument("--wait", type=float, default=0, metavar="SECONDS",
                           help="Wait up to 60 seconds for new activity or a state change")
        if name == "complete":
            c.add_argument("--step", required=True)
            c.add_argument("--claim", help="Claim from the original Claude handoff")
            c.add_argument("--reply", required=True)
    args = p.parse_args()
    if args.cmd == "input-path":
        return {"status":"ok", "path":input_path(workspace_path(args.workspace))}
    if args.cmd in ("catalog", "check", "plan", "run"):
        cfg = config()
        from sync_supervisor import check_synced
        check_synced(ROOT, cfg)
        if args.cmd == "catalog":
            result = show_catalog(cfg, args.short)
            if args.workspace:
                result["input_path"] = input_path(workspace_path(args.workspace))
            return result
        workspace = workspace_path(args.workspace)
        if args.cmd == "check":
            return check(cfg, workspace)
        spec = parse_json(sys.stdin.buffer.read().decode("utf-8-sig")) if args.spec == "-" else read_json(args.spec)
        steps = validate(spec, cfg, workspace)
        if args.cmd == "plan":
            run = new_run(cfg, workspace, steps, status="planned") if args.save else None
            return snapshot(run, read_state(run)) if run is not None else show_plan(cfg, workspace, steps)
        run = new_run(cfg, workspace, steps)
        print(f"run_dir={run.path}", file=sys.stderr, flush=True)
    else:
        run = owned_run(args.run_dir)
    if args.cmd == "result":
        require(0 <= args.wait <= 60, "--wait must be between 0 and 60 seconds")
        return wait_result(run, args.wait)
    if args.cmd == "revise":
        spec = parse_json(sys.stdin.buffer.read().decode("utf-8-sig")) if args.spec == "-" else read_json(args.spec)
        return revise(run, spec)
    try:
        saved = read_state(run)
        if saved.get("concurrent"):
            if args.cmd == "complete":
                return concurrent_complete(run, args.step, args.claim, args.reply)
            return concurrent_advance(run, one_step=args.cmd == "continue" and args.one_step,
                                      step_id=args.step if args.cmd == "continue" else None,
                                      stream=args.cmd == "continue" and args.stream,
                                      expected_agent=args.agent if args.cmd == "continue" else None)
        with lock(run):
            state = read_state(run)
            if args.cmd == "continue" and args.agent:
                require(args.step, "--agent requires --step")
                selected = next((s for s in state["steps"] if s["id"] == args.step), None)
                require(selected is not None and binding(state["config"], selected)["agent"] == args.agent,
                        "Saved step does not use the expected agent")
            handoff = None
            recovered = False
            if state["status"] == "needs_claude":
                waiting = next(s for s in state["steps"] if s["status"] == "needs_claude")
                if args.cmd == "continue" and args.step:
                    selected = next((s for s in state["steps"] if s["id"] == args.step), None)
                    require(selected is not None, f"Unknown step: {args.step}")
                    if selected["status"] in STEP_TERMINAL:
                        return snapshot(run, state)
                    require(waiting["id"] == args.step,
                            f"{args.step} cannot run yet; current step is {waiting['id']}")
                result = read_journal(run, waiting)
                if result is not None:
                    commit_result(run, state, waiting, result)
                    recovered = True
            if args.cmd == "complete":
                step = next((s for s in state["steps"] if s["id"] == args.step), None)
                require(step is not None, f"Unknown step: {args.step}")
                require(step["status"] == "needs_claude", f"{args.step} is {step['status']}; no pending Claude handoff")
                # Handoffs issued before claims were introduced are completed without a token.
                require(step.get("claim") == args.claim, "Use the claim from the original Claude handoff")
                finish(run, state, step, native_report(args.reply, run, step["id"]))
            elif not (args.cmd == "continue" and (args.one_step or args.step) and recovered):
                handoff = advance(run, state, one_step=args.cmd == "continue" and args.one_step,
                                  step_id=args.step if args.cmd == "continue" else None,
                                  stream=args.cmd == "continue" and args.stream)
            return snapshot(run, state, handoff)
    except Busy:
        out = snapshot(run, read_state(run))
        return {**out, "run_status":out["status"], "status":"busy", "execution_active":out["status"] == "running",
                "recovery_required":False, "summary":"Another invocation holds a run or step lock"}


if __name__ == "__main__":
    try:
        response = cli()
    except Exception as exc:
        response = {"status":"error", "summary":f"{type(exc).__name__}: {exc}"}
    sys.stdout.buffer.write((json.dumps(response, ensure_ascii=False, separators=(",",":")) + "\n").encode("utf-8"))
    sys.exit(1 if response["status"] in ("error","failed","interrupted","attention") else 0)
