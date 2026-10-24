"""Loading model results, computing metrics and checking uploaded datasets.

Two models can be used: the main model ClusteringModel.ipynb (v4, road first with the demand fitted to the people
available) and the older road-first model (v2, previous_versions/). The straight-line model (v3) is retired. The
results of PartitionPipeline.ipynb (every partition planned on its own) are listed too, one result set per partition.
The notebooks are never changed by the dashboard: results are read from the files they write.
"""
import ast
import json
import math
import re
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import shapely
import shapely.ops
from scipy.spatial import Delaunay

PROJECT = Path(__file__).resolve().parents[1]
MAIN_NOTEBOOK = PROJECT / "ClusteringModel.ipynb"
PREVIOUS_DIR = PROJECT / "previous_versions"
MOCK_DATA = PROJECT / "Dataset" / "mock_cabinets.csv"
RUNS_DIR = PROJECT / "outputs" / "runs"
SHARED_CACHE = PROJECT / "outputs" / "cache" / "osrm_cache.json"   # also the main notebook's own cache
PUBLIC_OSRM = "router.project-osrm.org"                # the public demo server
SECONDS_PER_REQUEST = {"public": 1.9, "own": 0.14}     # whole run time / requests, measured on a 1,564-cabinet run

BLUE, ORANGE = "#1800e1", "#fb5e00"   # Telekom Malaysia blue and orange
MODELS = {   # the straight-line model (v3) is retired: its old result files are left on disk, but not shown
    # v4, the main model = v2 + TOTAL_PEOPLE: demand scaled down or up to fit the people available, Save people,
    # Fit the people available
    "v4": {"name": "Main model (v4)", "short": "v4", "color": BLUE,
           "notebook": MAIN_NOTEBOOK, "subdir": "v4",
           "about": "Road first, with the demand fitted up or down to the people available.",
           "params": {"TOTAL_PEOPLE": None, "FIT_ROUNDS": 5, "MAX_DEMAND_SCALE": 3}},
    "v2": {"name": "Older model (v2)", "short": "v2", "color": ORANGE,
           "notebook": PREVIOUS_DIR / "two_layer_clustering_v2_road.ipynb", "subdir": "v2_road",
           "about": "The older model: radius by road, straight line as fallback, no fitting to the people.",
           "params": {}},
}
# the rules when nothing else gives them (the ALL row of parameters.csv)
DEFAULT_PARAMS = {"PEOPLE_PER_CLUSTER": 6, "MIN_PEOPLE": 1, "DEMAND_PER_PERSON": 6, "MIN_DEMAND_PER_PERSON": 4,
                  "SOLO_MAX_DEMAND": 7, "RADIUS_KM": 15, "MAX_SECONDARY": 10}
REQUIRED_COLUMNS = ["CabinetId", "cabinetlat", "cabinetlong"]
CABINET_COLUMNS = REQUIRED_COLUMNS + ["demand", "total_tickets", "total_days", "partition"]   # names the notebooks use
RULE_LABELS = {"ok": "Main rule", "road": "Main rule", "straight-line fallback": "Straight-line fallback",
               "leftover": "Leftover"}
# Step by step tab: the cluster-label columns of steps.csv, in the order the model works. v4 also has
# "leftovers" (after the leftover step); its "final" is after Save people.
STEP_KEYS = ["kmeans", "repair", "balance", "distance", "min_demand", "teams", "final"]
ALL_STEP_KEYS = ["kmeans", "repair", "balance", "distance", "min_demand", "teams", "leftovers", "final"]
FLAG_STEPS = ("leftovers", "final")     # steps that show the model's own leftover and fallback flags
# Cluster colours of the notebooks' maps, without red: red marks what breaks a rule
STEP_PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#4a3aa7"]
OUTLINE_PAD_DEG = 0.0018                # about 200 m, so one- and two-cabinet clusters show (as on the notebooks' maps)
EARTH_RADIUS_KM = 6371.0088


# ----------------------------------------------------------------------------------------------- result sets
@dataclass
class ResultSet:
    key: str
    label: str
    dataset: Path
    outputs: Path          # the run's outputs folder; v2's files are in outputs / "v2_road", v4's in outputs / "v4"
    params: dict
    facts: dict            # per model: seconds, osrm_requests, ok, error (v4 also people_saved, fit_runs)
    created: str = ""
    model: str = "v2"      # the model these results come from
    dataset_name: str = ""
    run: bool = False      # a run of the New run tab (has its own folder and run.json)
    folder: Path | None = None   # the model's files, if not in outputs / subdir (the v4 notebook's own results)
    partition: str | None = None   # one partition of a PartitionPipeline run: only its cabinets in `dataset`
    group: str | None = None       # that run's folder (outputs/<group>), shared by its partitions
    pipeline: dict | None = None   # that run's pipeline.json

    def model_dir(self, model):
        return self.folder if self.folder is not None else self.outputs / MODELS[model]["subdir"]


def run_params(stored):
    # The rules of a run (v4 runs also store TOTAL_PEOPLE and FIT_ROUNDS), with the defaults for any it doesn't give.
    return {**DEFAULT_PARAMS, **stored}


def team_band(params, people):
    # (minimum, maximum) tickets/day for a team of this many people.
    return people * params["MIN_DEMAND_PER_PERSON"], people * params["DEMAND_PER_PERSON"]


def team_list(clusters):
    # "C001 : 3 People" for every cluster, in cluster order.
    return [f"{c} : {n} {'Person' if n == 1 else 'People'}" for c, n in zip(clusters["cluster_id"], clusters["people"])]


def printed_facts(text):
    # The numbers a model prints in its summary: run time and OSRM requests; v4 also the people saved by Save
    # people and the extra runs to fit TOTAL_PEOPLE. Missing ones are None.
    patterns = {"seconds": r"Run time \(s\)\s+(\d+)", "osrm_requests": r"OSRM \(driving\), (\d+) requests this run",
                "people_saved": r"People saved by Save people\s+(\d+)",
                "fit_runs": r"Extra runs to fit TOTAL_PEOPLE\s+(\d+)"}
    return {key: int(m.group(1)) if (m := re.search(pattern, text)) else None for key, pattern in patterns.items()}


def _notebook_facts(path):
    # The printed facts in a notebook's saved summary output (read only), and whether a cell stopped with an error.
    facts = {**printed_facts(""), "ok": True, "error": None}
    try:
        nb = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return facts
    text = "\n".join("".join(o.get("text", "")) + "".join(o.get("data", {}).get("text/plain", ""))
                     for c in nb.get("cells", []) for o in c.get("outputs", []))
    facts.update(printed_facts(text))
    errors = [o for c in nb.get("cells", []) for o in c.get("outputs", []) if o.get("output_type") == "error"]
    if errors:
        facts["ok"], facts["error"] = False, f"{errors[0].get('ename')}: {errors[0].get('evalue')}"
    return facts


def notebook_params(model="v2", names=None):
    # The rules in a notebook's parameters cell (read only), so its own results are judged by the numbers they were
    # made with. A value that can't be read keeps its default.
    params = {**DEFAULT_PARAMS, **MODELS[model]["params"]}
    try:
        nb = json.loads(MODELS[model]["notebook"].read_text(encoding="utf-8"))
        source = "".join(next(c for c in nb["cells"] if c["cell_type"] == "code")["source"])
    except (OSError, ValueError, StopIteration):
        return params
    found = {}
    for name in ["PARAMETER_FILE", "PARTITION", "DATA_FILE", "WORKER_FILE", *(names or params)]:
        if m := re.search(rf"^{name}\s*=\s*([^#\n]+)", source, flags=re.M):
            try:
                found[name] = ast.literal_eval(m.group(1).strip())
            except (ValueError, SyntaxError):
                pass
    params.update({n: v for n, v in found.items() if n in (names or params)})
    if found.get("PARAMETER_FILE") and names is None:   # the rules are in a parameters file (ClusteringModel.ipynb)
        params.update(file_rules(found))
    return params


def file_rules(found):
    # The rules the main notebook uses from its parameters file: the row of its partition (PARTITION, else the data
    # file's only partition, else ALL). A blank TOTAL_PEOPLE is the partition's workers in WORKER_FILE. Rules that
    # can't be read keep their defaults.
    try:
        table = pd.read_csv(PROJECT / found["PARAMETER_FILE"], dtype=str).fillna("")
        table.index = table[next(c for c in table.columns if c.strip().lower() == "partition")].str.strip()
        partition = found.get("PARTITION")
        if partition is None:
            data = pd.read_csv(PROJECT / found.get("DATA_FILE", ""), dtype=str)
            col = next((c for c in data.columns if c.strip().lower() == "partition"), None)
            names = [] if col is None else data[col].fillna("").str.strip().unique()
            partition = names[0] if len(names) == 1 else "ALL"
        row = table.loc[partition]
    except (OSError, ValueError, KeyError, StopIteration):
        return {}
    rules = {}
    for name in [*DEFAULT_PARAMS, *MODELS["v4"]["params"]]:
        text = str(row.get(name, "")).strip()
        try:
            value = float(text) if text else None
        except ValueError:
            continue
        rules[name] = int(value) if value is not None and value == int(value) else value
    if rules.get("SOLO_MAX_DEMAND") is None and rules.get("DEMAND_PER_PERSON") is not None:
        rules["SOLO_MAX_DEMAND"] = rules["DEMAND_PER_PERSON"]
    if rules.get("TOTAL_PEOPLE") is None and found.get("WORKER_FILE"):
        try:
            workers = pd.read_csv(PROJECT / found["WORKER_FILE"], dtype=str)
            col = next((c for c in workers.columns if c.strip().lower() == "partition"), None)
            rules["TOTAL_PEOPLE"] = (len(workers) if col is None or partition == "ALL"
                                     else int((workers[col].fillna("").str.strip() == partition).sum()))
        except (OSError, ValueError):
            pass
    for name in ["MAX_DEMAND_SCALE", "FIT_ROUNDS"]:   # blank = the notebook's own default
        if rules.get(name) is None:
            rules.pop(name, None)
    return {n: v for n, v in rules.items() if v is not None or n in ("MAX_SECONDARY", "TOTAL_PEOPLE")}


def list_result_sets():
    # The main notebook's latest results (always listed first, so the dashboard also opens before anything has
    # run), the v2 notebook's results if it was run, then every dashboard run (newest first).
    files = notebook_params("v4", ["DATA_FILE", "OUTPUT_FOLDER", "PARTITION"])
    main_folder = PROJECT / files.get("OUTPUT_FOLDER", "outputs")
    main_data = PROJECT / files.get("DATA_FILE", "")
    if not main_data.is_file():     # its data file is not here: summarise the mock file instead
        main_data = MOCK_DATA
    sets = [ResultSet(key="main", label=f"{main_data.name} · latest ClusteringModel results",
                      dataset=main_data, outputs=main_folder, params=notebook_params("v4"), folder=main_folder,
                      facts={"v4": _notebook_facts(MAIN_NOTEBOOK)}, model="v4", dataset_name=main_data.name,
                      partition=files.get("PARTITION"))]   # the notebook plans one partition of its data file
    files = notebook_params("v2", ["DATA_FILE", "OUTPUT_FOLDER"])
    v2_folder = PROJECT / files.get("OUTPUT_FOLDER", "outputs/v2_road")
    v2_data = PROJECT / files.get("DATA_FILE", "")
    if (v2_folder / "clusters.csv").exists() and v2_data.is_file():
        sets.append(ResultSet(key="v2_notebook", label=f"{v2_data.name} · latest v2 notebook results",
                              dataset=v2_data, outputs=v2_folder, params=notebook_params("v2"), folder=v2_folder,
                              facts={"v2": _notebook_facts(MODELS["v2"]["notebook"])}, model="v2",
                              dataset_name=v2_data.name))
    sets += pipeline_sets()
    if RUNS_DIR.exists():
        for run in sorted(RUNS_DIR.iterdir(), reverse=True):
            info_file = run / "run.json"
            if not info_file.exists():
                continue
            info = json.loads(info_file.read_text(encoding="utf-8"))
            model = next((m for m in info.get("models", {}) if m in MODELS), None)
            if model is None:     # never run, or only with the retired straight-line model
                continue
            sets.append(ResultSet(key=run.name,
                                  label=f"{info.get('created', run.name)} · {info.get('dataset_name', '?')} · "
                                        f"{MODELS[model]['short']}",
                                  dataset=run / "dataset.csv", outputs=run / "outputs",
                                  params=run_params(info.get("params", {})),
                                  facts=info.get("models", {}), created=info.get("created", ""), model=model,
                                  dataset_name=info.get("dataset_name", "?"), run=True))
    return sets


def rules_params(path):
    # The rules a partition was planned with (its rules.csv), under the names of the parameters.
    row = pd.read_csv(path).iloc[0]
    params = {}
    for name in [*DEFAULT_PARAMS, *MODELS["v4"]["params"]]:
        value = row.get(name)
        if value is None or pd.isna(value):
            params[name] = None
        else:
            params[name] = int(value) if float(value).is_integer() else float(value)
    return run_params({n: v for n, v in params.items() if v is not None or n in ("MAX_SECONDARY", "TOTAL_PEOPLE")})


def pipeline_sets():
    # One result set per partition of every PartitionPipeline run (outputs/<folder>/combined/pipeline.json),
    # newest first.
    sets = []
    runs = [(json.loads(f.read_text(encoding="utf-8")), f.parent.parent)
            for f in (PROJECT / "outputs").glob("*/combined/pipeline.json")]
    for info, folder in sorted(runs, key=lambda r: r[0].get("created", ""), reverse=True):
        data = PROJECT / info["CABINET_FILE"]
        if not data.is_file():   # its cabinet file has been moved: the run can't be shown
            continue
        for p in info.get("partitions", []):
            part = folder / p
            if not ((part / "clusters.csv").exists() and (part / "rules.csv").exists()):
                continue
            sets.append(ResultSet(key=f"{folder.name}__{p}", label=f"{data.name} · partition {p}", dataset=data,
                                  outputs=part, params=rules_params(part / "rules.csv"),
                                  facts={"v4": _notebook_facts(part / "executed_clustering.ipynb")},
                                  created=info.get("created", ""), model="v4", dataset_name=data.name, folder=part,
                                  partition=p, group=folder.name, pipeline={**info, "folder": str(folder)}))
    return sets


def load_partition_run(folder):
    # The combined tables of a PartitionPipeline run (outputs/<folder>/combined) as one dict, with the cabinets'
    # positions and each partition's rules. Assignments and clusters get the same extra columns as load_model.
    folder = Path(folder)
    info = json.loads((folder / "combined" / "pipeline.json").read_text(encoding="utf-8"))
    ids = {"CabinetId": str, "cluster_id": str, "primary_cluster_id": str, "staff_id": str, "node": str,
           "staff_ids": str, "nodes": str, "partition": str, "type": str, "status": str}
    out = {"info": info}
    for name in ["summary", "clusters", "assignments", "secondaries", "worker_assignments", "cluster_workers",
                 "worker_cabinets"]:
        path = folder / "combined" / f"{name}.csv"
        out[name] = (pd.read_csv(path, dtype=ids, keep_default_na=False, na_values=[""]) if path.exists()
                     else pd.DataFrame())
    a, c = out["assignments"], out["clusters"]
    a["straight_km"], a["road_km"] = a["straight_dist_to_centre_km"], a["road_dist_to_centre_km"]
    a["rule"] = a["distance_rule"].map(RULE_LABELS).fillna(a["distance_rule"])
    c["max_straight_km"], c["max_road_km"] = c["max_straight_dist_to_centre_km"], c["max_road_dist_to_centre_km"]
    for name in ["worker_assignments", "cluster_workers"]:
        for col in ["cluster_id", "staff_ids", "nodes"]:
            if col in out[name]:
                out[name][col] = out[name][col].fillna("")
    cab = read_cabinets(PROJECT / info["CABINET_FILE"])
    cab["partition"] = cab["partition"].astype(str).str.strip()
    out["cabinets"] = cab[["partition", "CabinetId", "cabinetlat", "cabinetlong", "demand"]]
    out["rules"] = {p: rules_params(folder / p / "rules.csv") for p in info.get("partitions", [])
                    if (folder / p / "rules.csv").exists()}
    return out


# ----------------------------------------------------------------------------------------------- model results
@dataclass
class ModelResult:
    model: str
    clusters: pd.DataFrame
    assignments: pd.DataFrame   # normalised: straight_km, road_km, rule (Main rule / ... )
    map_path: Path
    secondaries: pd.DataFrame | None = None   # normalised: straight_km, road_km; None if there is no file


def load_model(result_set, model):
    # None if this model was not run (or its files are missing).
    folder = result_set.model_dir(model)
    files = [folder / "clusters.csv", folder / "assignments.csv", folder / "map.html"]
    if not all(f.exists() for f in files):
        return None
    clusters = pd.read_csv(files[0])
    a = pd.read_csv(files[1])
    a["straight_km"], a["road_km"] = a["straight_dist_to_centre_km"], a["road_dist_to_centre_km"]
    clusters["max_straight_km"] = clusters["max_straight_dist_to_centre_km"]
    clusters["max_road_km"] = clusters["max_road_dist_to_centre_km"]
    a["rule"] = a["distance_rule"].map(RULE_LABELS).fillna(a["distance_rule"])
    secondaries = None
    if (folder / "secondaries.csv").exists():
        secondaries = pd.read_csv(folder / "secondaries.csv")
        secondaries["straight_km"] = secondaries["straight_dist_to_centre_km"]
        secondaries["road_km"] = secondaries["road_dist_to_centre_km"]
    return ModelResult(model, clusters, a, files[2], secondaries)


def standard_columns(df):
    # The cabinet columns under the names the notebooks use, whatever their upper/lower case in the file (the new
    # cabinet files call the ID column "cabinetid"). Other columns are left as they are.
    df = df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    by_lower = {c.lower(): c for c in df.columns}
    return df.rename(columns={by_lower[n.lower()]: n for n in CABINET_COLUMNS if n.lower() in by_lower})


def read_cabinets(source, partition=None):
    # A cabinet file (path or uploaded file) or a dataframe, with the standard column names; with a partition, only
    # its cabinets.
    df = standard_columns(source if isinstance(source, pd.DataFrame) else pd.read_csv(source))
    if partition is not None:
        df = df[df["partition"].astype(str).str.strip() == partition].reset_index(drop=True)
    return df


WORKER_FILES = ("worker_assignments.csv", "cluster_workers.csv")   # written by WorkerLayer.ipynb


def load_workers(model_dir):
    # The Worker Layer's results for one clustering result (in its workers/ folder): (one row per worker, one row
    # per cluster), or None if the Worker Layer has not been run on these results.
    folder = Path(model_dir) / "workers"
    if not all((folder / f).exists() for f in WORKER_FILES):
        return None
    workers = pd.read_csv(folder / WORKER_FILES[0], dtype={"staff_id": str, "node": str, "cluster_id": str})
    clusters = pd.read_csv(folder / WORKER_FILES[1], dtype={"cluster_id": str, "staff_ids": str, "nodes": str})
    workers["cluster_id"] = workers["cluster_id"].fillna("")
    clusters[["staff_ids", "nodes"]] = clusters[["staff_ids", "nodes"]].fillna("")
    return workers, clusters


def dataset_summary(path, params, partition=None):
    df = read_cabinets(path, partition)
    demand = df.loc[df["demand"] > 0, "demand"]
    cap = params["PEOPLE_PER_CLUSTER"] * params["DEMAND_PER_PERSON"]
    total = float(demand.sum())
    k_min = math.ceil(total / cap - 1e-9)
    smallest_min = team_band(params, params["MIN_PEOPLE"])[0]     # the smallest team at its minimum
    return {"cabinets": int(len(demand)), "total_demand": total, "cap": cap, "k_min": k_min,
            "k_max": max(k_min, math.floor(total / smallest_min + 1e-9)) if smallest_min > 0 else None}


# ----------------------------------------------------------------------------------------------- metrics
def model_metrics(result, facts, params, raw_demand=None):
    # raw_demand: the data's total demand, before v4 scales it to fit the people available
    c, a = result.clusters, result.assignments
    demand = c["demand_per_day"]
    fill = 100 * demand / c["max_demand"]
    facts = facts or {}
    m = {
        "clusters": len(c),
        "people_days": int(c["people"].sum()),
        "teams": {int(t): int(n) for t, n in c["people"].value_counts().items()},
        "below_min": int((c["demand_rule"] == "below minimum").sum()),
        "special": int((c["demand_rule"] == "one person, special case").sum()),
        "mean_fill": fill.mean(),
        "min_fill": fill.min(),
        "demand_per_person": (demand / c["people"]).mean(),
        "demand_std": demand.std(ddof=0),
        "worst_gap": demand.max() - demand.min(),
        "mean_km": a["straight_km"].mean(),
        "p95_km": a["straight_km"].quantile(0.95),
        "max_km": a["straight_km"].max(),
        "main_rule_share": 100 * (a["rule"] == "Main rule").mean(),
        "flagged": int((a["rule"] != "Main rule").sum()),
        "fallback": int((a["rule"] == "Straight-line fallback").sum()),
        "leftover": int((a["rule"] == "Leftover").sum()),
        "seconds": facts.get("seconds"),
        "osrm_requests": facts.get("osrm_requests"),
        "people_available": params.get("TOTAL_PEOPLE"),
        "demand_used": float(demand.sum()),
        "demand_share": 100 * demand.sum() / raw_demand if raw_demand else None,
        "people_saved": facts.get("people_saved"),
        "fit_runs": facts.get("fit_runs"),
        "max_road_km": a.loc[a["rule"] == "Main rule", "road_km"].max() if a["road_km"].notna().any() else None,
        "mean_road_km": a["road_km"].mean() if a["road_km"].notna().any() else None,
        "secondary_links": None if result.secondaries is None else len(result.secondaries),
        "secondary_mean": None if result.secondaries is None else len(result.secondaries) / len(c),
        "secondary_demand_mean": (None if result.secondaries is None
                                  else result.secondaries["demand"].sum() / len(c)),
    }
    return m


# ----------------------------------------------------------------------------------------------- step by step
def step_list(params, model, demand_share=None):
    # The slider positions: key, title and one line of text, with this run's own numbers. demand_share: the share
    # of the data's demand v4 planned with to fit the people available (%; above 100 = scaled up).
    full, low = params["PEOPLE_PER_CLUSTER"], params["MIN_PEOPLE"]
    full_min, cap = team_band(params, full)
    radius = f"{params['RADIUS_KM']:g} km"
    road = model in ("v2", "v4")
    how = "by road" if road else "in a straight line"
    scaled = (f" Demand is scaled {'down' if demand_share < 100 else 'up'} to {demand_share:.1f}% of the data to fit "
              f"{params['TOTAL_PEOPLE']} people."
              if model == "v4" and demand_share is not None and abs(demand_share - 100) >= 0.05 else "")
    steps = [
        ("cabinets", "Cabinets", "Every cabinet in the file. A bigger dot means more tickets per day." + scaled),
        ("kmeans", "Demand Layer: first split",
         f"Total demand divided by {cap:g} gives the number of clusters. Each cabinet goes to its nearest centre, "
         f"so some clusters end up above {cap:g}."),
        ("repair", "Demand Layer: repair",
         f"Cabinets move out of clusters above {cap:g} into touching clusters that have room."),
        ("balance", "Demand Layer: balancing",
         "Single cabinets move from heavier clusters to lighter touching ones, so demand is shared evenly."),
        ("distance", "Distance Layer",
         f"A cabinet more than {radius} {how} from its centre is moved, taken out on its own, or its cluster is "
         "split in two."),
        ("min_demand", "Minimum demand",
         f"A cluster below {full_min:g} merges with a neighbour, is shared out, or takes over cabinets from "
         "neighbours." + (f" Where the road rule blocks all three, a cluster below {team_band(params, low)[0]:g} "
                          "uses the straight line; a bigger one waits for a smaller team." if road
                          else "")),
        ("teams", "Smaller teams",
         (f"A cluster still below {full_min:g} gets a smaller team, one person fewer each round, down to {low}. "
          "Every team follows its demand."
          if low < full else "Every cluster keeps the full team in this run.")),
        ("leftovers" if model == "v4" else "final", "Leftovers",
         "A cluster no team fits is broken up. Each of its cabinets joins the cluster with the nearest border. "
         "Flagged cabinets are grey and do not move the centre. Tick \"Show flagged cabinets in their cluster\" "
         "to see them with their cluster."),
    ]
    if model == "v4":
        steps.append(("final", "Save people",
                      f"A cluster a little over a multiple of {params['DEMAND_PER_PERSON']:g} passes cabinets on to "
                      "touching clusters whose team stays the same, so it needs one person fewer."
                      + (f" If the clusters need more or fewer than {params['TOTAL_PEOPLE']} people, the demand "
                         "is scaled again and the model runs again; this is the run that was kept."
                         if params.get("TOTAL_PEOPLE") is not None else "")))
    steps += [
        ("secondary", "Secondary cabinets",
         f"Each cluster lists the nearest cabinets of other clusters within {radius} of its centre. Backup work, "
         "not counted in demand."),
    ]
    return [{"key": k, "title": t, "text": x} for k, t, x in steps]


def load_steps(result_set, model):
    # The steps recorded for a model (one row per cabinet), or None if there are none.
    path = result_set.model_dir(model) / "steps.csv"
    if not path.exists():
        return None
    steps = pd.read_csv(path)
    needed = ["CabinetId", "cabinetlat", "cabinetlong", "demand", *STEP_KEYS, "people_teams", "people_final",
              "leftover"]
    if "leftovers" in steps:      # v4
        needed.append("people_leftovers")
    return steps if set(needed) <= set(steps.columns) else None


def step_keys(steps):
    # The cluster-label columns these recorded steps have, in the order the model works.
    return [k for k in ALL_STEP_KEYS if k in steps.columns]


def steps_cluster_ids(steps, assignments):
    # {last step's label: cluster ID} if the recorded steps end in the same clusters as the results, else None.
    both = steps[["CabinetId", "final"]].merge(assignments[["CabinetId", "cluster_id"]], on="CabinetId")
    pairs = both[["final", "cluster_id"]].drop_duplicates()
    same = (len(both) == len(steps) == len(assignments) and pairs["final"].is_unique
            and pairs["cluster_id"].is_unique)
    return dict(zip(pairs["final"], pairs["cluster_id"])) if same else None


def haversine_km(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(np.radians, (lat1, lon1, lat2, lon2))
    a = np.sin((lat2 - lat1) / 2) ** 2 + np.cos(lat1) * np.cos(lat2) * np.sin((lon2 - lon1) / 2) ** 2
    return 2 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(a))


def step_view(steps, key, params, model):
    # What one slider position shows: (cabinets, clusters, facts). clusters is None before the first split.
    cab = steps[["CabinetId", "cabinetlat", "cabinetlong", "demand"]].copy()
    if key == "cabinets":
        return cab, None, {}
    col = "final" if key == "secondary" else key
    keys = step_keys(steps)
    before = keys[keys.index(col) - 1] if key not in ("kmeans", "secondary") else None
    cab["label"] = steps[col].to_numpy()
    cab["moved"] = steps[before].to_numpy() != cab["label"].to_numpy() if before else False
    cab["leftover"] = steps["leftover"].to_numpy(dtype=bool) if col in FLAG_STEPS else False
    radius = params["RADIUS_KM"] + 1e-9
    by_road = model in ("v2", "v4") and f"road_{col}" in steps
    # from the minimum demand step on, a fallback cabinet only has to be within the radius in a straight line
    relaxed = (steps["fallback"].to_numpy(dtype=bool) & (col in ("min_demand", "teams", *FLAG_STEPS)) if by_road
               else np.zeros(len(cab), dtype=bool))
    # flagged cabinets (leftover, fallback) don't move a centre; a cluster made only of them uses them all
    keep = ~(cab["leftover"].to_numpy() | relaxed)
    keep = keep | ~pd.Series(keep).groupby(cab["label"].to_numpy()).transform("any").to_numpy()
    own = cab.groupby("label")
    core = cab[keep].groupby("label")[["cabinetlat", "cabinetlong"]].mean()
    centre = core.reindex(cab["label"])
    cab["straight_km"] = haversine_km(cab["cabinetlat"].to_numpy(), cab["cabinetlong"].to_numpy(),
                                      centre["cabinetlat"].to_numpy(), centre["cabinetlong"].to_numpy())
    if by_road:
        cab["road_km"] = steps[f"road_{col}"].to_numpy(dtype=float)
        # on the last steps the model's own flags count; before that, a flag only shows once it is needed
        cab["fallback"] = relaxed if col in FLAG_STEPS else relaxed & (cab["road_km"] > radius)
        cab["far"] = np.where(relaxed, cab["straight_km"] > radius, cab["road_km"] > radius) & ~cab["leftover"]
    else:
        cab["road_km"], cab["fallback"] = np.nan, False
        cab["far"] = (cab["straight_km"] > radius) & ~cab["leftover"]
    # drawn apart on the map: grey, not in the cluster's colour or outline
    cab["apart"] = cab["leftover"] | (cab["fallback"] if col in FLAG_STEPS else False)

    c = own.agg(n_cabinets=("demand", "size"), demand=("demand", "sum")).reset_index()
    c["centre_lat"] = c["label"].map(core["cabinetlat"])
    c["centre_long"] = c["label"].map(core["cabinetlong"])
    # every cluster counts as a full team until the smaller teams step has run; 0 = too small for any team
    team = {"teams": "people_teams", "leftovers": "people_leftovers", "final": "people_final"}.get(col)
    c["people"] = c["label"].map(steps.groupby(col)[team].first()) if team else params["PEOPLE_PER_CLUSTER"]
    c["min_demand"], c["max_demand"] = team_band(params, c["people"].clip(lower=params["MIN_PEOPLE"]))
    if team:   # once the teams are known, one person may take up to SOLO_MAX_DEMAND as a special case
        solo = params.get("SOLO_MAX_DEMAND", params["DEMAND_PER_PERSON"])
        special = (c["people"] == 1) & (c["demand"] > c["max_demand"] + 1e-6) & (c["demand"] <= solo + 1e-6)
        c["max_demand"] = np.where(special, solo, c["max_demand"])
    c["state"] = np.select([c["demand"] > c["max_demand"] + 1e-6, c["people"] == 0,
                            c["demand"] < c["min_demand"] - 1e-6],
                           ["Above maximum", "Too small for a team", "Below minimum"], "Within range")
    cab["cluster_demand"] = cab["label"].map(c.set_index("label")["demand"])
    facts = {"clusters": len(c), "clusters_before": steps[before].nunique() if before else None,
             "above": int((c["state"] == "Above maximum").sum()),
             "below": int(c["state"].isin(["Below minimum", "Too small for a team"]).sum()),
             "too_small": int((c["state"] == "Too small for a team").sum()),
             "far": int(cab["far"].sum()), "by_road": by_road, "moved": int(cab["moved"].sum()),
             "leftover": int(cab["leftover"].sum()), "fallback": int(cab["fallback"].sum()),
             "demand_min": c["demand"].min(), "demand_max": c["demand"].max(),
             "people": int(c["people"].sum()),
             "teams": {int(t): int(n) for t, n in c["people"].value_counts().items()}}
    if col == "final" and "leftovers" in steps:     # v4: people before Save people
        facts["people_before"] = int(steps.groupby("leftovers")["people_leftovers"].first().sum())
    return cab, c, facts


def step_outlines(cab, together=False):
    # {cluster label: (longs, lats)}: the outline around each cluster's cabinets. Cabinets drawn apart (leftover,
    # and fallback on the last step) are left out, as on the notebook's map. together=True instead gives, for the
    # clusters that have such cabinets, the wider outline around all their cabinets, flagged ones included.
    rows = cab[cab["label"].isin(cab.loc[cab["apart"], "label"])] if together else cab[~cab["apart"]]
    outlines = {}
    for label, members in rows.groupby("label"):
        shape = shapely.MultiPoint(members[["cabinetlong", "cabinetlat"]].to_numpy()).convex_hull
        x, y = shape.buffer(OUTLINE_PAD_DEG, quad_segs=3).exterior.coords.xy
        outlines[int(label)] = (list(x), list(y))
    return outlines


def dashed(outline, dash_deg=0.004):
    # An outline (longs, lats) cut into short pieces with gaps between them, so it can be drawn as a dashed line
    # (about 450 m drawn, 450 m left out).
    ring = shapely.LineString(np.column_stack(outline))
    pieces = []
    for start in np.arange(0, ring.length, 2 * dash_deg):
        piece = shapely.ops.substring(ring, start, min(start + dash_deg, ring.length))
        if piece.geom_type == "LineString" and len(piece.coords) > 1:
            x, y = piece.coords.xy
            pieces.append((list(x), list(y)))
    return pieces


def step_colours(steps):
    # One colour per cluster label, the same on every step. Clusters that are next to each other on any step get
    # different colours where the palette allows.
    try:
        tri = Delaunay(steps[["cabinetlong", "cabinetlat"]].to_numpy()).simplices
        links = np.unique(np.sort(np.vstack([tri[:, [0, 1]], tri[:, [1, 2]], tri[:, [0, 2]]]), axis=1), axis=0)
    except Exception:   # too few cabinets, or all on one line
        links = np.empty((0, 2), dtype=int)
    touching = {}
    for key in step_keys(steps):
        label = steps[key].to_numpy()
        a, b = label[links[:, 0]], label[links[:, 1]]
        for p, q in set(zip(a[a != b].tolist(), b[a != b].tolist())):
            touching.setdefault(p, set()).add(q)
            touching.setdefault(q, set()).add(p)
    labels = {int(p) for key in step_keys(steps) for p in steps[key].unique()}
    colours, used = {}, Counter()
    for p in sorted(labels, key=lambda p: (-len(touching.get(p, ())), p)):
        taken = {colours[q] for q in touching.get(p, ()) if q in colours}
        free = [c for c in STEP_PALETTE if c not in taken] or STEP_PALETTE
        colours[p] = min(free, key=lambda c: (used[c], STEP_PALETTE.index(c)))
        used[colours[p]] += 1
    return colours


# ----------------------------------------------------------------------------------------------- uploads
def validate_upload(df, params):
    # Returns (clean dataframe or None, errors, warnings) for an uploaded cabinet file.
    errors, warnings = [], []
    df = standard_columns(df)
    missing = [c for c in REQUIRED_COLUMNS if c not in df.columns]
    if missing:
        errors.append(f"Missing column(s): {', '.join(c.lower() for c in missing)}. Needed: cabinetid, cabinetlat, "
                      "cabinetlong and demand.")
        return None, errors, warnings
    if "demand" not in df.columns:
        if {"total_tickets", "total_days"} <= set(df.columns):
            df["demand"] = pd.to_numeric(df["total_tickets"], errors="coerce") / pd.to_numeric(
                df["total_days"], errors="coerce")
            warnings.append("No `demand` column: worked it out as total_tickets / total_days.")
        else:
            errors.append("Missing `demand` column (tickets per day).")
            return None, errors, warnings
    for col in ["cabinetlat", "cabinetlong", "demand"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    bad = df[["cabinetlat", "cabinetlong", "demand"]].isna().any(axis=1)
    if bad.any():
        errors.append(f"{int(bad.sum())} row(s) have an empty or non-numeric latitude, longitude or demand "
                      f"(e.g. {', '.join(df.loc[bad, 'CabinetId'].astype(str).head(5))}).")
    dup = df["CabinetId"].duplicated(keep=False)
    if dup.any():
        errors.append(f"CabinetId must be unique: {', '.join(df.loc[dup, 'CabinetId'].astype(str).unique()[:5])} "
                      "appear more than once.")
    out = ~df["cabinetlat"].between(-90, 90) | ~df["cabinetlong"].between(-180, 180)
    if out.any():
        errors.append(f"{int(out.sum())} cabinet(s) have coordinates outside the valid range.")
    if (df["demand"] < 0).any():
        errors.append("Demand can't be negative.")
    cap = params["PEOPLE_PER_CLUSTER"] * params["DEMAND_PER_PERSON"]
    big = df["demand"] > cap
    if big.any():
        ids = ", ".join(f"{i} ({d:.2f})" for i, d in zip(df.loc[big, "CabinetId"], df.loc[big, "demand"]))
        errors.append(f"Cabinet(s) above CAP ({cap}/day) can never fit in one day: {ids}. "
                      "This data needs a separate step for very busy cabinets.")
    zero = (df["demand"] == 0).sum()
    if zero:
        warnings.append(f"{zero} cabinet(s) have zero demand; the models skip them.")
    if errors:
        return None, errors, warnings
    span_km = 111 * max(df["cabinetlat"].max() - df["cabinetlat"].min(),
                        (df["cabinetlong"].max() - df["cabinetlong"].min()) * math.cos(math.radians(df["cabinetlat"].mean())))
    if span_km > 400:
        warnings.append(f"The cabinets span about {span_km:.0f} km; the models' flat-map maths is meant for a region "
                        "up to a few hundred km across.")
    return df, errors, warnings


def osrm_server(model="v2"):
    # The road server a notebook asks: OSRM_URL in its parameters cell (read only).
    nb = json.loads(MODELS[model]["notebook"].read_text(encoding="utf-8"))
    cell = next(c for c in nb["cells"] if c["cell_type"] == "code")
    m = re.search(r"""^OSRM_URL\s*=\s*["']([^"']+)""", "".join(cell["source"]), flags=re.M)
    return m.group(1) if m else ""


def osrm_answers(url, timeout=3):
    # True if a server answers at this address.
    try:
        urllib.request.urlopen(url, timeout=timeout)
    except urllib.error.HTTPError:
        return True      # it answered, even if only to say the address is incomplete
    except OSError:      # refused, unreachable or no answer in time
        return False
    return True


def estimate_road_run(n_cabinets, public=True):
    # Rough cost of the road-first model on new data with an empty cache. Re-runs reuse saved distances.
    # Requests: about 1.1 per cabinet (1,564 cabinets with nothing saved took 1,763 requests).
    requests = max(5, round(1.1 * n_cabinets))
    minutes = max(1, round(requests * SECONDS_PER_REQUEST["public" if public else "own"] / 60))
    return requests, minutes
