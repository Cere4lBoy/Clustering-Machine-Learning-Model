"""NEO-I cabinet clustering dashboard (Telekom Malaysia theme).

Run from the project folder:  python -m streamlit run dashboard/app.py   (or double-click run_dashboard.bat)
Shows the results of the main model (v4, ClusteringModel.ipynb: road first, with the demand fitted to the people
available), of PartitionPipeline.ipynb (every partition planned on its own, combined or side by side) and of the older
model (v2, previous_versions/two_layer_clustering_v2_road.ipynb), and runs v4 or v2 on a new dataset. The
straight-line model (v3) is retired. The notebooks are never changed by the dashboard.
"""
import base64
import html
import io
import shutil
import sys
import zipfile
from pathlib import Path

import numpy as np
import pandas as pd
import plotly
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

sys.path.insert(0, str(Path(__file__).resolve().parent))
import results as R  # noqa: E402
import runner  # noqa: E402

ASSETS = Path(__file__).parent / "assets"
st.set_page_config(page_title="NEO-I · Cabinet Clustering", page_icon=str(ASSETS / "neo_i_logo.png"), layout="wide")
st.markdown(f"<style>{(Path(__file__).parent / 'style.css').read_text(encoding='utf-8')}</style>",
            unsafe_allow_html=True)
st.logo(str(ASSETS / "neo_i_logo.png"), size="large")

NAME = {m: R.MODELS[m]["name"] for m in R.MODELS}
COLOR = {m: R.MODELS[m]["color"] for m in R.MODELS}
NAVY = "#150080"
ROAD_MODELS = ("v2", "v4")
RULE_COLOR = {"Main rule": "#a3a8c3", "Straight-line fallback": NAVY, "Leftover": "#d92d20"}
INK = "#1a1a2e"
APART = "#9aa0a6"     # flagged cabinets: grey, not in their cluster's colour


@st.cache_data(show_spinner=False)
def asset_uri(name):
    # A file from dashboard/assets as a data URI, so it can be placed inside HTML.
    mime = {".png": "image/png", ".svg": "image/svg+xml"}[Path(name).suffix]
    return f"data:{mime};base64," + base64.b64encode((ASSETS / name).read_bytes()).decode()


# ============================================================================================== data access
@st.cache_data(show_spinner=False)
def cached_model(_rs, key, model, stamp):
    return R.load_model(_rs, model)


@st.cache_data(show_spinner=False)
def cached_text(path, stamp):
    return Path(path).read_text(encoding="utf-8")


@st.cache_data(show_spinner=False)
def cached_summary(path, params_items, partition, stamp):
    return R.dataset_summary(path, dict(params_items), partition)


def stamp(path):
    path = Path(path)
    return path.stat().st_mtime if path.exists() else 0.0


def load_results(rs):
    # {model: results} for the one model of this result set; empty if its files are missing
    result = cached_model(rs, rs.key, rs.model, stamp(rs.model_dir(rs.model) / "clusters.csv"))
    return {} if result is None else {rs.model: result}


def style_fig(fig, title, height=380):
    fig.update_layout(title=dict(text=title, font=dict(size=15, color=NAVY)), template="plotly_white",
                      height=height, margin=dict(l=10, r=10, t=56, b=10),
                      font=dict(family="Inter, Segoe UI, sans-serif", color="#3d4160"),
                      legend=dict(orientation="h", yanchor="bottom", y=1.0, xanchor="right", x=1, title=None),
                      hoverlabel=dict(font_family="Inter, Segoe UI, sans-serif"))
    fig.update_xaxes(gridcolor="#eef2f7", zeroline=False)
    fig.update_yaxes(gridcolor="#eef2f7", zeroline=False)
    return fig


def circle_points(lat, lon, km, n=120):
    t = np.linspace(0, 2 * np.pi, n)
    return lat + km / 111.32 * np.sin(t), lon + km / (111.32 * np.cos(np.radians(lat))) * np.cos(t)


def rgba(colour, alpha):
    return "rgba({},{},{},{})".format(*(int(colour[k:k + 2], 16) for k in (1, 3, 5)), alpha)


def joined(parts):
    # Several lines or shapes as one map trace: their points in a row, with a gap between them.
    xs, ys = [], []
    for x, y in parts:
        xs += [*x, None]
        ys += [*y, None]
    return xs, ys


def badge(model):
    return f'<span class="badge badge-{model}">{R.MODELS[model]["short"]}</span>'


def demand_text(d, digits=2, unit="/day"):
    # Demand as shown: "0.61/day", or "0.61/day (scaled) · 0.45/day (real)" when the v4 demand was scaled (SCALE).
    if SCALE is None:
        return f"{d:.{digits}f}{unit}"
    return f"{d:.{digits}f}{unit} (scaled) · {d / SCALE:.{digits}f}{unit} (real)"


def kpi(label, value, sub="", accent=R.BLUE):
    return (f'<div class="kpi" style="--accent:{accent}"><div class="kpi-label">{label}</div>'
            f'<div class="kpi-value">{value}</div><div class="kpi-sub">{sub}</div></div>')


# ============================================================================================== sidebar
sets = R.list_result_sets()
by_key = {s.key: s for s in sets}
# One entry per result in the Results list; a PartitionPipeline run is one entry for all its partitions.
entries = {}
for s_ in sets:
    entries.setdefault(f"group:{s_.group}" if s_.group else s_.key, []).append(s_)


def entry_label(k):
    first = entries[k][0]
    if not first.group:
        return first.label
    return (f"{first.created} · {first.dataset_name} · partitions "
            f"({', '.join(x.partition for x in entries[k])})")


# A widget's value can't be changed once it is drawn, so buttons below ask for a switch here, on the next rerun.
if "switch_to" in st.session_state:
    st.session_state["result_key"] = st.session_state.pop("switch_to")
if st.session_state.get("result_key") not in entries:
    st.session_state["result_key"] = next(iter(entries))

with st.sidebar:
    st.selectbox("Results", options=list(entries), key="result_key", format_func=entry_label,
                 help="The latest results of ClusteringModel.ipynb, the runs of PartitionPipeline.ipynb (one entry "
                      "per run, its partitions below), the v2 notebook's results if it was run, and the runs of the "
                      "New run tab.")
    group = entries[st.session_state["result_key"]]
    if group[0].group:
        names = {x.key: x.partition for x in group}
        if st.session_state.get("partition_key") not in names:
            st.session_state["partition_key"] = next(iter(names))
        st.selectbox("Partition", options=list(names), key="partition_key", format_func=names.get,
                     help="The tabs show this partition, planned on its own. The Partitions tab shows all of them "
                          "together, or two side by side.")
        rs = by_key[st.session_state["partition_key"]]
    else:
        rs = group[0]
    M = rs.model     # the model these results come from
    p = rs.params
    cap = p["PEOPLE_PER_CLUSTER"] * p["DEMAND_PER_PERSON"]
    full_min = p["PEOPLE_PER_CLUSTER"] * p["MIN_DEMAND_PER_PERSON"]
    team_sizes = list(range(p["PEOPLE_PER_CLUSTER"], p["MIN_PEOPLE"] - 1, -1))
    teams_text = f"{p['MIN_PEOPLE']}–{p['PEOPLE_PER_CLUSTER']}" if len(team_sizes) > 1 else f"{p['PEOPLE_PER_CLUSTER']}"
    st.markdown("**Model**")
    st.markdown(f'<div class="legend-card"><div class="t"><span class="dot" style="background:{COLOR[M]}"></span>'
                f'{NAME[M]}</div><div class="d">{R.MODELS[M]["about"]}</div></div>', unsafe_allow_html=True)
    st.markdown("**Rules**" + (f" of {rs.partition} (parameters.csv)" if rs.partition else ""))
    bands = "".join(f"- Team of {t}: **{t * p['MIN_DEMAND_PER_PERSON']:.3g}–{t * p['DEMAND_PER_PERSON']:g}** "
                    "tickets/day" + (f" (special case up to {p['SOLO_MAX_DEMAND']:g})"
                                     if t == 1 and p["SOLO_MAX_DEMAND"] > p["DEMAND_PER_PERSON"] else "") + "\n"
                    for t in team_sizes)
    st.markdown(bands +
                f"- Every cabinet within **{p['RADIUS_KM']:g} km** of its centre ({2 * p['RADIUS_KM']:g} km across)\n"
                "- No overlapping cluster outlines\n"
                + (f"- Up to **{p['MAX_SECONDARY']}** nearest secondary cabinets per cluster"
                   if p.get("MAX_SECONDARY") is not None else "- All secondary cabinets within the radius")
                + ("" if M != "v4" else
                   "\n- Save people: drop a person where a little demand can move"
                   + ("" if p.get("TOTAL_PEOPLE") is None else
                      f"\n- **{p['TOTAL_PEOPLE']}** people per day: demand scaled down or up (at most "
                      f"{p.get('MAX_DEMAND_SCALE', 3):g}×) to fit")))
    if rs.run:
        with st.popover("Delete this run", icon=":material/delete:"):
            st.write("Removes this run's folder. Notebooks and their results are kept.")
            if st.button("Yes, delete it", type="primary"):
                shutil.rmtree(rs.outputs.parent, ignore_errors=True)
                st.session_state["switch_to"] = "main"
                st.rerun()

results = load_results(rs)
info = cached_summary(str(rs.dataset), tuple(sorted(rs.params.items())), rs.partition, stamp(rs.dataset))
metrics = {m: R.model_metrics(results[m], rs.facts.get(m), rs.params, info["total_demand"]) for m in results}
# v4 results whose demand was scaled to fit the people: the factor, so real tickets can be shown next to scaled ones
_share = metrics[M]["demand_share"] if M == "v4" and M in metrics else None
SCALE = _share / 100 if _share and abs(_share - 100) >= 0.05 else None

# ============================================================================================== header
dataset_name = html.escape(rs.dataset_name)
header_facts = [f"<b>{dataset_name}</b>", *([f"partition <b>{html.escape(rs.partition)}</b>"] if rs.partition else []),
                f"{info['cabinets']:,} cabinets", f"{info['total_demand']:,.1f} tickets/day",
         f"{info['k_min']}–{info['k_max']} possible clusters",
         f"teams of {teams_text}", f"{p['MIN_DEMAND_PER_PERSON']:.3g}–{p['DEMAND_PER_PERSON']} tickets/day per person",
         f"{p['RADIUS_KM']:g} km"]
if M == "v4" and p.get("TOTAL_PEOPLE") is not None:
    header_facts.append(f"{p['TOTAL_PEOPLE']} people available")
if rs.created:
    header_facts.append(f"run {rs.created}")
st.markdown(
    '<div class="topbar"><div class="brand">'
    f'<img class="brand-logo" src="{asset_uri("neo_i_logo.png")}" alt="NEO-I">'
    '<div><div class="brand-name">NEO-I</div><div class="brand-title">Cabinet Clustering</div></div></div>'
    f'<div class="partner"><img class="tm-logo" src="{asset_uri("tm_logo.png")}" alt="Telekom Malaysia">'
    f'<img class="tm-element" src="{asset_uri("tm_element.svg")}" alt=""></div></div>'
    '<div class="facts">' + '<span class="sep">·</span>'.join(f"<span>{f}</span>" for f in header_facts) + "</div>",
    unsafe_allow_html=True)

if (rs.facts.get(M) or {}).get("ok") is False:
    st.error(f"{NAME[M]} did not finish for this dataset: {rs.facts[M].get('error')}")

(tab_overview, tab_effect, tab_partitions, tab_maps, tab_steps, tab_secondary, tab_clusters, tab_workers,
 tab_new) = st.tabs(["Overview", "Analytics", "Partitions", "Maps", "Step by step", "Secondary view", "Clusters",
                     "Workers", "New run"])


# ============================================================================================== overview
def model_panel(m):
    st.markdown(f'<div class="panel-title"><span class="dot" style="background:{COLOR[m]}"></span>{NAME[m]} '
                f'{badge(m)}</div><div class="panel-about">{R.MODELS[m]["about"]}</div>', unsafe_allow_html=True)
    if m not in results:
        st.markdown('<div class="empty-panel">Not run for this dataset.</div>', unsafe_allow_html=True)
        return
    x = metrics[m]
    far = (kpi("Farthest cabinet", f"{x['max_road_km']:.1f} km", f"by road · {x['max_km']:.1f} km straight", COLOR[m])
           if x["max_road_km"] is not None else
           kpi("Farthest cabinet", f"{x['max_km']:.1f} km", "straight line", COLOR[m]))
    requests = x["osrm_requests"]
    run_sub = (f"{requests} OSRM requests" + (" (all from cache)" if requests == 0 else "")) if requests is not None \
        else "no road data needed"
    by_team = " · ".join(f"{x['teams'].get(t, 0)} of {t}" for t in team_sizes)
    cards = [
        kpi("Clusters", f"{x['clusters']}", f"possible {info['k_min']}–{info['k_max']}", COLOR[m]),
        kpi("People per day", f"{x['people_days']}", f"teams: {by_team}", COLOR[m]),
        kpi("Mean fill rate", f"{x['mean_fill']:.1f}%",
            f"lowest {x['min_fill']:.1f}% · {x['special']} one-person special case", COLOR[m]),
        kpi("Demand per person", f"{x['demand_per_person']:.2f}", f"tickets/day (max {p['DEMAND_PER_PERSON']})", COLOR[m]),
        far,
        kpi("Flagged cabinets", f"{x['flagged']}", f"{x['fallback']} fallback · {x['leftover']} leftover", COLOR[m]),
        kpi("Secondary cabinets", "–" if x["secondary_links"] is None else f"{x['secondary_links']:,}",
            "not in these results" if x["secondary_mean"] is None else
            f"{x['secondary_mean']:.1f} per cluster", COLOR[m]),
        kpi("Secondary demand", "–" if x["secondary_demand_mean"] is None else
            f"{x['secondary_demand_mean']:.1f}", "tickets/day per cluster", COLOR[m]),
        kpi("Run time", "–" if x["seconds"] is None else f"{x['seconds']:.0f} s", run_sub, COLOR[m]),
    ]
    if m == "v4":    # fitting the demand to the people available, next to People per day
        available = x["people_available"]
        cards[2:2] = [
            kpi("People available", "no limit" if available is None else f"{available}",
                "needed: " + f"{x['people_days']}" + ("" if available is None else
                                                      " · fits" if x["people_days"] <= available else
                                                      f" · {x['people_days'] - available} too many"), COLOR[m]),
            kpi("Demand scale", "–" if x["demand_share"] is None else f"{x['demand_share']:.1f}%",
                f"{x['demand_used']:,.1f} scaled · {info['total_demand']:,.1f} real tickets/day", COLOR[m]),
            kpi("Save people", "–" if x["people_saved"] is None else f"{x['people_saved']}",
                "people saved" + ("" if x["fit_runs"] is None else f" · {x['fit_runs']} extra runs to fit"),
                COLOR[m]),
        ]
    st.markdown('<div class="kpi-grid">' + "".join(cards) + "</div>", unsafe_allow_html=True)
    c = results[m].clusters
    special = set(c.loc[c["demand_rule"] == "one person, special case", "cluster_id"])
    chips = "".join(f'<span class="team-chip{" special" if cid in special else ""}"'
                    f' title="{d:.2f} tickets/day">{t}</span>'
                    for cid, t, d in zip(c["cluster_id"], R.team_list(c), c["demand_per_day"]))
    st.markdown(f'<div class="team-head">Team per cluster · <b>{x["people_days"]} people per day</b></div>'
                f'<div class="team-grid">{chips}</div>', unsafe_allow_html=True)


with tab_overview:
    model_panel(M)


# ============================================================================================== analytics
def series(name, colour, clusters, assignments, params, secondaries=None, workers=None):
    # One set of results for the charts below: a name and colour, its clusters and cabinets, the rules they were
    # made with, and (if there are any) its secondary cabinets and workers.
    return {"name": name, "colour": colour, "fill": rgba(colour, 0.16), "c": clusters, "a": assignments,
            "p": params, "sec": secondaries, "w": workers}


def bands_over(fig, S, lo_hi, label):
    # A shaded band per category (box position k) for the target of each series' rules.
    for k, s in enumerate(S):
        lo, hi = lo_hi(s)
        fig.add_shape(type="rect", x0=k - 0.45, x1=k + 0.45, y0=lo, y1=hi, fillcolor="#eeebff", opacity=0.6,
                      line_width=0, layer="below")
        fig.add_annotation(x=k, y=hi, text=f"{label} {lo:.3g}–{hi:.3g}", showarrow=False, yshift=10,
                           font=dict(color=NAVY, size=12))


def demand_chart(S):
    # One column per team size, each with the band its clusters should be in (for one set of rules).
    sizes = sorted({int(t) for s in S for t in s["c"]["people"]}, reverse=True)
    label = {t: f"Team of {t}" for t in sizes}
    fig = go.Figure()
    for s in S:
        fig.add_trace(go.Box(x=s["c"]["people"].map(label), y=s["c"]["demand_per_day"], name=s["name"],
                             marker_color=s["colour"], fillcolor=s["fill"], boxpoints="all", jitter=0.45, pointpos=0,
                             line_width=2, customdata=s["c"]["cluster_id"],
                             hovertemplate="%{customdata}: %{y:.2f} tickets/day<extra></extra>"))
    if len(S) == 1:
        for k, t in enumerate(sizes):
            lo, hi = R.team_band(S[0]["p"], t)
            fig.add_shape(type="rect", x0=k - 0.48, x1=k + 0.48, y0=lo, y1=hi, fillcolor="#eeebff", opacity=0.6,
                          line_width=0, layer="below")
            fig.add_annotation(x=k, y=hi, text=f"target {lo:.3g}–{hi:.3g}", showarrow=False, yshift=10,
                               font=dict(color=NAVY, size=12))
    fig.update_layout(boxmode="group", showlegend=len(S) > 1)
    fig.update_xaxes(categoryorder="array", categoryarray=[label[t] for t in sizes])
    fig.update_yaxes(title="tickets/day per cluster")
    return style_fig(fig, "Demand per cluster, by team size")


def fill_chart(S):
    fig = go.Figure()
    for s in S:
        fig.add_trace(go.Histogram(x=100 * s["c"]["fill_rate"], name=s["name"], marker_color=s["colour"],
                                   opacity=0.75, xbins=dict(start=0, end=100.01, size=5),
                                   hovertemplate="%{x}% full: %{y} clusters<extra></extra>"))
    mean = np.mean([100 * s["c"]["fill_rate"].mean() for s in S]) if len(S) == 1 else None
    if mean is not None:
        fig.add_vline(x=mean, line_dash="dash", line_color=NAVY, annotation_text=f"mean {mean:.0f}%",
                      annotation_position="top left")
    fig.update_layout(barmode="overlay", showlegend=len(S) > 1)
    fig.update_xaxes(title="fill rate (%)", range=[0, 102])
    fig.update_yaxes(title="clusters")
    return style_fig(fig, "Fill rate of each cluster")


def per_person_chart(S):
    # Tickets per person in every cluster, against the rules' fewest and most per person. One set: a bar per
    # cluster (busiest first), with the real tickets as dots when the demand was scaled. Several: one box each.
    fig = go.Figure()
    if len(S) == 1:
        s = S[0]
        c = s["c"].sort_values("demand_per_person", ascending=False)
        fig.add_trace(go.Bar(x=c["cluster_id"], y=c["demand_per_person"], name="planned", marker_color=s["colour"],
                             customdata=c["people"], hovertemplate="%{x} · team of %{customdata}<br>"
                                                                   "%{y:.2f} tickets per person<extra></extra>"))
        if "real_demand_per_person" in c and (c["real_demand_per_person"] - c["demand_per_person"]).abs().max() > 0.01:
            fig.add_trace(go.Scatter(x=c["cluster_id"], y=c["real_demand_per_person"], mode="markers", name="real",
                                     marker=dict(color=R.ORANGE, size=9, symbol="diamond"),
                                     hovertemplate="%{x}: %{y:.2f} real tickets per person<extra></extra>"))
        for value, text in [(s["p"]["MIN_DEMAND_PER_PERSON"], "fewest"), (s["p"]["DEMAND_PER_PERSON"], "most")]:
            fig.add_hline(y=value, line_dash="dot", line_color="#6b7090",
                          annotation_text=f"{text} {value:g}", annotation_position="top right")
        fig.update_xaxes(title="clusters, busiest per person first", showticklabels=len(c) <= 30)
        fig.update_layout(bargap=0.25, barcornerradius=3)
    else:
        for s in S:
            fig.add_trace(go.Box(x=[s["name"]] * len(s["c"]), y=s["c"]["demand_per_person"], name=s["name"],
                                 marker_color=s["colour"], fillcolor=s["fill"], boxpoints="all", jitter=0.45,
                                 pointpos=0, line_width=2, customdata=s["c"]["cluster_id"],
                                 hovertemplate="%{customdata}: %{y:.2f} tickets per person<extra></extra>"))
        bands_over(fig, S, lambda s: (s["p"]["MIN_DEMAND_PER_PERSON"], s["p"]["DEMAND_PER_PERSON"]), "rule")
        fig.update_layout(showlegend=False)
    fig.update_yaxes(title="tickets per person per day")
    return style_fig(fig, "Tickets per person in each cluster")


def team_mix_chart(S):
    rows = [{"Results": s["name"], "Team": f"Team of {t}", "size": t, "Clusters": n, "People": t * n}
            for s in S for t, n in s["c"]["people"].value_counts().items()]
    df = pd.DataFrame(rows).sort_values("size", ascending=False)
    fig = px.bar(df, x="Team", y="Clusters", color="Results", barmode="group", text="Clusters",
                 color_discrete_map={s["name"]: s["colour"] for s in S}, custom_data=["People"])
    fig.update_traces(textposition="outside", hovertemplate="%{x}: %{y} clusters, %{customdata[0]} people"
                                                            "<extra></extra>")
    fig.update_layout(showlegend=len(S) > 1, barcornerradius=3)
    fig.update_yaxes(title="clusters", range=[0, 1.2 * df["Clusters"].max()])   # room for the numbers on top
    fig.update_xaxes(title=None)
    return style_fig(fig, "Clusters by team size")


def size_chart(S):
    fig = go.Figure()
    for s in S:
        fig.add_trace(go.Box(y=s["c"]["n_cabinets"], name=s["name"], marker_color=s["colour"], fillcolor=s["fill"],
                             boxpoints="all", jitter=0.45, pointpos=0, line_width=2, customdata=s["c"]["cluster_id"],
                             hovertemplate="%{customdata}: %{y} cabinets<extra></extra>"))
    fig.update_layout(showlegend=False)
    fig.update_yaxes(title="cabinets per cluster")
    return style_fig(fig, "Cluster size")


def distance_chart(S):
    fig = go.Figure()

    def add(values, label, color, dash):
        v = np.sort(values[np.isfinite(values)])
        if len(v):
            fig.add_trace(go.Scatter(x=v, y=100 * np.arange(1, len(v) + 1) / len(v), mode="lines", name=label,
                                     line=dict(color=color, width=2.5, dash=dash),
                                     hovertemplate="%{y:.0f}% of cabinets within %{x:.1f} km<extra></extra>"))

    for s in S:
        prefix = f"{s['name']} · " if len(S) > 1 else ""
        add(s["a"]["straight_km"].to_numpy(dtype=float), f"{prefix}straight line", s["colour"], "solid")
        if s["a"]["road_km"].notna().any():
            add(s["a"]["road_km"].to_numpy(dtype=float), f"{prefix}by road", s["colour"], "dot")
    for radius in sorted({s["p"]["RADIUS_KM"] for s in S}):
        fig.add_vline(x=radius, line_dash="dash", line_color="#6b7090", annotation_text=f"{radius:g} km",
                      annotation_position="top right")
    fig.update_xaxes(title="km from cabinet to its cluster centre")
    fig.update_yaxes(title="% of cabinets", range=[0, 101])
    return style_fig(fig, "How far cabinets are from their centre")


def reach_chart(S):
    # Each cluster's demand against its farthest cabinet: busy and compact clusters sit top left.
    fig = go.Figure()
    for s in S:
        c = s["c"]
        far = c["max_road_km"].where(c["max_road_km"].notna() & np.isfinite(c["max_road_km"]), c["max_straight_km"])
        fig.add_trace(go.Scatter(
            x=far, y=c["demand_per_day"], mode="markers", name=s["name"],
            marker=dict(size=8 + 2.2 * np.sqrt(c["n_cabinets"]), color=s["colour"], opacity=0.75,
                        line=dict(color="#ffffff", width=1)),
            customdata=np.column_stack([c["cluster_id"], c["people"], c["n_cabinets"]]),
            hovertemplate="%{customdata[0]} · team of %{customdata[1]}<br>%{y:.2f} tickets/day, "
                          "%{customdata[2]} cabinets<br>farthest cabinet %{x:.1f} km<extra></extra>"))
    fig.update_layout(showlegend=len(S) > 1)
    fig.update_xaxes(title="farthest cabinet from the centre (km, by road)")
    fig.update_yaxes(title="tickets/day")
    return style_fig(fig, "Demand against reach (dot size = cabinets)")


def rule_chart(S):
    rows = [{"Results": s["name"], "Rule": rule, "Cabinets": int((s["a"]["rule"] == rule).sum())}
            for s in S for rule in RULE_COLOR]
    fig = px.bar(pd.DataFrame(rows), y="Results", x="Cabinets", color="Rule", color_discrete_map=RULE_COLOR,
                 text="Cabinets", orientation="h")
    fig.update_traces(textposition="inside", texttemplate="%{text}")
    fig.update_yaxes(title=None)
    return style_fig(fig, "Which distance rule each cabinet meets", height=260 + 40 * len(S))


def secondary_chart(s):
    # Every cluster's own demand next to the backup work within its radius.
    c = s["c"]
    if "secondary_demand_per_day" not in c:
        return None
    c = c.sort_values("demand_per_day", ascending=False)
    fig = go.Figure([
        go.Bar(x=c["cluster_id"], y=c["demand_per_day"], name="own cabinets", marker_color=s["colour"],
               hovertemplate="%{x}: %{y:.2f} tickets/day of its own<extra></extra>"),
        go.Bar(x=c["cluster_id"], y=c["secondary_demand_per_day"], name="secondary cabinets",
               marker_color=rgba(s["colour"], 0.35), customdata=c["n_secondary"],
               hovertemplate="%{x}: %{y:.2f} tickets/day in %{customdata} secondary cabinets<extra></extra>")])
    fig.update_layout(barmode="group", bargap=0.2, barcornerradius=3)
    fig.update_xaxes(title=None, showticklabels=len(c) <= 30)
    fig.update_yaxes(title="tickets/day")
    return style_fig(fig, "Own demand and backup work nearby")


def trip_chart(S):
    # How far placed workers travel from their base to their cluster centre.
    S = [s for s in S if s["w"] is not None and (s["w"]["status"] == "assigned").any()]
    if not S:
        return None
    fig = go.Figure()
    for s in S:
        w = s["w"][s["w"]["status"] == "assigned"]
        col = "road_km_to_centre" if w["road_km_to_centre"].notna().any() else "straight_km_to_centre"
        if len(S) == 1:
            fig.add_trace(go.Histogram(x=w[col], marker_color=s["colour"], nbinsx=20, name=s["name"],
                                       hovertemplate="%{x} km: %{y} workers<extra></extra>"))
            fig.add_vline(x=w[col].mean(), line_dash="dash", line_color=NAVY,
                          annotation_text=f"average {w[col].mean():.1f} km", annotation_position="top right")
        else:
            fig.add_trace(go.Box(x=w[col], name=s["name"], marker_color=s["colour"], fillcolor=s["fill"],
                                 boxpoints="all", jitter=0.45, pointpos=0, customdata=w["staff_id"],
                                 hovertemplate="%{customdata}: %{x:.1f} km<extra></extra>"))
    fig.update_layout(showlegend=False, bargap=0.08)
    fig.update_xaxes(title="km from base to cluster centre")
    fig.update_yaxes(title="workers" if len(S) == 1 else None)
    return style_fig(fig, "Trip from base to cluster")


def node_chart(w):
    # Workers per node, placed or spare.
    rows = w.assign(status=w["status"].map({"assigned": "placed", "spare": "spare"}).fillna(w["status"]))
    df = rows.groupby(["node", "status"]).size().reset_index(name="Workers")
    fig = px.bar(df, x="node", y="Workers", color="status", text="Workers",
                 color_discrete_map={"placed": R.BLUE, "spare": APART})
    fig.update_traces(textposition="inside")
    fig.update_layout(barcornerradius=3)
    fig.update_xaxes(title="node", categoryorder="total descending")
    return style_fig(fig, "Workers per node")


def cabinet_chart(S):
    fig = go.Figure()
    for s in S:
        a = s["a"]
        values = a["real_demand"] if "real_demand" in a else a["allocated_demand"]
        fig.add_trace(go.Histogram(x=values, marker_color=s["colour"], nbinsx=30, name=s["name"], opacity=0.75,
                                   hovertemplate="%{x} tickets/day: %{y} cabinets<extra></extra>"))
    fig.update_layout(barmode="overlay", bargap=0.08, showlegend=len(S) > 1)
    fig.update_xaxes(title="real tickets/day per cabinet")
    fig.update_yaxes(title="cabinets")
    return style_fig(fig, "Demand per cabinet")


def people_chart(summary, colours):
    # Per partition: the people its clusters need, its workers and the places left empty.
    s = summary[summary["partition"] != "Total"]
    fig = go.Figure()
    for col, name, colour in [("people_needed", "people needed", NAVY), ("workers", "workers", R.BLUE),
                              ("empty_places", "places empty", RULE_COLOR["Leftover"])]:
        fig.add_trace(go.Bar(x=s["partition"], y=s[col], name=name, marker_color=colour, text=s[col],
                             textposition="outside", hovertemplate=f"%{{x}}: %{{y}} {name}<extra></extra>"))
    fig.update_layout(barmode="group", barcornerradius=3)
    fig.update_xaxes(title=None)
    fig.update_yaxes(title="people per day")
    return style_fig(fig, "People needed and workers, per partition")


def chart(fig, key):
    if fig is None:
        return
    named = [t for t in fig.data if t.name and t.showlegend is not False]
    if fig.layout.showlegend is not False and len(named) > 2:   # a long legend goes under the chart, off the title
        fig.update_layout(legend=dict(orientation="h", yanchor="top", y=-0.25, xanchor="left", x=0),
                          margin=dict(b=40))
    st.plotly_chart(fig, theme=None, key=key)


def insight_cards(s, accent):
    # A few numbers behind the charts.
    c, a = s["c"], s["a"]
    busiest, quietest = c.loc[c["demand_per_day"].idxmax()], c.loc[c["demand_per_day"].idxmin()]
    real = c["real_demand_per_person"] if "real_demand_per_person" in c else c["demand_per_person"]
    road = a["road_km"] if a["road_km"].notna().any() else a["straight_km"]
    cards = [
        kpi("Busiest cluster", f"{busiest['demand_per_day']:.1f}", f"{busiest['cluster_id']} · team of "
            f"{busiest['people']} · tickets/day", accent),
        kpi("Quietest cluster", f"{quietest['demand_per_day']:.1f}", f"{quietest['cluster_id']} · team of "
            f"{quietest['people']} · tickets/day", accent),
        kpi("Real tickets per person", f"{real.mean():.2f}", f"per day · {real.min():.2f} to {real.max():.2f}",
            accent),
        kpi("People per cluster", f"{c['people'].mean():.1f}", f"{len(c)} clusters, {int(c['people'].sum())} "
            "people", accent),
        kpi("Cabinets within 10 km", f"{(road <= 10).mean():.0%}", "of their cluster centre"
            + (", by road" if a["road_km"].notna().any() else ""), accent),
    ]
    if s["w"] is not None and (s["w"]["status"] == "assigned").any():
        w = s["w"][s["w"]["status"] == "assigned"]
        col = "road_km_to_centre" if w["road_km_to_centre"].notna().any() else "straight_km_to_centre"
        cards.append(kpi("Average trip", f"{w[col].mean():.1f} km", f"base to centre · longest {w[col].max():.1f} km",
                         accent))
    return '<div class="kpi-grid">' + "".join(cards) + "</div>"


@st.cache_data(show_spinner=False)
def cached_workers(model_dir, stamp):
    return R.load_workers(model_dir)


with tab_effect:
    if not results:
        st.info("No results for this dataset yet.")
    else:
        loaded = cached_workers(str(rs.model_dir(M)), stamp(rs.model_dir(M) / "workers" / R.WORKER_FILES[0]))
        one = series(NAME[M] if rs.partition is None else rs.partition, COLOR[M], results[M].clusters,
                     results[M].assignments, p, results[M].secondaries, None if loaded is None else loaded[0])
        st.markdown(insight_cards(one, COLOR[M]), unsafe_allow_html=True)
        st.markdown('<div class="section">Workload</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2, gap="large")
        with c1:
            chart(demand_chart([one]), "an_demand")
            chart(per_person_chart([one]), "an_person")
        with c2:
            chart(fill_chart([one]), "an_fill")
            chart(team_mix_chart([one]), "an_teams")
        st.markdown('<div class="section">Distance</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2, gap="large")
        with c1:
            chart(distance_chart([one]), "an_distance")
            chart(size_chart([one]), "an_size")
        with c2:
            chart(reach_chart([one]), "an_reach")
            chart(rule_chart([one]), "an_rule")
        st.markdown('<div class="section">Backup work, workers and cabinets</div>', unsafe_allow_html=True)
        c1, c2 = st.columns(2, gap="large")
        with c1:
            chart(secondary_chart(one), "an_secondary")
            if loaded is not None:
                chart(node_chart(loaded[0]), "an_nodes")
        with c2:
            chart(trip_chart([one]), "an_trips")
            chart(cabinet_chart([one]), "an_cabinets")
        if loaded is None:
            st.caption("Run the Worker Layer on these results to see the workers' trips here too.")


# ============================================================================================== partitions
@st.cache_data(show_spinner=False)
def cached_partition_run(folder, stamp):
    return R.load_partition_run(folder)


def partition_colours(parts):
    return {q: R.STEP_PALETTE[k % len(R.STEP_PALETTE)] for k, q in enumerate(parts)}


def partition_series(run, q, colour):
    def pick(df):
        return df[df["partition"] == q] if len(df) else df
    w = pick(run["worker_assignments"])
    return series(q, colour, pick(run["clusters"]), pick(run["assignments"]), run["rules"][q],
                  pick(run["secondaries"]), w if len(w) else None)


def partition_map(run, parts, colours, by_cluster=False, outlines=True, height=560):
    # The cabinets and cluster centres of these partitions. Cabinets in the colour of their partition (or, with
    # by_cluster, of their cluster); outlines around each cluster's main-rule cabinets.
    a = run["assignments"][run["assignments"]["partition"].isin(parts)]
    cab = a.merge(run["cabinets"], on=["partition", "CabinetId"], suffixes=("", "_file"))
    c = run["clusters"][run["clusters"]["partition"].isin(parts)]
    cluster_colour = {cid: R.STEP_PALETTE[k % len(R.STEP_PALETTE)] for k, cid in enumerate(sorted(c["cluster_id"]))}
    colour_of = (lambda cid, q: cluster_colour[cid]) if by_cluster else (lambda cid, q: colours[q])
    fig = go.Figure()
    if outlines and len(cab):
        codes, names = pd.factorize(cab["cluster_id"])
        shapes = R.step_outlines(cab.assign(label=codes, apart=cab["rule"] != "Main rule"))
        owner = cab.drop_duplicates("cluster_id").set_index("cluster_id")["partition"]
        by_colour = {}
        for label, shape in shapes.items():
            cid = names[label]
            by_colour.setdefault(colour_of(cid, owner[cid]), []).append(shape)
        for colour, parts_ in by_colour.items():
            lon, lat = joined(parts_)
            fig.add_trace(go.Scattermap(lat=lat, lon=lon, mode="lines", fill="toself", fillcolor=rgba(colour, 0.1),
                                        line=dict(color=colour, width=2), hoverinfo="skip", showlegend=False))
    for q in parts:
        rows = cab[cab["partition"] == q]
        if not len(rows):
            continue
        flagged = rows["rule"] != "Main rule"
        fig.add_trace(go.Scattermap(
            lat=rows["cabinetlat"], lon=rows["cabinetlong"], mode="markers", name=f"{q} cabinets ({len(rows)})",
            marker=dict(size=6 + 3 * np.sqrt(rows["allocated_demand"]),
                        color=np.where(flagged, APART, [colour_of(cid, q) for cid in rows["cluster_id"]]),
                        opacity=0.85),
            text=[f"<b>{i}</b> · {q}<br>cluster {cid}<br>{d:.3f} tickets/day<br>{r}"
                  for i, cid, d, r in zip(rows["CabinetId"], rows["cluster_id"], rows["allocated_demand"],
                                          rows["rule"])],
            hovertemplate="%{text}<extra></extra>"))
    fig.add_trace(go.Scattermap(lat=c["centre_lat"], lon=c["centre_long"], mode="markers", hoverinfo="skip",
                                marker=dict(size=24, color="#ffffff"), showlegend=False))
    for q in parts:
        rows = c[c["partition"] == q]
        if not len(rows):
            continue
        fig.add_trace(go.Scattermap(
            lat=rows["centre_lat"], lon=rows["centre_long"], mode="markers+text", name=f"{q} centres ({len(rows)})",
            marker=dict(size=18, color=colours[q]), textposition="top right", textfont=dict(size=11, color=INK),
            texttemplate=[f"{cid.split('-')[-1]} : {n}" for cid, n in zip(rows["cluster_id"], rows["people"])],
            text=[f"<b>{cid}</b><br>team of {n}<br>{d:.2f} tickets/day · {k} cabinets"
                  for cid, n, d, k in zip(rows["cluster_id"], rows["people"], rows["demand_per_day"],
                                          rows["n_cabinets"])],
            hovertemplate="%{text}<extra></extra>"))
    lat, lon = (cab["cabinetlat"], cab["cabinetlong"]) if len(cab) else (pd.Series([3.0]), pd.Series([101.7]))
    span = max(lat.max() - lat.min(), lon.max() - lon.min(), 0.02)
    fig.update_layout(map=dict(style="open-street-map", zoom=float(np.log2(300 / span)) - 0.3,
                               center=dict(lat=(lat.max() + lat.min()) / 2, lon=(lon.max() + lon.min()) / 2)),
                      height=height, margin=dict(l=0, r=0, t=0, b=0), font=dict(family="Inter, Segoe UI, sans-serif"),
                      legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.92)", bordercolor="#e2e8f0",
                                  borderwidth=1))
    return fig


def partition_cards(run, parts, accent=NAVY):
    s = run["summary"][run["summary"]["partition"].isin(parts)]
    c = run["clusters"][run["clusters"]["partition"].isin(parts)]
    w = run["worker_assignments"]
    w = w[w["partition"].isin(parts) & (w["status"] == "assigned")] if len(w) else w
    km = w["road_km_to_centre"] if len(w) else pd.Series(dtype=float)
    needed, workers = int(s["people_needed"].sum()), int(s["workers"].sum())
    cards = [
        kpi("Cabinets", f"{int(s['cabinets'].sum()):,}", f"{s['real_demand_per_day'].sum():,.1f} real tickets/day",
            accent),
        kpi("Clusters", f"{int(s['clusters'].sum())}", f"in {len(parts)} partitions" if len(parts) > 1 else
            "" if c.empty else f"teams of {int(c['people'].min())}–{int(c['people'].max())}", accent),
        kpi("People needed", f"{needed}", f"per day · workers: {workers}", accent),
        kpi("Workers placed", f"{int(s['workers_placed'].sum())}", f"{int(s['spare'].sum())} spare", accent),
        kpi("Places empty", f"{int(s['empty_places'].sum())}", "not enough workers" if s["empty_places"].sum()
            else "every team is full", accent),
        kpi("Mean fill rate", "–" if c.empty else f"{100 * c['fill_rate'].mean():.1f}%",
            "" if c.empty else f"lowest {100 * c['fill_rate'].min():.1f}%", accent),
        kpi("Average trip", "–" if km.empty or km.isna().all() else f"{km.mean():.1f} km",
            "base to centre, by road", accent),
    ]
    return '<div class="kpi-grid">' + "".join(cards) + "</div>"


RULE_NAMES = {"PEOPLE_PER_CLUSTER": "Biggest team", "MIN_PEOPLE": "Smallest team",
              "DEMAND_PER_PERSON": "Most tickets per person", "MIN_DEMAND_PER_PERSON": "Fewest tickets per person",
              "SOLO_MAX_DEMAND": "One person, special case up to", "RADIUS_KM": "Radius (km)",
              "MAX_SECONDARY": "Secondary cabinets per cluster", "TOTAL_PEOPLE": "People available (TOTAL_PEOPLE)",
              "MAX_DEMAND_SCALE": "Most demand scale-up", "FIT_ROUNDS": "Extra runs to fit"}


def combined_view(run, parts, colours, g):
    chosen = st.multiselect("Partitions", parts, default=parts, key=f"part_pick_{g}",
                            help="Leave out partitions to see the others on their own.")
    if not chosen:
        st.info("Pick at least one partition.")
        return
    st.markdown(partition_cards(run, chosen), unsafe_allow_html=True)
    s = run["summary"][run["summary"]["partition"].isin(chosen)].copy()
    s["DEMAND_SCALE"] = 100 * pd.to_numeric(s.get("DEMAND_SCALE"), errors="coerce")
    st.dataframe(s[["partition", "cabinets", "real_demand_per_day", "clusters", "people_needed", "workers",
                    "workers_placed", "spare", "empty_places", "PEOPLE_PER_CLUSTER", "MIN_DEMAND_PER_PERSON",
                    "DEMAND_PER_PERSON", "RADIUS_KM", "DEMAND_SCALE"]],
                 hide_index=True, column_config={
                     "partition": "Partition", "cabinets": "Cabinets",
                     "real_demand_per_day": st.column_config.NumberColumn("Real demand/day", format="%.1f"),
                     "clusters": "Clusters", "people_needed": "People needed", "workers": "Workers",
                     "workers_placed": "Placed", "spare": "Spare", "empty_places": "Empty places",
                     "PEOPLE_PER_CLUSTER": "Biggest team", "MIN_DEMAND_PER_PERSON": "Fewest/person",
                     "DEMAND_PER_PERSON": "Most/person", "RADIUS_KM": "Radius km",
                     "DEMAND_SCALE": st.column_config.NumberColumn("Demand scale", format="%.1f%%",
                                                                   help="Planning demand as a share of the real "
                                                                        "demand (above 100% = scaled up)")})
    left, right = st.columns([1, 3], vertical_alignment="center")
    by_cluster = left.toggle("Colour by cluster", key=f"part_by_cluster_{g}",
                             help="Off: every cabinet in its partition's colour.")
    right.caption("Grey cabinets are flagged (fallback or leftover). Clusters of different partitions may overlap: "
                  "the no-overlap rule holds within a partition.")
    st.plotly_chart(partition_map(run, chosen, colours, by_cluster), theme=None, key=f"part_map_{g}")
    S = [partition_series(run, q, colours[q]) for q in chosen]
    c1, c2 = st.columns(2, gap="large")
    with c1:
        chart(people_chart(run["summary"][run["summary"]["partition"].isin(chosen)], colours), f"part_people_{g}")
        chart(per_person_chart(S), f"part_person_{g}")
        chart(distance_chart(S), f"part_distance_{g}")
    with c2:
        chart(fill_chart(S), f"part_fill_{g}")
        chart(team_mix_chart(S), f"part_teams_{g}")
        chart(trip_chart(S), f"part_trips_{g}")

    st.markdown('<div class="section">Workers and their cabinets</div>'
                '<div class="section-sub">One row per worker and cabinet: Primary = a cabinet of the worker\'s own '
                'cluster, Secondary = nearby backup work in another cluster of the same partition.</div>',
                unsafe_allow_html=True)
    wc = run["worker_cabinets"]
    wc = wc[wc["partition"].isin(chosen)] if len(wc) else wc
    f1, f2 = st.columns([1, 2])
    kinds = f1.multiselect("Type", ["Primary", "Secondary"], default=["Primary", "Secondary"], key=f"part_type_{g}")
    query = f2.text_input("Search", placeholder="Staff ID, cabinet or cluster, e.g. Q109099, KJ_C005 or TRIS_1-C001",
                          key=f"part_search_{g}").strip().upper()
    rows = wc[wc["type"].isin(kinds)] if len(wc) else wc
    if query and len(rows):
        rows = rows[rows[["staff_id", "CabinetId", "cluster_id"]].apply(
            lambda col: col.astype(str).str.upper().str.contains(query, regex=False)).any(axis=1)]
    st.markdown(f"**{len(rows):,} rows** · {rows['staff_id'].nunique() if len(rows) else 0} workers")
    st.dataframe(rows, hide_index=True, height=380, column_config={
        "staff_id": "Staff ID", "CabinetId": "Cabinet", "type": "Primary/Secondary", "partition": "Partition",
        "cluster_id": "Cluster"})
    d1, d2, _ = st.columns([1, 1, 2])
    d1.download_button("These rows (CSV)", rows.to_csv(index=False).encode(), "worker_cabinets_filtered.csv",
                       "text/csv", icon=":material/download:", width="stretch", key=f"part_dl_rows_{g}")
    combined = Path(run["info"]["folder"]) / "combined" if "folder" in run["info"] else None
    if combined is not None and combined.exists():
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in sorted(combined.iterdir()):
                z.write(f, f"combined/{f.name}")
        d2.download_button("All combined files (ZIP)", buf.getvalue(), f"partitions_{g}.zip", "application/zip",
                           icon=":material/folder_zip:", width="stretch", key=f"part_dl_zip_{g}")


def side_by_side_view(run, parts, colours, g):
    if len(parts) < 2:
        st.info("This run has only one partition.")
        return
    a, b = st.columns(2, gap="large")
    left = a.selectbox("Left", parts, index=0, key=f"part_left_{g}")
    right = b.selectbox("Right", parts, index=1, key=f"part_right_{g}")
    rules = pd.DataFrame({q: {RULE_NAMES[n]: run["rules"][q].get(n) for n in RULE_NAMES} for q in (left, right)})
    rules = rules.map(lambda v: "blank" if v is None or (isinstance(v, float) and np.isnan(v)) else f"{v:g}"
                      if isinstance(v, (int, float)) else str(v))
    if left == right:
        rules = rules.iloc[:, :1]
    differ = rules.nunique(axis=1) > 1
    st.markdown('<div class="section">Rules</div><div class="section-sub">From parameters.csv, as each partition was '
                "planned. Rules that differ are marked.</div>", unsafe_allow_html=True)
    st.dataframe(rules.style.apply(lambda r: ["background-color:#fff1e8;font-weight:600"] * len(r)
                                   if differ[r.name] else [""] * len(r), axis=1), width="stretch")
    S = {q: partition_series(run, q, colours[q]) for q in (left, right)}
    top = max(float(S[q]["c"]["demand_per_person"].max()) for q in S) if all(len(S[q]["c"]) for q in S) else None
    for col, q, side in zip(st.columns(2, gap="large"), (left, right), ("l", "r")):
        with col:
            s = S[q]
            st.markdown(f'<div class="panel-title"><span class="dot" style="background:{colours[q]}"></span>{q}</div>',
                        unsafe_allow_html=True)
            st.markdown(partition_cards(run, [q], colours[q]), unsafe_allow_html=True)
            st.plotly_chart(partition_map(run, [q], colours, by_cluster=True, height=420), theme=None,
                            key=f"side_map_{g}_{side}")
            fig = per_person_chart([s])
            if top:
                fig.update_yaxes(range=[0, 1.12 * max(top, s["p"]["DEMAND_PER_PERSON"])])
            chart(fig, f"side_person_{g}_{side}")
            chart(fill_chart([s]), f"side_fill_{g}_{side}")
            chart(team_mix_chart([s]), f"side_teams_{g}_{side}")
            chart(trip_chart([s]), f"side_trips_{g}_{side}")


with tab_partitions:
    pipeline_sets = [s for s in sets if s.group]
    if rs.group is None:
        st.markdown('<div class="section">Partitions</div><div class="section-sub">PartitionPipeline.ipynb plans every '
                    "partition (TRIS, TROS_1, TROS_2, ...) on its own, with its own rules from parameters.csv, and "
                    "combines the results. This tab shows such a run: all partitions together, or two side by "
                    "side.</div>", unsafe_allow_html=True)
        if pipeline_sets:
            latest = pipeline_sets[0]
            st.info(f"These results have no partitions. The latest partition run is {latest.dataset_name} "
                    f"({latest.created}).", icon=":material/info:")
            if st.button("Show the latest partition run", type="primary", icon=":material/arrow_forward:"):
                st.session_state["switch_to"] = f"group:{latest.group}"
                st.rerun()
        else:
            st.info("No partition runs yet. Run PartitionPipeline.ipynb.", icon=":material/info:")
    else:
        folder = Path(rs.pipeline["folder"])
        run = cached_partition_run(str(folder), stamp(folder / "combined" / "summary.csv"))
        run["info"]["folder"] = str(folder)
        parts = [q for q in run["info"].get("partitions", []) if q in run["rules"]]
        colours = partition_colours(parts)
        st.markdown(f'<div class="section">Partitions · {html.escape(rs.dataset_name)} · {run["info"].get("created", "")}'
                    '</div><div class="section-sub">Every partition was planned on its own, with its own rules and '
                    'its own workers. The other tabs show one partition: pick it in the sidebar.</div>',
                    unsafe_allow_html=True)
        view_combined, view_side = st.tabs(["Combined", "Side by side"])
        with view_combined:
            combined_view(run, parts, colours, rs.group)
        with view_side:
            side_by_side_view(run, parts, colours, rs.group)


# ============================================================================================== maps
with tab_maps:
    if not results:
        st.info("No maps for this dataset yet.")
    else:
        st.caption("Layers can be switched on and off at the top right of the map. \"Flagged cabinets shown in their "
                   "cluster\" draws fallback and leftover cabinets with their cluster.")
        map_path = results[M].map_path
        components.html(cached_text(str(map_path), stamp(map_path)), height=720)


# ============================================================================================== step by step
RED, GREY = RULE_COLOR["Leftover"], "#a3a8c3"
TEAM_COLOR = [NAVY, "#5b4cf0", "#9d94ff"]     # full team first, lighter for every person fewer
STATE_COLOR = {"Above maximum": RED, "Below minimum": GREY, "Too small for a team": "#6b7090"}
TEAM_STEPS = ("teams", "leftovers", "final", "secondary")    # steps where every cluster has its team
FLAG_STEPS = ("leftovers", "final", "secondary")             # steps with the model's own flagged cabinets


@st.cache_resource(show_spinner=False)
def step_map_component():
    # The map of this tab as a small component of its own (dashboard/step_map): unlike st.plotly_chart it is not
    # rebuilt when the figure changes, so the zoom stays while the slider moves. It draws with the plotly.js that
    # comes with the installed plotly package, copied next to its page once.
    folder = Path(__file__).parent / "step_map"
    source = Path(plotly.__file__).parent / "package_data" / "plotly.min.js"
    if not (folder / "plotly.min.js").exists() or (folder / "plotly.min.js").stat().st_size != source.stat().st_size:
        shutil.copyfile(source, folder / "plotly.min.js")
    return components.declare_component("step_map", path=str(folder))


@st.cache_data(show_spinner=False)
def cached_steps(_rs, key, model, stamp):
    # (recorded steps, colour per cluster label, busiest cluster on any step); all None if nothing is recorded.
    steps = R.load_steps(_rs, model)
    if steps is None:
        return None, None, None
    return steps, R.step_colours(steps), max(steps.groupby(k)["demand"].sum().max() for k in R.step_keys(steps))


def steps_map(m, key, cab, c, colours, ids, circles, together=False):
    # together: flagged cabinets drawn in their cluster's colour, with a dashed outline around all of the cluster's
    # cabinets. The centres stay where they are.
    radius = p["RADIUS_KM"]
    size = 7 + 4 * np.sqrt(cab["demand"])
    fig = go.Figure()
    if c is None:
        fig.add_trace(go.Scattermap(lat=cab["cabinetlat"], lon=cab["cabinetlong"], mode="markers",
                                    marker=dict(size=size, color="#6b7090", opacity=0.85),
                                    name=f"Cabinets ({len(cab):,})",
                                    text=[f"<b>{i}</b><br>{demand_text(d)}"
                                          for i, d in zip(cab["CabinetId"], cab["demand"])],
                                    hovertemplate="%{text}<extra></extra>"))
    else:
        name = {q: ids.get(q, "cluster") if ids and key in FLAG_STEPS else "cluster" for q in c["label"]}
        outlines = R.step_outlines(cab)
        for colour in R.STEP_PALETTE:
            lon, lat = joined(shape for q, shape in outlines.items() if colours[q] == colour)
            if lon:
                fig.add_trace(go.Scattermap(lat=lat, lon=lon, mode="lines", fill="toself",
                                            fillcolor=rgba(colour, 0.13), line=dict(color=colour, width=2),
                                            hoverinfo="skip", showlegend=False))
        if together:
            wider = R.step_outlines(cab, together=True)
            for colour in R.STEP_PALETTE:
                lon, lat = joined(piece for q, shape in wider.items() if colours[q] == colour
                                  for piece in R.dashed(shape))
                if lon:
                    fig.add_trace(go.Scattermap(lat=lat, lon=lon, mode="lines", line=dict(color=colour, width=2),
                                                hoverinfo="skip", showlegend=False))
        over = [outlines[q] for q in c.loc[c["state"] == "Above maximum", "label"] if q in outlines]
        if over:
            lon, lat = joined(over)
            fig.add_trace(go.Scattermap(lat=lat, lon=lon, mode="lines", line=dict(color=RED, width=3.5),
                                        hoverinfo="skip", name=f"Above maximum ({len(over)})"))
        if circles:
            lat, lon = joined(circle_points(a, b, radius) for a, b in zip(c["centre_lat"], c["centre_long"]))
            fig.add_trace(go.Scattermap(lat=lat, lon=lon, mode="lines", line=dict(color="#64748b", width=1),
                                        hoverinfo="skip", name=f"{radius:g} km circles"))
        if key == "secondary" and ids and results[m].secondaries is not None:
            at = cab.set_index("CabinetId")[["cabinetlat", "cabinetlong"]]
            centre = results[m].clusters.set_index("cluster_id")[["centre_lat", "centre_long"]]
            sec = results[m].secondaries
            a, b = centre.loc[sec["cluster_id"]].to_numpy(), at.loc[sec["CabinetId"]].to_numpy()
            lat, lon = joined(zip(zip(a[:, 0], b[:, 0]), zip(a[:, 1], b[:, 1])))
            fig.add_trace(go.Scattermap(lat=lat, lon=lon, mode="lines", line=dict(color=rgba(NAVY, 0.45), width=1),
                                        hoverinfo="skip", name=f"Secondary links ({len(sec):,})"))
        # a ring is a bigger dot under the cabinet's own dot
        for flag, label, colour in [("moved", "Changed cluster in this step", INK),
                                    ("far", f"More than {radius:g} km from its centre", RED),
                                    ("fallback", "Straight-line fallback", RULE_COLOR["Straight-line fallback"]),
                                    ("leftover", "Leftover", RED)]:
            rows = cab[cab[flag] & (flag != "fallback" or key in FLAG_STEPS)]
            if len(rows):
                fig.add_trace(go.Scattermap(lat=rows["cabinetlat"], lon=rows["cabinetlong"], mode="markers",
                                            marker=dict(size=size[rows.index] + 6, color=colour),
                                            hoverinfo="skip", name=f"{label} ({len(rows):,})"))
        km = [f"{r:.2f} km by road" if np.isfinite(r) else f"{s:.2f} km" for r, s in
              zip(cab["road_km"], cab["straight_km"])]
        at = c.set_index("label")[["centre_lat", "centre_long"]]
        clat, clon = cab["label"].map(at["centre_lat"]), cab["label"].map(at["centre_long"])
        fig.add_trace(go.Scattermap(lat=cab["cabinetlat"], lon=cab["cabinetlong"], mode="markers",
                                    marker=dict(size=size, color=np.where(cab["apart"] & (not together), APART,
                                                                          cab["label"].map(colours))),
                                    showlegend=False,
                                    text=[f"<b>{i}</b><br>{demand_text(d)}<br>{name[q]}: {demand_text(t)}"
                                          f"<br>cabinet lat, long: {la:.5f}, {lo:.5f}"
                                          f"<br>centre lat, long: {cla:.5f}, {clo:.5f}<br>{k} to centre"
                                          + (f"<br><b>{'flagged' if together else 'drawn apart'}</b>: counts for "
                                             "this cluster, does not move its centre" if a else "")
                                          for i, d, q, t, k, la, lo, cla, clo, a in
                                          zip(cab["CabinetId"], cab["demand"], cab["label"], cab["cluster_demand"],
                                              km, cab["cabinetlat"], cab["cabinetlong"], clat, clon,
                                              cab["apart"])],
                                    hovertemplate="%{text}<extra></extra>"))
        # centres: one legend entry per team size once the teams are known
        teams_known = key in TEAM_STEPS
        shrink = 0.8 if len(c) > 40 else 1      # a little smaller when there are many clusters
        # a white halo under every centre, so the centres stand out from the cabinets
        fig.add_trace(go.Scattermap(lat=c["centre_lat"], lon=c["centre_long"], mode="markers", hoverinfo="skip",
                                    marker=dict(size=shrink * 26, color="#ffffff"), showlegend=False))
        for team in sorted(c["people"].unique(), reverse=True):
            rows = c[c["people"] == team]
            fewer = min(p["PEOPLE_PER_CLUSTER"] - team, len(TEAM_COLOR) - 1) if team else 0
            label = (f"Too small for a team ({len(rows)})" if team == 0 else
                     f"Team of {team} ({len(rows)})" if teams_known else f"Cluster centres ({len(rows)})")
            named = teams_known and ids and key in FLAG_STEPS
            fig.add_trace(go.Scattermap(
                lat=rows["centre_lat"], lon=rows["centre_long"], mode="markers+text" if named else "markers",
                name=label, textposition="top right", textfont=dict(size=12, color=INK),
                texttemplate=[f"{name[q]} : {t}" for q, t in zip(rows["label"], rows["people"])] if named else None,
                marker=dict(size=shrink * (20 - 2 * fewer if team else 14),
                            color=TEAM_COLOR[fewer] if team else "#6b7090"),
                text=[f"<b>Centre of {name[q]}</b><br>lat, long: {la:.5f}, {lo_:.5f}<br>{n} cabinets<br>"
                      f"{demand_text(d)}"
                      + (f"<br>team of {t} ({lo:g} to {hi:g} tickets/day)" if teams_known and t else "")
                      + ("" if s == "Within range" else f"<br>{s.lower()}")
                      for q, n, d, t, lo, hi, s, la, lo_ in zip(rows["label"], rows["n_cabinets"], rows["demand"],
                                                                rows["people"], rows["min_demand"],
                                                                rows["max_demand"], rows["state"],
                                                                rows["centre_lat"], rows["centre_long"])],
                hovertemplate="%{text}<extra></extra>"))
    lat, lon = cab["cabinetlat"], cab["cabinetlong"]
    span = max(lat.max() - lat.min(), lon.max() - lon.min(), 0.02)
    fig.update_layout(map=dict(style="open-street-map", zoom=float(np.log2(300 / span)),
                               center=dict(lat=(lat.max() + lat.min()) / 2, lon=(lon.max() + lon.min()) / 2)),
                      uirevision=rs.key, height=600, margin=dict(l=0, r=0, t=0, b=0),
                      font=dict(family="Inter, Segoe UI, sans-serif"),
                      legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.92)", bordercolor="#e2e8f0",
                                  borderwidth=1))
    return fig


def steps_bars(m, c, top, teams_known):
    full_min, cap = R.team_band(p, p["PEOPLE_PER_CLUSTER"])
    c = c.sort_values("demand", ascending=False).reset_index(drop=True)
    fig = go.Figure()
    for state, colour in [("Within range", COLOR[m]), *STATE_COLOR.items()]:
        rows = c[c["state"] == state]
        if len(rows):
            team = [f"<br>team of {t} ({lo:g} to {hi:g})" if teams_known and t else ""
                    for t, lo, hi in zip(rows["people"], rows["min_demand"], rows["max_demand"])]
            fig.add_trace(go.Bar(x=rows.index + 1, y=rows["demand"], name=f"{state} ({len(rows)})",
                                 marker_color=colour, customdata=np.column_stack([rows["n_cabinets"], team]),
                                 hovertemplate="%{y:.2f} tickets/day<br>%{customdata[0]} cabinets%{customdata[1]}"
                                               "<extra></extra>"))
    # the two lines are named in the right margin, where no bar can cover the text
    for value, text in [(cap, f"maximum<br>{cap:g}"), (full_min, f"minimum<br>{full_min:g} (full team)")]:
        fig.add_hline(y=value, line_color="#6b7090", line_width=1)
        fig.add_annotation(xref="paper", x=1.01, y=value, xanchor="left", text=text, showarrow=False, align="left",
                           font=dict(size=12, color="#3d4160"))
    fig.update_xaxes(title="clusters, busiest first", showticklabels=False, showgrid=False)
    fig.update_yaxes(title="tickets/day", range=[0, 1.08 * max(top, cap)])
    style_fig(fig, "Demand per cluster", height=600)
    fig.update_layout(bargap=0.2, barcornerradius=3, showlegend=len(fig.data) > 1, margin=dict(r=118, b=70),
                      legend=dict(orientation="h", yanchor="top", y=-0.07, xanchor="left", x=0))
    return fig


def cabinet_demand_chart(m, cab):
    fig = go.Figure(go.Histogram(x=cab["demand"], marker_color=COLOR[m], nbinsx=30,
                                 hovertemplate="%{x} tickets/day: %{y} cabinets<extra></extra>"))
    fig.update_layout(bargap=0.08)
    fig.update_xaxes(title="tickets/day per cabinet")
    fig.update_yaxes(title="cabinets")
    return style_fig(fig, "Demand per cabinet", height=600)


def step_cards(m, key, cab, f):
    # The numbers above the map for one step.
    full_min, cap = R.team_band(p, p["PEOPLE_PER_CLUSTER"])
    if key == "cabinets":
        return [kpi("Cabinets", f"{len(cab):,}", "with demand above zero", COLOR[m]),
                kpi("Total demand", f"{cab['demand'].sum():,.1f}", "tickets/day", COLOR[m]),
                kpi("Busiest cabinet", f"{cab['demand'].max():.2f}", "tickets/day", COLOR[m]),
                kpi("Clusters needed", f"{info['k_min']}–{info['k_max']}",
                    f"{info['k_min']} if every cluster is full", COLOR[m])]
    change = f["clusters"] - f["clusters_before"] if f["clusters_before"] is not None else None
    teams_known = key in TEAM_STEPS
    cards = [
        kpi("Clusters", f"{f['clusters']}", f"possible {info['k_min']}–{info['k_max']}" if change is None else
            "same as the step before" if change == 0 else f"{change:+d} from the step before", COLOR[m]),
        kpi("Above maximum", f"{f['above']}", f"clusters over {cap:g} tickets/day", COLOR[m]),
        kpi("Below minimum", f"{f['below']}", "clusters below their team's minimum" if teams_known else
            f"clusters under {full_min:g} tickets/day", COLOR[m]),
        kpi(f"Beyond {p['RADIUS_KM']:g} km", f"{f['far']}", "cabinets, by road" if f["by_road"] else
            "cabinets, straight line" + (" (road comes next)" if m in ROAD_MODELS else ""), COLOR[m]),
        kpi("Changed cluster", f"{f['moved']:,}", "cabinets in this step", COLOR[m]),
        kpi("Demand per cluster", f"{f['demand_min']:.1f}–{f['demand_max']:.1f}", "tickets/day", COLOR[m]),
    ]
    if teams_known:
        by_team = " · ".join(f"{f['teams'].get(t, 0)} of {t}" for t in team_sizes)
        cards.append(kpi("People per day", f"{f['people']}", f"teams: {by_team}", COLOR[m]))
    if key == "final" and "people_before" in f:     # v4: Save people
        saved = f["people_before"] - f["people"]
        cards.append(kpi("People saved", f"{saved}", f"{f['people_before']} → {f['people']} people per day",
                         COLOR[m]))
    if key in FLAG_STEPS:
        cards.append(kpi("Flagged cabinets", f"{f['leftover'] + f['fallback']}",
                         f"{f['fallback']} fallback · {f['leftover']} leftover", COLOR[m]))
    if key == "secondary" and results[m].secondaries is not None:
        cards.append(kpi("Secondary cabinets", f"{len(results[m].secondaries):,}",
                         f"{len(results[m].secondaries) / f['clusters']:.1f} per cluster", COLOR[m]))
    return cards


def step_note(key, f):
    # A short remark when a step had nothing to do on this data.
    if key in ("repair", "balance", "distance", "min_demand") and f["moved"] == 0:
        return "Nothing had to change in this step for this data."
    if key == "teams" and f["below"] == 0 and set(f["teams"]) == {p["PEOPLE_PER_CLUSTER"]}:
        return "Every cluster reached the full team's minimum, so no smaller team is needed."
    if key == "final" and "people_before" in f:     # v4: Save people
        return "" if f["people_before"] > f["people"] else "No cluster could pass cabinets on and drop a person."
    if key in ("leftovers", "final") and f["leftover"] == 0:
        return "No cluster was too small for a team, so there are no leftovers."
    return ""


def shift_step(by, last):
    st.session_state["step_pos"] = min(max(st.session_state["step_pos"] + by, 0), last)


with tab_steps:
    if not results:
        st.info("No results for this dataset yet.")
    else:
        m = M
        steps, colours, top_demand = cached_steps(rs, rs.key, m, stamp(rs.model_dir(m) / "steps.csv"))
        ids = None if steps is None else R.steps_cluster_ids(steps, results[m].assignments)
        if steps is None:
            st.markdown('<div class="empty-panel">The steps of this result are not recorded yet.</div>',
                        unsafe_allow_html=True)
        elif ids is None:
            st.warning("The recorded steps end in different clusters than this result: record them again.")
        if steps is None or ids is None:
            if st.button("Build step-by-step view", type="primary", icon=":material/play_arrow:",
                         key=f"steps_build_{m}",
                         help="Runs a copy of the model up to the leftover step and keeps the clusters after "
                              "every step. The results and the notebooks are not changed."):
                with st.status(f"Running {NAME[m]} step by step…", expanded=True) as status:
                    bar = st.progress(0.0, text="starting…")
                    built = runner.build_steps(rs, m, progress=lambda f, t: bar.progress(min(f, 1.0), text=t))
                    status.update(label=(f"Steps recorded in {built['seconds']} s" if built["ok"] else
                                         f"{NAME[m]} stopped: {built['error']}"),
                                  state="complete" if built["ok"] else "error", expanded=not built["ok"])
                if built["ok"]:
                    st.rerun()
                elif m in ROAD_MODELS:
                    st.caption("If a road distance is missing, start the road server with start_osrm.bat and "
                               "try again.")
        if steps is not None:
            step_names = R.step_list(p, m, metrics[m]["demand_share"])
            last = len(step_names) - 1
            if not isinstance(st.session_state.get("step_pos"), int):
                st.session_state["step_pos"] = 0
            back, slider, forward = st.columns([1.3, 9, 1.3], vertical_alignment="center")
            back.button("Back", icon=":material/chevron_left:", width="stretch", on_click=shift_step,
                        args=(-1, last), disabled=st.session_state["step_pos"] == 0)
            forward.button("Next", icon=":material/chevron_right:", width="stretch", on_click=shift_step,
                           args=(1, last), disabled=st.session_state["step_pos"] == last)
            pos = slider.select_slider("Step", options=list(range(len(step_names))), key="step_pos",
                                       format_func=lambda k: f"{k + 1}. {step_names[k]['title']}",
                                       label_visibility="collapsed")
            step = step_names[pos]
            cab, c, facts = R.step_view(steps, step["key"], p, m)
            ticks = st.columns([1, 2, 3])
            circles = ticks[0].checkbox(f"Show {p['RADIUS_KM']:g} km circles", key="steps_circles",
                                        disabled=c is None)
            together = ticks[1].checkbox(
                "Show flagged cabinets in their cluster", key="steps_together",
                disabled=step["key"] not in FLAG_STEPS or not cab.get("apart", pd.Series(False)).any(),
                help="Colours flagged cabinets as their cluster and draws a dashed outline round all its cabinets. "
                     "The centre does not move.")
            note = step_note(step["key"], facts)
            st.markdown(f'<div class="section">Step {pos + 1} of {len(step_names)} · {step["title"]}</div>'
                        f'<div class="section-sub">{step["text"]}{" <b>" + note + "</b>" if note else ""}</div>'
                        '<div class="kpi-grid">' + "".join(step_cards(m, step["key"], cab, facts)) + "</div>",
                        unsafe_allow_html=True)
            left, right = st.columns([3, 2], gap="large")
            with left:
                figure = steps_map(m, step["key"], cab, c, colours, ids, circles and c is not None,
                                   together and step["key"] in FLAG_STEPS)
                step_map_component()(figure=figure.to_json(), key="steps_map", default=None)
            with right:
                # the first split and the repair share one scale, so the bars visibly come down to the maximum
                top_bar = top_demand if step["key"] in ("kmeans", "repair") else 0
                st.plotly_chart(cabinet_demand_chart(m, cab) if c is None else
                                steps_bars(m, c, top_bar, step["key"] in TEAM_STEPS),
                                theme=None, key="steps_chart")


# ============================================================================================== secondary view
SECONDARY_COLOR = R.BLUE   # secondaries in the other theme colour, so they stand out


@st.cache_data(show_spinner=False)
def cached_coords(path, partition, stamp):
    return R.read_cabinets(path, partition)[["CabinetId", "cabinetlat", "cabinetlong"]]


def focus_map(m, cid, prim, sec, others, centre, dist_col):
    clat, clon = centre
    unit = "km by road" if dist_col == "road_km" else "km"
    fig = go.Figure()
    ring_lat, ring_lon = circle_points(clat, clon, p["RADIUS_KM"])
    fig.add_trace(go.Scattermap(lat=ring_lat, lon=ring_lon, mode="lines", line=dict(color="#64748b", width=1.5),
                                name=f"{p['RADIUS_KM']:g} km straight-line circle", hoverinfo="skip"))
    fig.add_trace(go.Scattermap(lat=others["cabinetlat"], lon=others["cabinetlong"], mode="markers",
                                marker=dict(size=6, color="#cbd5e1"), name="Other cabinets",
                                text=others["CabinetId"] + " · " + others["cluster_id"],
                                hovertemplate="%{text}<extra></extra>"))
    fig.add_trace(go.Scattermap(lat=sec["cabinetlat"], lon=sec["cabinetlong"], mode="markers",
                                marker=dict(size=13, color=SECONDARY_COLOR), name=f"Secondary ({len(sec)})",
                                text=[f"<b>{c}</b> · secondary<br>own cluster: {o}<br>{demand_text(d)}<br>{k:.2f} {unit}"
                                      for c, o, d, k in zip(sec["CabinetId"], sec["primary_cluster_id"], sec["demand"],
                                                            sec[dist_col])],
                                hovertemplate="%{text}<extra></extra>"))
    apart = prim["rule"] != "Main rule"
    for rows, label, colour in [(prim[~apart], f"Primary ({int((~apart).sum())})", COLOR[m]),
                                (prim[apart], f"Primary, flagged ({int(apart.sum())})", APART)]:
        if len(rows):
            fig.add_trace(go.Scattermap(
                lat=rows["cabinetlat"], lon=rows["cabinetlong"], mode="markers", marker=dict(size=13, color=colour),
                name=label,
                text=[f"<b>{c}</b> · primary<br>{demand_text(d)}<br>cabinet lat, long: {la:.5f}, {lo:.5f}"
                      f"<br>centre lat, long: {clat:.5f}, {clon:.5f}<br>{k:.2f} {unit} to centre"
                      + ("" if r == "Main rule" else f"<br><b>{r.lower()}</b>: does not move the centre")
                      for c, d, k, la, lo, r in zip(rows["CabinetId"], rows["allocated_demand"], rows[dist_col],
                                                    rows["cabinetlat"], rows["cabinetlong"], rows["rule"])],
                hovertemplate="%{text}<extra></extra>"))
    fig.add_trace(go.Scattermap(lat=[clat], lon=[clon], mode="markers", marker=dict(size=30, color="#ffffff"),
                                hoverinfo="skip", showlegend=False))
    fig.add_trace(go.Scattermap(lat=[clat], lon=[clon], mode="markers+text", marker=dict(size=22, color=NAVY),
                                text=[f"{cid}"], textposition="top right", textfont=dict(size=13, color=INK),
                                name=f"Centre of {cid}",
                                hovertemplate=f"Centre of {cid}<br>lat, long: {clat:.5f}, {clon:.5f}<extra></extra>"))
    fig.update_layout(map=dict(style="open-street-map", center=dict(lat=clat, lon=clon), zoom=10.2),
                      height=540, margin=dict(l=0, r=0, t=0, b=0), font=dict(family="Inter, Segoe UI, sans-serif"),
                      legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.92)", bordercolor="#e2e8f0",
                                  borderwidth=1))
    return fig


with tab_secondary:
    if not results:
        st.info("No results for this dataset yet.")
    else:
        st.markdown('<div class="section">Primary and secondary cabinets per cluster</div>'
                    '<div class="section-sub">Secondary cabinets belong to other clusters but lie within the radius '
                    "of this centre. They are not counted in this cluster's demand.</div>", unsafe_allow_html=True)
        m = M
        r = results[m]
        if r.secondaries is None:
            st.info("There are no secondary cabinets in these results.")
        else:
            c = r.clusters.sort_values("cluster_id").reset_index(drop=True)
            pick = f"sec_pick_{rs.key}_{m}"
            if st.session_state.get(pick) not in set(c["cluster_id"]):
                st.session_state[pick] = c["cluster_id"].iloc[0]
            per_row = 10
            for start in range(0, len(c), per_row):
                cols = st.columns(per_row)
                for col, row in zip(cols, c.iloc[start:start + per_row].itertuples()):
                    col.button(row.cluster_id, key=f"{pick}_{row.cluster_id}", width="stretch",
                               type="primary" if row.cluster_id == st.session_state[pick] else "secondary",
                               help=f"team of {row.people} · {row.n_cabinets} primary · "
                                    f"{row.demand_per_day:.1f}/day · {row.n_secondary} secondary",
                               on_click=lambda k=pick, v=row.cluster_id: st.session_state.__setitem__(k, v))
            cid = st.session_state[pick]
            row = c.set_index("cluster_id").loc[cid]
            coords = cached_coords(str(rs.dataset), rs.partition, stamp(rs.dataset))
            dist_col = "road_km"
            prim = r.assignments[r.assignments["cluster_id"] == cid].merge(coords, on="CabinetId")
            sec = r.secondaries[r.secondaries["cluster_id"] == cid].merge(coords, on="CabinetId")
            others = r.assignments[(r.assignments["cluster_id"] != cid)
                                   & ~r.assignments["CabinetId"].isin(sec["CabinetId"])].merge(coords, on="CabinetId")

            st.markdown(f'<div class="panel-title" style="margin-top:.8rem"><span class="dot" '
                        f'style="background:{COLOR[m]}"></span>{cid} · {NAME[m]}</div>', unsafe_allow_html=True)
            k1, k2, k3, k4, k5 = st.columns(5)
            k1.metric("Primary cabinets", f"{len(prim)}", border=True)
            k2.metric("Primary demand", f"{row['demand_per_day']:.2f}/day", border=True,
                      help=f"Team of {row['people']}: {row['fill_rate']:.0%} of its maximum "
                           f"({row['max_demand']:.3g}/day)")
            k3.metric("Secondary cabinets", f"{len(sec)}", border=True)
            k4.metric("Secondary demand", f"{sec['demand'].sum():.2f}/day", border=True,
                      help="Backup work nearby; not counted in this cluster's demand")
            k5.metric("Nearest secondary", "–" if sec.empty else
                      f"{sec[dist_col].min():.1f} km by road", border=True)
            st.plotly_chart(focus_map(m, cid, prim, sec, others, (row["centre_lat"], row["centre_long"]), dist_col),
                            theme=None)

            t1, t2 = st.columns(2, gap="large")
            prim_cols = ["CabinetId", "allocated_demand", "straight_km", "road_km", "rule"]
            sec_cols = ["CabinetId", "primary_cluster_id", "demand", "straight_km", "road_km"]
            km_config = {"straight_km": st.column_config.NumberColumn("To centre (straight km)", format="%.2f"),
                         "road_km": st.column_config.NumberColumn("To centre (road km)", format="%.2f")}
            with t1:
                st.markdown(f"**Primary cabinets of {cid}** · {len(prim)}")
                st.dataframe(prim.sort_values(dist_col)[prim_cols], hide_index=True, height=360, column_config={
                    "CabinetId": "Cabinet", "rule": "Distance rule",
                    "allocated_demand": st.column_config.NumberColumn("Demand/day", format="%.3f"), **km_config})
            with t2:
                st.markdown(f"**Secondary cabinets of {cid}** · {len(sec)}")
                st.dataframe(sec.sort_values(dist_col)[sec_cols], hide_index=True, height=360, column_config={
                    "CabinetId": "Cabinet", "primary_cluster_id": "Own cluster",
                    "demand": st.column_config.NumberColumn("Demand/day", format="%.3f"), **km_config})


# ============================================================================================== clusters
def zip_bytes():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for m, r in results.items():
            folder = {"v2": "v2_older_model", "v4": "v4_main_model"}[m]
            for f in ["clusters.csv", "teams.csv", "assignments.csv", "secondaries.csv", "map.html",
                      *(f"workers/{w}" for w in R.WORKER_FILES)]:
                if (rs.model_dir(m) / f).exists():
                    z.write(rs.model_dir(m) / f, f"{folder}/{f}")
        z.write(rs.dataset, "dataset.csv")
        if rs.run:
            z.write(rs.outputs.parent / "run.json", "run.json")
    return buf.getvalue()


with tab_clusters:
    if not results:
        st.info("No results for this dataset yet.")
    else:
        m = M
        top = st.columns([3, 2], vertical_alignment="bottom")
        with top[0]:
            query = st.text_input("Search", placeholder="Cluster or cabinet ID, e.g. C004 or CAB0123").strip().upper()
        with top[1]:
            st.download_button("Download all results (ZIP)", zip_bytes(), file_name=f"clustering_{rs.key}.zip",
                               mime="application/zip", icon=":material/download:")
        r = results[m]
        c = r.clusters.copy()
        c["fill_pct"] = 100 * c["fill_rate"]
        a = r.assignments.copy()
        if query:
            hit = a["CabinetId"].astype(str).str.upper().str.contains(query, regex=False) | \
                  a["cluster_id"].astype(str).str.upper().str.contains(query, regex=False)
            a = a[hit]
            c = c[c["cluster_id"].isin(a["cluster_id"])]
        cluster_cols = ["cluster_id", "n_cabinets", "people", "demand_per_day", "fill_pct", "demand_per_person",
                        "max_straight_km", "max_road_km", "n_leftover", "centre_lat", "centre_long"]
        if "n_straight_line_fallback" in c:
            cluster_cols.insert(-2, "n_straight_line_fallback")
        if "n_secondary" in c:
            cluster_cols[4:4] = ["n_secondary", "secondary_demand_per_day"]
        cluster_cols.insert(cluster_cols.index("demand_per_person") + 1, "demand_rule")
        if "real_demand_per_day" in c:   # v4 results since the demand can be scaled up: the unscaled numbers too
            cluster_cols[cluster_cols.index("demand_rule"):cluster_cols.index("demand_rule")] = [
                "real_demand_per_day", "real_demand_per_person"]
        st.markdown(f'<div class="section">Clusters · {NAME[m]}</div>', unsafe_allow_html=True)
        share = metrics[m]["demand_share"]
        if m == "v4" and share is not None and abs(share - 100) >= 0.05:
            st.caption(f"Demand in these tables is scaled {'down' if share < 100 else 'up'} to {share:.1f}% of the "
                       f"real demand, to fit {p['TOTAL_PEOPLE']} people per day"
                       + (". The real columns show the unscaled tickets." if "real_demand_per_day" in c else "."))
        st.dataframe(c[cluster_cols], hide_index=True, height=320, column_config={
            "cluster_id": "Cluster", "n_cabinets": "Cabinets",
            "people": st.column_config.NumberColumn("Team", help="People in this cluster's team"),
            "demand_rule": st.column_config.TextColumn("Demand rule", help="one person, special case = one person "
                                                       "takes 6 to 7 tickets/day; below minimum = leftovers that no "
                                                       "cluster could take"),
            "demand_per_day": st.column_config.NumberColumn("Demand/day", format="%.2f"),
            "fill_pct": st.column_config.ProgressColumn("Fill rate", format="%.0f%%", min_value=0, max_value=100,
                                                        help="Demand as a share of the team's maximum"),
            "demand_per_person": st.column_config.NumberColumn("Per person", format="%.2f"),
            "real_demand_per_day": st.column_config.NumberColumn("Real demand/day", format="%.2f",
                                                                 help="Before scaling: the real tickets per day"),
            "real_demand_per_person": st.column_config.NumberColumn("Real per person", format="%.2f",
                                                                    help="Real tickets per team member per day"),
            "max_straight_km": st.column_config.NumberColumn("Farthest (straight km)", format="%.2f"),
            "max_road_km": st.column_config.NumberColumn("Farthest (road km)", format="%.2f"),
            "n_straight_line_fallback": "Fallback", "n_leftover": "Leftover",
            "n_secondary": st.column_config.NumberColumn("Secondary cabinets", help="Other clusters' cabinets "
                                                         "within the radius of this centre"),
            "secondary_demand_per_day": st.column_config.NumberColumn("Secondary demand/day", format="%.1f"),
            "centre_lat": st.column_config.NumberColumn("Centre lat", format="%.5f"),
            "centre_long": st.column_config.NumberColumn("Centre long", format="%.5f")})
        cab_cols = ["CabinetId", "cluster_id", "allocated_demand", *(["real_demand"] if "real_demand" in a else []),
                    "straight_km", "road_km", "rule"]
        st.markdown(f'<div class="section">Cabinets · {NAME[m]}</div>', unsafe_allow_html=True)
        st.dataframe(a[cab_cols], hide_index=True, height=360, column_config={
            "CabinetId": "Cabinet", "cluster_id": "Cluster",
            "allocated_demand": st.column_config.NumberColumn("Demand/day", format="%.3f"),
            "real_demand": st.column_config.NumberColumn("Real demand/day", format="%.3f"),
            "straight_km": st.column_config.NumberColumn("To centre (straight km)", format="%.2f"),
            "road_km": st.column_config.NumberColumn("To centre (road km)", format="%.2f"),
            "rule": "Distance rule"})
        if r.secondaries is not None:
            sec = r.secondaries
            if query:
                sec = sec[sec["cluster_id"].astype(str).str.upper().str.contains(query, regex=False)
                          | sec["CabinetId"].astype(str).str.upper().str.contains(query, regex=False)
                          | sec["primary_cluster_id"].astype(str).str.upper().str.contains(query, regex=False)]
            st.markdown(f'<div class="section">Secondary cabinets · {NAME[m]}</div>', unsafe_allow_html=True)
            sec_cols = ["cluster_id", "CabinetId", "primary_cluster_id", "demand", "straight_km", "road_km"]
            st.dataframe(sec[sec_cols], hide_index=True, height=320, column_config={
                "cluster_id": "Secondary for cluster", "CabinetId": "Cabinet", "primary_cluster_id": "Own cluster",
                "demand": st.column_config.NumberColumn("Demand/day", format="%.3f"),
                "straight_km": st.column_config.NumberColumn("To centre (straight km)", format="%.2f"),
                "road_km": st.column_config.NumberColumn("To centre (road km)", format="%.2f")})
        d1, d2, d3, d4, _ = st.columns([1, 1, 1, 1, 1])
        folder = rs.model_dir(m)
        if (folder / "secondaries.csv").exists():
            d4.download_button("secondaries.csv", (folder / "secondaries.csv").read_bytes(),
                               file_name=f"{m}_secondaries.csv", mime="text/csv", icon=":material/table:")
        d1.download_button("clusters.csv", (folder / "clusters.csv").read_bytes(), file_name=f"{m}_clusters.csv",
                           mime="text/csv", icon=":material/table:")
        d2.download_button("assignments.csv", (folder / "assignments.csv").read_bytes(),
                           file_name=f"{m}_assignments.csv", mime="text/csv", icon=":material/table:")
        d3.download_button("map.html", (folder / "map.html").read_bytes(), file_name=f"{m}_map.html",
                           mime="text/html", icon=":material/map:")


# ============================================================================================== workers
def workers_map(w, cw, focus, km_col):
    # Cluster centres, one marker per base (workers of a base share its lat/long, the number = workers there) and a
    # line from each base to every cluster it sends workers to (in the cluster's colour, thicker = more workers).
    colours = {c: R.STEP_PALETTE[i % len(R.STEP_PALETTE)] for i, c in enumerate(cw["cluster_id"])}
    centre = cw.set_index("cluster_id")
    unit = "km by road" if km_col == "road_km_to_centre" else "km straight line"
    fig = go.Figure()
    bases = []
    for n, ((lat, lon), here) in enumerate(w.groupby(["latitude", "longitude"], sort=False)):
        placed = here[here["status"] == "assigned"]
        lines = [f"<b>Base</b> · node {', '.join(sorted(here['node'].unique()))} · {len(here)} workers"]
        for cid, team in placed.groupby("cluster_id"):
            c = centre.loc[cid]
            lines.append(f"{cid} ({team[km_col].iloc[0]:.1f} {unit}): {', '.join(team['staff_id'])}")
            fig.add_trace(go.Scattermap(
                lat=[lat, c["centre_lat"]], lon=[lon, c["centre_long"]], mode="lines", hoverinfo="skip",
                line=dict(color=colours[cid], width=1.5 + 1.2 * len(team)), opacity=1 if cid == focus else 0.7,
                name="Base to cluster", legendgroup="lines", showlegend=not any(t.legendgroup == "lines"
                                                                                 for t in fig.data)))
        spare = here.loc[here["status"] == "spare", "staff_id"]
        if len(spare):
            lines.append(f"<b>spare</b>: {', '.join(spare)}")
        goes_to = list(placed["cluster_id"].unique())
        bases.append({"lat": lat, "lon": lon, "n": len(here), "text": "<br>".join(lines), "focus": focus in goes_to,
                      "colour": APART if not goes_to else colours[goes_to[0]] if len(goes_to) == 1 else "#3d4160"})
    b = pd.DataFrame(bases)
    fig.add_trace(go.Scattermap(
        lat=b["lat"], lon=b["lon"], mode="markers+text", name=f"Bases ({len(b)})",
        marker=dict(size=np.where(b["focus"], 32, 14 + 4.4 * np.sqrt(b["n"])), color=b["colour"]),
        text=b["n"].astype(str), textposition="middle center", textfont=dict(size=11, color="#ffffff"),
        customdata=b["text"], hovertemplate="%{customdata}<extra></extra>"))
    short = cw["missing"] > 0
    fig.add_trace(go.Scattermap(
        lat=cw["centre_lat"], lon=cw["centre_long"], mode="markers+text", name="Cluster centres",
        marker=dict(size=np.where(cw["cluster_id"] == focus, 26, 20), color=np.where(short, "#d92d20", NAVY)),
        text=cw["cluster_id"], textposition="top right", textfont=dict(size=12, color=NAVY),
        customdata=[f"<b>{c}</b> · {a} of {n} people" + (f" · <b>{m} missing</b>" if m else "")
                    + f"<br>{s.replace('; ', ', ') or 'no workers'}"
                    for c, a, n, m, s in zip(cw["cluster_id"], cw["assigned"], cw["people"], cw["missing"],
                                             cw["staff_ids"])],
        hovertemplate="%{customdata}<extra></extra>"))
    if focus in centre.index:
        mid, zoom = (centre.loc[focus, "centre_lat"], centre.loc[focus, "centre_long"]), 10.3
    else:
        mid, zoom = (pd.concat([w["latitude"], cw["centre_lat"]]).mean(),
                     pd.concat([w["longitude"], cw["centre_long"]]).mean()), 7.4
    fig.update_layout(map=dict(style="open-street-map", center=dict(lat=mid[0], lon=mid[1]), zoom=zoom),
                      height=560, margin=dict(l=0, r=0, t=0, b=0), font=dict(family="Inter, Segoe UI, sans-serif"),
                      legend=dict(x=0.01, y=0.99, bgcolor="rgba(255,255,255,0.92)", bordercolor="#e2e8f0",
                                  borderwidth=1))
    return fig


with tab_workers:
    st.markdown('<div class="section">Workers per cluster</div>'
                '<div class="section-sub">Each cluster gets exactly its team, from the nearest workers: the total '
                "distance from the workers' bases to their cluster centres is as small as possible.</div>",
                unsafe_allow_html=True)
    model_dir = rs.model_dir(M)
    loaded = cached_workers(str(model_dir), stamp(model_dir / "workers" / R.WORKER_FILES[0]))
    if not results:
        st.info("No results for this dataset yet.")
    elif loaded is None:
        where = model_dir.relative_to(R.PROJECT).as_posix()
        st.info(f"No workers for these clusters yet. Run `WorkerLayer.ipynb` with `RESULTS_FOLDER = \"{where}\"` "
                f"and `OUTPUT_FOLDER = \"{where}/workers\"`.", icon=":material/badge:")
    else:
        w, cw = loaded
        made_for = cw.set_index("cluster_id")["people"].sort_index()
        current = results[M].clusters.set_index("cluster_id")["people"].sort_index()
        if not made_for.equals(current):
            st.warning("These workers were matched to an older version of the clusters. Run "
                       "`WorkerLayer.ipynb` again.", icon=":material/warning:")
        placed = w[w["status"] == "assigned"]
        km_col = "road_km_to_centre" if placed["road_km_to_centre"].notna().any() else "straight_km_to_centre"
        by = "road" if km_col == "road_km_to_centre" else "straight line"
        km = placed[km_col]
        n_spare, n_missing = int((w["status"] == "spare").sum()), int(cw["missing"].sum())
        cards = [
            kpi("Workers", f"{len(w)}", f"at {len(w.groupby(['latitude', 'longitude']))} bases", COLOR[M]),
            kpi("Places filled", f"{len(placed)} of {int(cw['people'].sum())}", f"{n_spare} spare", COLOR[M]),
            kpi("Places empty", f"{n_missing}", f"in {int((cw['missing'] > 0).sum())} clusters", COLOR[M]),
            kpi("Total km", f"{km.sum():,.0f}", f"to cluster centres, {by}", COLOR[M]),
            kpi("Average trip", f"{km.mean():.1f} km", f"longest {km.max():.1f} km", COLOR[M]),
            kpi("Within 10 km", f"{(km <= 10).mean():.0%}", "of placed workers", COLOR[M]),
        ]
        st.markdown('<div class="kpi-grid">' + "".join(cards) + "</div>", unsafe_allow_html=True)

        focus = st.selectbox("Focus on a cluster", ["All clusters", *cw["cluster_id"]], key=f"workers_focus_{rs.key}",
                             help="Zooms the map to one cluster and lists its team below.")
        st.plotly_chart(workers_map(w, cw, focus, km_col), theme=None)

        km_config = {"road_km_to_centre": st.column_config.NumberColumn("To centre (road km)", format="%.1f"),
                     "straight_km_to_centre": st.column_config.NumberColumn("To centre (straight km)",
                                                                             format="%.1f")}
        t1, t2 = st.columns([3, 2], gap="large")
        with t1:
            query = st.text_input("Search workers", placeholder="Staff ID, node or cluster, e.g. IP_001, TSK or C004",
                                  key=f"workers_search_{rs.key}").strip().upper()
            rows = w if focus == "All clusters" else w[w["cluster_id"] == focus]
            if query:
                rows = rows[rows[["staff_id", "node", "cluster_id"]].apply(
                    lambda s: s.str.upper().str.contains(query, regex=False)).any(axis=1)]
            st.markdown(f"**Workers** · {len(rows)}")
            st.dataframe(rows[["staff_id", "node", "cluster_id", "latitude", "longitude", "road_km_to_centre",
                               "straight_km_to_centre", "status"]], hide_index=True, height=380, column_config={
                "staff_id": "Staff ID", "node": "Node", "cluster_id": "Cluster", "status": "Status",
                "latitude": st.column_config.NumberColumn("Base lat", format="%.5f"),
                "longitude": st.column_config.NumberColumn("Base long", format="%.5f"), **km_config})
        with t2:
            st.markdown(f"**Teams per cluster** · {len(cw)}")
            mean_col, max_col = ("mean_road_km", "max_road_km") if by == "road" else \
                ("mean_straight_km", "max_straight_km")
            st.dataframe(cw[["cluster_id", "people", "assigned", "missing", "staff_ids", "nodes", mean_col, max_col]],
                         hide_index=True, height=450, column_config={
                "cluster_id": "Cluster", "people": "Team", "assigned": "Assigned", "missing": "Empty",
                "staff_ids": "Staff IDs", "nodes": "Nodes",
                mean_col: st.column_config.NumberColumn("Average km", format="%.1f"),
                max_col: st.column_config.NumberColumn("Longest km", format="%.1f")})
        d1, d2, _ = st.columns([1, 1, 2])
        d1.download_button("Workers CSV", (model_dir / "workers" / R.WORKER_FILES[0]).read_bytes(),
                           R.WORKER_FILES[0], "text/csv", icon=":material/download:", width="stretch")
        d2.download_button("Teams CSV", (model_dir / "workers" / R.WORKER_FILES[1]).read_bytes(),
                           R.WORKER_FILES[1], "text/csv", icon=":material/download:", width="stretch")


# ============================================================================================== new run
with tab_new:
    st.markdown('<div class="section">New run</div>'
                '<div class="section-sub">Upload a cabinet CSV, set the rules and run. The run is added to the '
                'Results list in the sidebar.</div>', unsafe_allow_html=True)
    left, right = st.columns([3, 2], gap="large")
    with right:
        with st.container(border=True):
            run_m = st.radio("Model", list(R.MODELS), format_func=lambda m: NAME[m], horizontal=True,
                             key="new_run_model", help="v4, the main model: road first, Save people, and the demand "
                                                       "scaled down or up to fit the people available. v2: the "
                                                       "older model, without the fitting.")
            st.caption(R.MODELS[run_m]["about"])
            # the main model starts from the rules of the ALL row of parameters.csv
            defaults = {**R.DEFAULT_PARAMS, **(R.notebook_params("v4") if run_m == "v4" else {})}
            st.markdown("**Rules for this run**")
            people = st.number_input("Biggest team (people)", 1, 10, int(defaults["PEOPLE_PER_CLUSTER"]))
            min_people = st.number_input("Smallest team (people)", 1, int(people),
                                         min(int(defaults["MIN_PEOPLE"]), int(people)),
                                         help="Quiet areas get a smaller team, down to this size.")
            per_person = st.number_input("Most tickets per person per day", 1, 30, int(defaults["DEMAND_PER_PERSON"]))
            min_per_person = st.number_input("Fewest tickets per person per day", 1, int(per_person),
                                             min(int(defaults["MIN_DEMAND_PER_PERSON"]), int(per_person)))
            solo_max = st.number_input("One person, special case: up to", int(per_person), int(per_person) + 5,
                                       min(max(int(defaults["SOLO_MAX_DEMAND"]), int(per_person)), int(per_person) + 5),
                                       help="Only used when a one-person cluster can't get down to the normal "
                                            "maximum. Set it equal to the maximum per person to switch it off.")
            new_cap = people * per_person
            radius = st.number_input("Radius (km)", 1.0, 100.0, float(defaults["RADIUS_KM"]), step=0.5,
                                     help="From the centre, so a cluster is at most twice this across.")
            st.markdown("  \n".join(f"Team of {t}: **{t * min_per_person}–{t * per_person}** tickets/day"
                                     + (f" (special case up to {solo_max})" if t == 1 and solo_max > per_person
                                        else "")
                                     for t in range(int(people), int(min_people) - 1, -1)))
            st.divider()
            keep_all = st.checkbox("List every secondary cabinet within the radius", value=False)
            max_secondary = st.slider("Secondary cabinets per cluster (nearest first)", 1, 30,
                                      int(defaults["MAX_SECONDARY"] or R.DEFAULT_PARAMS["MAX_SECONDARY"]),
                                      disabled=keep_all,
                                      help="Other clusters' cabinets within the radius of a centre, nearest first.")
            if run_m == "v4":
                st.divider()
                v4_defaults = R.notebook_params("v4")
                fit = st.checkbox("Fit to the people available", value=v4_defaults["TOTAL_PEOPLE"] is not None,
                                  help="Every cabinet's demand is scaled by the same factor so the clusters "
                                       "use about this many people: down if there is too much work, up (at most "
                                       f"{v4_defaults.get('MAX_DEMAND_SCALE', 3):g}×) if there is too little.")
                total_people = st.number_input("People available per day", 1, 5000,
                                               int(v4_defaults["TOTAL_PEOPLE"] or 92), disabled=not fit)
                fit_rounds = st.number_input("Extra runs to fit the people (at most)", 0, 20,
                                             int(v4_defaults["FIT_ROUNDS"]), disabled=not fit,
                                             help="If the clusters need more or fewer people than available, "
                                                  "the demand is scaled again and the model runs again.")
        new_params = {"PEOPLE_PER_CLUSTER": int(people), "MIN_PEOPLE": int(min_people),
                      "DEMAND_PER_PERSON": int(per_person), "MIN_DEMAND_PER_PERSON": int(min_per_person),
                      "SOLO_MAX_DEMAND": int(solo_max),
                      "RADIUS_KM": radius if radius % 1 else int(radius),
                      "MAX_SECONDARY": None if keep_all else int(max_secondary)}
        if run_m == "v4":
            new_params.update(TOTAL_PEOPLE=int(total_people) if fit else None, FIT_ROUNDS=int(fit_rounds))
    with left:
        upload = st.file_uploader("Cabinet file (CSV)", type=["csv"],
                                  help="Columns: cabinetid, cabinetlat, cabinetlong and demand (tickets per day).")
        sample = (R.read_cabinets(R.PROJECT / "Dataset" / "mock_cabinets.csv").head(5)
                  [["CabinetId", "cabinetlat", "cabinetlong", "demand"]].rename(columns={"CabinetId": "cabinetid"}))
        st.download_button("Download a template CSV", sample.to_csv(index=False).encode(), "cabinets_template.csv",
                           "text/csv", icon=":material/description:")
    if upload is not None:
        try:
            raw = pd.read_csv(upload)
        except Exception as err:   # not a readable CSV
            st.error(f"Could not read this file as CSV: {err}")
            raw = None
        if raw is not None:
            clean, errors, warnings = R.validate_upload(raw, new_params)
            for e in errors:
                st.error(e, icon=":material/error:")
            for w in warnings:
                st.warning(w, icon=":material/warning:")
            if clean is not None and "partition" in clean:
                found = sorted(clean["partition"].dropna().astype(str).str.strip().unique())
                st.info(f"This file has partitions ({', '.join(found)}). New run plans all its cabinets together "
                        "with the rules on the right. To plan every partition on its own, with its own rules from "
                        "parameters.csv and its own workers, run PartitionPipeline.ipynb; its results show in the "
                        "Partitions tab.", icon=":material/info:")
            if clean is not None:
                st.success("The file looks good.", icon=":material/check_circle:")
                demand = clean.loc[clean["demand"] > 0, "demand"]
                k_min = int(np.ceil(demand.sum() / new_cap - 1e-9))
                k_max = max(k_min, int(np.floor(demand.sum() / (min_people * min_per_person) + 1e-9)))
                s1, s2, s3, s4 = st.columns(4)
                s1.metric("Cabinets", f"{len(demand):,}", border=True)
                s2.metric("Total demand", f"{demand.sum():,.1f}/day", border=True)
                s3.metric("Possible clusters", f"{k_min}–{k_max}", border=True)
                s4.metric("Busiest cabinet", f"{demand.max():.2f}/day", border=True)
                limit = new_params.get("TOTAL_PEOPLE")
                if run_m == "v4" and limit is not None:
                    room = limit * new_params["DEMAND_PER_PERSON"]
                    up = R.notebook_params("v4").get("MAX_DEMAND_SCALE", 3)   # the notebook's own cap
                    st.caption(f"{limit} people × {new_params['DEMAND_PER_PERSON']} = {room:,} tickets/day: " +
                               (f"demand scaled down to about {100 * room / demand.sum():.1f}% (maybe less after "
                                "the extra runs)." if room < demand.sum() else
                                f"demand scaled up to about {100 * min(room / demand.sum(), up):.0f}% (at most "
                                f"{100 * up:.0f}%; real tickets per person drop by the same factor)."))
                fig = px.scatter_map(clean, lat="cabinetlat", lon="cabinetlong", color="demand", size="demand",
                                     size_max=12, zoom=9, height=420, map_style="open-street-map",
                                     hover_name="CabinetId", color_continuous_scale=["#b9b0ff", R.BLUE, R.ORANGE])
                fig.update_layout(margin=dict(l=0, r=0, t=0, b=0))
                st.plotly_chart(fig, theme=None)

                server = R.osrm_server(run_m)
                public = R.PUBLIC_OSRM in server
                req, minutes = R.estimate_road_run(len(demand), public)
                where = ("Cabinet coordinates are sent to the public OSRM demo server, so use test data only."
                         if public else f"Road distances come from our own OSRM server ({server}).")
                reruns = new_params.get("FIT_ROUNDS", 0) if new_params.get("TOTAL_PEOPLE") is not None else 0
                st.info(f"Runs {NAME[run_m]}. About **{req} OSRM requests** (~{minutes} min) if no distances are "
                        "saved yet" + (f", and up to {reruns} more runs if the people don't fit" if reruns else "")
                        + f". {where}", icon=":material/info:")
                run_clicked = st.button("Run model", type="primary", icon=":material/play_arrow:")
                if run_clicked and not public and not R.osrm_answers(server):
                    st.error(f"The road server at {server} is not answering. Start it with start_osrm.bat, "
                             "then press Run model again.")
                    run_clicked = False
                if run_clicked:
                    run_dir = runner.new_run(clean, upload.name, new_params, run_m)
                    with st.status(f"Running {NAME[run_m]}…", expanded=True) as status:
                        bar = st.progress(0.0, text="starting…")
                        res = runner.run_model(run_dir, run_m,
                                               progress=lambda f, t: bar.progress(min(f, 1.0), text=t))
                        status.update(label=(f"{NAME[run_m]} finished in {res['seconds']} s" if res["ok"] else
                                             f"{NAME[run_m]} stopped: {res['error']}"),
                                      state="complete" if res["ok"] else "error", expanded=not res["ok"])
                    if res["ok"]:
                        st.session_state["switch_to"] = run_dir.name
                        st.session_state["toast"] = f"Run saved: {run_dir.name}"
                        st.rerun()
                    else:
                        st.warning("The run is saved, but the model stopped (see above). This usually means the "
                                   "rules can't be met on this data.")

if msg := st.session_state.pop("toast", None):
    st.toast(msg, icon=":material/check_circle:")
