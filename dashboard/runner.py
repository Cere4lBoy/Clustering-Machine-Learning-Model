"""Run a clustering notebook (v4, the main model, or the older v2) on a new dataset without changing the notebook file.

Each run gets its own folder in outputs/runs/. A copy of the notebook is executed in memory with the run folder as
its working directory, so the notebook reads dataset.csv from there and writes its results into
<run>/outputs/<model folder>/. Only the parameter values in the copy's first code cell are replaced.

For the Step by step tab the copy also gets a few extra cells that keep the cluster labels after every step and
write them to steps.csv. They only record; they are taken out again before the executed copy is saved.
"""
import csv
import json
import re
import time
from datetime import datetime
from pathlib import Path

import nbformat
from nbclient import NotebookClient
from nbclient.exceptions import CellExecutionError

import results as R
from results import ALL_STEP_KEYS, DEFAULT_PARAMS, MODELS, PROJECT, RUNS_DIR, SHARED_CACHE, STEP_KEYS

PARAM_NAMES = ["DATA_FILE", "OUTPUT_FOLDER", "PEOPLE_PER_CLUSTER", "MIN_PEOPLE", "DEMAND_PER_PERSON",
               "MIN_DEMAND_PER_PERSON", "SOLO_MAX_DEMAND", "RADIUS_KM", "MAX_SECONDARY"]
TIMEOUT_S = {"v2": 3600, "v4": 4 * 3600}    # per notebook cell; v4's fit cell runs the whole model several times
# The rules in parameters.csv, in its column order, with the values used where the dashboard doesn't set them.
# None = blank (TOTAL_PEOPLE blank with no worker file: the demand as it is).
RULE_DEFAULTS = {"PEOPLE_PER_CLUSTER": 6, "MIN_PEOPLE": 1, "DEMAND_PER_PERSON": 6, "MIN_DEMAND_PER_PERSON": 4,
                 "SOLO_MAX_DEMAND": 7, "RADIUS_KM": 15, "MAX_SECONDARY": 10, "TOTAL_PEOPLE": None,
                 "MAX_DEMAND_SCALE": 3, "FIT_ROUNDS": 5, "SEED": 67, "BALANCE_TOL": 1.0}

# (step, first line of the notebook cell that finishes it). The v2 lines are in v4 too.
STEP_CELLS = {
    "v2": [
        ("balance", r"^demand_labels, demand_log = demand_layer\("),
        ("distance", r"^labels, distance_log, first_fail = distance_layer\("),
        ("min_demand", r"^labels, (?:fallback, )?min_log, still_low = min_demand_step\("),
        ("teams", r"^labels, (?:fallback, )?people, break_up, team_log = smaller_teams_step\("),
        ("final", r"^labels, people, leftover, leftover_log = leftover_step\("),
    ],
}
STEP_CELLS["v4"] = [*STEP_CELLS["v2"][:4],
                    ("leftovers", STEP_CELLS["v2"][4][1]),
                    ("final", r"^labels, trim_log = trim_people\("),
                    ("fit", r"^set_demand\(chosen\)")]

# ------------------------------------------------------------------------------------------------ v2 recorders
# Runs before the Demand Layer: the two wrappers keep a copy of the labels they are given and then call the
# notebook's own function, so the first split and the repair can be shown without running anything twice.
RECORD_START = '''\
_STEPS, _STEP_PEOPLE = {}, {}
_repair_overloads, _balance_loads = repair_overloads, balance_loads


def repair_overloads(labels, *args, **kwargs):
    _STEPS.setdefault("kmeans", labels.copy())
    return _repair_overloads(labels, *args, **kwargs)


def balance_loads(labels, *args, **kwargs):
    _STEPS["repair"] = labels.copy()
    return _balance_loads(labels, *args, **kwargs)
'''

RECORD_STEP = {
    "balance": '''\
_STEPS["balance"] = demand_labels.copy()
repair_overloads, balance_loads = _repair_overloads, _balance_loads
''',
    "distance": '_STEPS["distance"] = labels.copy()\n',
    "min_demand": '_STEPS["min_demand"] = labels.copy()\n',
    "teams": '''\
_STEPS["teams"] = labels.copy()
_STEP_PEOPLE = dict(people)
''',
    # Road distances of the earlier steps are asked for only here, after the model's own road checks, and the
    # request counters are put back, so the numbers the notebook prints stay the same.
    "final": f'''\
_STEPS["final"] = labels.copy()
try:
    _steps = pd.DataFrame({{"CabinetId": cabinets["CabinetId"].to_numpy(), "cabinetlat": LAT, "cabinetlong": LON,
                           "demand": DEM}})
    for _key in {STEP_KEYS!r}:
        _steps[_key] = _STEPS[_key]
    _steps["people_teams"] = [_STEP_PEOPLE.get(int(p), 0) for p in _STEPS["teams"]]
    _steps["people_final"] = [people[int(p)] for p in labels]
    _steps["leftover"] = leftover
    if "road_to_centre" in globals():
        _steps["fallback"] = fallback
        _stats = dict(OSRM_STATS)
        try:
            for _key in ["balance", "distance", "min_demand", "teams"]:
                _steps["road_" + _key] = np.round(road_to_centre(_STEPS[_key], RADIUS_KM)[0], 4)
            _steps["road_final"] = np.round(road, 4)
        finally:
            OSRM_STATS.update(_stats)
        save_cache_if_due(force=True)
    _steps.to_csv(OUT_DIR / "steps.csv", index=False)
except Exception as _err:
    print(f"step-by-step view: steps not saved ({{type(_err).__name__}}: {{_err}})")
''',
}

# ------------------------------------------------------------------------------------------------ v4 recorders
# v4 may run the whole model again with less demand ("Fit the people available"), so the steps are kept per
# demand scale and the kept run's steps are written at the end. The first run is recorded cell by cell, as in v2;
# the reruns by wrappers around the step functions, put in place just before the fit cell.
RECORD_START_V4 = '''\
_RUN_STEPS, _STEP_DEPTH = {}, [0]
_repair_overloads, _balance_loads = repair_overloads, balance_loads


def _run_steps():
    # the steps of the run being made now, one per demand scale
    return _RUN_STEPS.setdefault(float(DEMAND_SCALE), {})


def repair_overloads(labels, *args, **kwargs):
    _run_steps().setdefault("kmeans", labels.copy())
    return _repair_overloads(labels, *args, **kwargs)


def balance_loads(labels, *args, **kwargs):
    _run_steps()["repair"] = labels.copy()
    return _balance_loads(labels, *args, **kwargs)
'''

RECORD_BEFORE_V4 = {
    "fit": '''\
def _recorded(name, record):
    # Wrap a step function so the reruns are recorded too. Only the outermost call counts, as one step may call
    # another inside.
    inner = globals()[name]

    def wrapper(*args, **kwargs):
        _STEP_DEPTH[0] += 1
        try:
            out = inner(*args, **kwargs)
        finally:
            _STEP_DEPTH[0] -= 1
        if _STEP_DEPTH[0] == 0:
            record(_run_steps(), out)
        return out
    globals()[name] = wrapper


_recorded("demand_layer", lambda s, out: s.update(balance=out[0].copy()))
_recorded("distance_layer", lambda s, out: s.update(distance=out[0].copy()))
_recorded("min_demand_step", lambda s, out: s.update(min_demand=out[0].copy()))
_recorded("smaller_teams_step", lambda s, out: s.update(teams=out[0].copy(), people_teams=dict(out[2])))
_recorded("leftover_step", lambda s, out: s.update(leftovers=out[0].copy(), people_leftovers=dict(out[1])))
_recorded("trim_people", lambda s, out: s.update(final=out[0].copy()))
''',
}

RECORD_STEP_V4 = {
    "balance": '_run_steps()["balance"] = demand_labels.copy()\n',
    "distance": '_run_steps()["distance"] = labels.copy()\n',
    "min_demand": '_run_steps()["min_demand"] = labels.copy()\n',
    "teams": '_run_steps().update(teams=labels.copy(), people_teams=dict(people))\n',
    "leftovers": '_run_steps().update(leftovers=labels.copy(), people_leftovers=dict(people))\n',
    "final": '_run_steps()["final"] = labels.copy()\n',
    # the kept run: labels, people, fallback, leftover and road are its own after the fit cell
    "fit": f'''\
try:
    _s = _RUN_STEPS[float(DEMAND_SCALE)]
    _steps = pd.DataFrame({{"CabinetId": cabinets["CabinetId"].to_numpy(), "cabinetlat": LAT, "cabinetlong": LON,
                           "demand": DEM}})
    for _key in {ALL_STEP_KEYS!r}:
        _steps[_key] = _s[_key]
    _steps["people_teams"] = [_s["people_teams"].get(int(p), 0) for p in _s["teams"]]
    _steps["people_leftovers"] = [_s["people_leftovers"].get(int(p), 0) for p in _s["leftovers"]]
    _steps["people_final"] = [people[int(p)] for p in labels]
    _steps["leftover"] = leftover
    _steps["fallback"] = fallback
    _stats = dict(OSRM_STATS)
    try:
        for _key in ["balance", "distance", "min_demand", "teams"]:
            _steps["road_" + _key] = np.round(road_to_centre(_s[_key], RADIUS_KM)[0], 4)
        _steps["road_leftovers"] = np.round(road_to_centre(_s["leftovers"], np.inf, leftover | fallback)[0], 4)
        _steps["road_final"] = np.round(road, 4)
    finally:
        OSRM_STATS.update(_stats)
    save_cache_if_due(force=True)
    _steps.to_csv(OUT_DIR / "steps.csv", index=False)
except Exception as _err:
    print(f"step-by-step view: steps not saved ({{type(_err).__name__}}: {{_err}})")
''',
}

RECORDERS = {"v2": (RECORD_START, RECORD_STEP, {}), "v4": (RECORD_START_V4, RECORD_STEP_V4, RECORD_BEFORE_V4)}


def new_run(df, file_name, params, model="v2"):
    # Create the run folder with the dataset and run.json; returns its path.
    slug = re.sub(r"[^A-Za-z0-9_-]+", "-", Path(file_name).stem).strip("-")[:40] or "dataset"
    run_dir = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}_{slug}"
    run_dir.mkdir(parents=True)
    # a partition column is left out: the dashboard plans the whole file together (PartitionPipeline.ipynb plans
    # every partition on its own)
    df.drop(columns=[c for c in df.columns if str(c).strip().lower() == "partition"]).to_csv(run_dir / "dataset.csv",
                                                                                         index=False)
    info = {"created": f"{datetime.now():%Y-%m-%d %H:%M}", "dataset_name": Path(file_name).name,
            "cabinets": int(len(df)), "params": {**DEFAULT_PARAMS, **MODELS[model]["params"], **params},
            "model": model, "models": {}}
    _write_json(run_dir / "run.json", info)
    return run_dir


def patched_copy(model, params, folder):
    # The notebook as an in-memory copy with the parameter values replaced (the file is not touched). A notebook
    # that reads its rules from a parameters file (ClusteringModel.ipynb) gets them in <folder>/parameters.csv, as
    # the one partition ALL.
    nb = nbformat.read(MODELS[model]["notebook"], as_version=4)
    cell = next(c for c in nb.cells if c.cell_type == "code")
    values = {"DATA_FILE": "dataset.csv", "OUTPUT_FOLDER": f"outputs/{MODELS[model]['subdir']}", **DEFAULT_PARAMS,
              **MODELS[model]["params"], **params}
    source = cell.source
    names = PARAM_NAMES + list(MODELS[model]["params"])
    if re.search(r"^PARAMETER_FILE\s*=", source, flags=re.M):
        rules = {**RULE_DEFAULTS, **{n: values[n] for n in names if n in RULE_DEFAULTS}}
        with open(Path(folder) / "parameters.csv", "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["partition", *rules])
            writer.writerow(["ALL", *("" if v is None else v for v in rules.values())])
        values.update(PARAMETER_FILE="parameters.csv", PARTITION=None, WORKER_FILE=None)
        names = ["DATA_FILE", "OUTPUT_FOLDER", "PARAMETER_FILE", "PARTITION", "WORKER_FILE"]
    for name in names:
        source, n = re.subn(rf"^{name}\s*=.*$", f"{name} = {values[name]!r}", source, flags=re.M)
        if n != 1:
            raise RuntimeError(f"parameter {name} not found in {MODELS[model]['notebook'].name}")
    cell.source = source
    return nb


def _set_values(nb, values):
    # Replace these parameters in the first code cell of a notebook copy.
    cell = next(c for c in nb.cells if c.cell_type == "code")
    for name, value in values.items():
        cell.source, n = re.subn(rf"^{name}\s*=.*$", lambda _: f"{name} = {value!r}", cell.source, flags=re.M)
        if n != 1:
            raise RuntimeError(f"parameter {name} not found in the notebook")


def _project_path(path):
    # A path as the notebooks take it: relative to the project folder where possible.
    try:
        return Path(path).resolve().relative_to(PROJECT).as_posix()
    except ValueError:
        return str(path)


def with_step_cells(nb, model="v2"):
    # Add the recording cells to a notebook copy. If the notebook's step cells are not found, the copy is returned
    # as it is: the run then simply has no step-by-step view.
    start, after, before = RECORDERS[model]
    code = [i for i, c in enumerate(nb.cells) if c.cell_type == "code"]
    found = [next((i for i in code if re.search(pattern, nb.cells[i].source, flags=re.M)), None)
             for _, pattern in STEP_CELLS[model]]
    if None in found or found != sorted(set(found)):
        return nb

    def recorder(source):
        return nbformat.v4.new_code_cell(source, metadata={"step_view": True})

    for (step, _), i in reversed(list(zip(STEP_CELLS[model], found))):
        nb.cells.insert(i + 1, recorder(after[step]))
        if step in before:
            nb.cells.insert(i, recorder(before[step]))
    nb.cells.insert(found[0], recorder(start))
    return nb


def build_steps(result_set, model, progress=None, timeout=None):
    # Record the steps for results that have none yet (a notebook's own results, or a partition). The copy is cut
    # off right after the last recording cell, so it writes steps.csv and none of the result files.
    # Returns {"ok", "error", "seconds"}.
    if result_set.pipeline:    # one partition of a PartitionPipeline run: the rules it was planned with (rules.csv)
        info, part = result_set.pipeline, result_set.model_dir(model)
        nb, folder = nbformat.read(MODELS[model]["notebook"], as_version=4), PROJECT
        _set_values(nb, {"DATA_FILE": info["CABINET_FILE"], "OUTPUT_FOLDER": _project_path(part),
                         "PARAMETER_FILE": _project_path(part / "rules.csv"), "PARTITION": result_set.partition,
                         "WORKER_FILE": None, "CACHE_FILE": info.get("CACHE_FILE")})
    elif not result_set.run:   # a notebook's own results: the notebook as it is, run from the project folder
        nb, folder = nbformat.read(MODELS[model]["notebook"], as_version=4), PROJECT
    else:
        folder = result_set.outputs.parent
        info = json.loads((folder / "run.json").read_text(encoding="utf-8"))
        nb = patched_copy(model, info["params"], folder)
    nb = with_step_cells(nb, model)
    recorders = [i for i, c in enumerate(nb.cells) if c.metadata.get("step_view")]
    if not recorders:
        return {"ok": False, "error": "the notebook's steps were not recognised", "seconds": 0}
    nb.cells = nb.cells[:recorders[-1] + 1]
    target = result_set.model_dir(model) / "steps.csv"
    before = target.stat().st_mtime if target.exists() else None
    if result_set.run:
        seed_cache(result_set.model_dir(model) / "cache" / "osrm_cache.json", model)
    ok, error, seconds = _execute(nb, folder, model, progress, timeout or TIMEOUT_S[model])
    if result_set.run:
        merge_cache(result_set.model_dir(model) / "cache" / "osrm_cache.json")
    if ok and (not target.exists() or target.stat().st_mtime == before):
        printed = "".join("".join(o.get("text", "")) for o in nb.cells[-1].get("outputs", []))
        ok, error = False, printed.strip() or "steps.csv was not written"
    return {"ok": ok, "error": error, "seconds": seconds}


def _execute(nb, folder, model, progress, timeout):
    # Execute a notebook copy with `folder` as its working directory. progress(fraction, text) is called after
    # every code cell of the notebook itself. Returns (ok, error, seconds).
    code_cells = [i for i, c in enumerate(nb.cells) if c.cell_type == "code" and not c.metadata.get("step_view")]

    def on_cell_executed(cell_index=None, **_):
        if progress and cell_index in code_cells:
            done = code_cells.index(cell_index) + 1
            progress(done / len(code_cells), f"{MODELS[model]['name']}: step {done} of {len(code_cells)}")

    client = NotebookClient(nb, timeout=timeout, kernel_name="python3",
                            resources={"metadata": {"path": str(folder)}}, on_cell_executed=on_cell_executed)
    start, ok, error = time.time(), True, None
    try:
        client.execute()
    except CellExecutionError as err:
        ok, error = False, f"{getattr(err, 'ename', 'Error')}: {getattr(err, 'evalue', str(err))}"
    except Exception as err:   # e.g. the kernel could not start
        ok, error = False, f"{type(err).__name__}: {err}"
    return ok, error, round(time.time() - start)


def run_model(run_dir, model, progress=None, timeout=None):
    # Execute one notebook copy for this run. progress(fraction, text) is called after every code cell.
    run_dir = Path(run_dir)
    info = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    nb = with_step_cells(patched_copy(model, info["params"], run_dir), model)
    run_cache = run_dir / "outputs" / MODELS[model]["subdir"] / "cache" / "osrm_cache.json"
    seed_cache(run_cache, model)

    ok, error, seconds = _execute(nb, run_dir, model, progress, timeout or TIMEOUT_S[model])
    nb.cells = [c for c in nb.cells if not c.metadata.get("step_view")]
    nbformat.write(nb, run_dir / f"executed_{model}.ipynb")

    printed = "\n".join("".join(o.get("text", "")) + "".join(o.get("data", {}).get("text/plain", ""))
                        for c in nb.cells for o in c.get("outputs", []))
    facts = R.printed_facts(printed)
    result = {"ok": ok, "error": error, "seconds": seconds, "osrm_requests": facts["osrm_requests"]}
    if model == "v4":
        result.update(people_saved=facts["people_saved"], fit_runs=facts["fit_runs"])
    result["cache_entries_added"] = merge_cache(run_cache)
    info["models"][model] = result
    _write_json(run_dir / "run.json", info)
    return result


def seed_cache(run_cache, model):
    # Start a run's road-distance cache with the distances fetched before: the shared cache and, for v4, the
    # cache of the v4 notebook's own runs. Nothing is written if there is nothing saved yet.
    sources = [p for p in [SHARED_CACHE, MODELS[model].get("own_cache")] if p is not None and Path(p).exists()]
    if not sources:
        return
    run_cache = Path(run_cache)
    run_cache.parent.mkdir(parents=True, exist_ok=True)
    if len(sources) == 1:
        run_cache.write_bytes(Path(sources[0]).read_bytes())
        return
    merged = {"road_km": {}, "snap_km": {}}
    for source in sources:
        saved = json.loads(Path(source).read_text(encoding="utf-8"))
        for part in ("road_km", "snap_km"):
            merged.setdefault(part, {}).update(saved.get(part, {}))
    _write_json(run_cache, merged, indent=None)


def merge_cache(run_cache):
    # Copy road distances fetched during a run back into the shared cache, so later runs reuse them.
    if not Path(run_cache).exists():
        return 0
    new = json.loads(Path(run_cache).read_text(encoding="utf-8"))
    shared = (json.loads(SHARED_CACHE.read_text(encoding="utf-8")) if SHARED_CACHE.exists()
              else {"road_km": {}, "snap_km": {}})
    added = 0
    for part in ("road_km", "snap_km"):
        for key, value in new.get(part, {}).items():
            if key not in shared.setdefault(part, {}):
                shared[part][key] = value
                added += 1
    if added:
        SHARED_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _write_json(SHARED_CACHE, shared, indent=None)
    return added


def _write_json(path, data, indent=1, attempts=6):
    # Write via a temporary file; retry briefly if a synced folder (OneDrive) has the file locked.
    tmp = Path(path).with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=indent), encoding="utf-8")
    for attempt in range(attempts):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5 * 2 ** attempt)
