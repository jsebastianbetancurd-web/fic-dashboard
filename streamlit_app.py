"""
sfc_dashboard.py
----------------
Dashboard of the Colombian collective investment funds (FIC): gross per fund (base 100, returns,
volatility, place in the category), net ranking per class, one fund against its peers, returns,
volatility and drawdown, risk vs return, AUM by manager, data quality, and a page that says what
the figures are and what they are not. Every tab follows the filters of the side bar: cutoff
date, asset class, SIFIC category, manager, fund type, minimum AUM (Renta Fija by default).

The same file runs in two places:
    this PC      streamlit run sfc_dashboard.py      -> http://localhost:8501
                 (with the project's .venv: .venv\\Scripts\\streamlit run ...)
                 Reads 2. Power BI/*.parquet, the files Power BI uses: python pipeline.py builds
                 them (see README.md); refresh the browser to see new data.
    Streamlit    as streamlit_app.py of the public repo (publicar.py copies it there). There is
                 no 2. Power BI there: it downloads the tables publicar.py published
                 (PUBLISHED_URL), checks their sizes against estado.json, and looks for a newer
                 publication every CHECK_EVERY_S seconds.
Notes about editing the hand tables are shown only on this PC (LOCAL).

Metrics are computed here for any cutoff date, from the unit values (fact_rentabilidad):
    rent_<h>  effective annual return over 30 / 180 days, year to date, 1 / 3 / 5 years:
              (VU_t / VU_ini) ^ (365 / days) - 1, VU_ini = last value on or before the window
              start, at most MAX_START_GAP_DAYS earlier (else blank). SFC publishes the same.
    vol_1a    stddev of the daily returns of the last year (jumps left out) x sqrt(365), blank
              under MIN_COVERAGE of the days; vol_3a comes from fact_volatilidad.
    caida_1a  maximum drawdown of the unit value over the last year (blank with jumps).
    cuartil   quartile of the 1-year (and 3-year) return within the SIFIC category, among every
              participation reported at the cutoff (Q1 = best 25%), whatever the other filters.
Gross figures (tab "Bruta por fondo") are not computed here: they are read from
fact_rentabilidad_bruta on each fund's last date up to the cutoff (base 100, gross returns and
volatility for 1, 3 and 5 years, as fic_bruta.py built them).
Categories (clase_activo > categoria_sific) come from fic_relaciones.py: yours, LVA's official
one, researched, or inferred (the "Calidad de datos" tab counts them by source).

Requirements: streamlit, plotly, duckdb, pandas, openpyxl
"""

# =============================================================================
# CONFIG
# =============================================================================
import os
from pathlib import Path

PBI_DIR = Path(__file__).resolve().parent / "2. Power BI"
# The published tables (publicar.py). FIC_DATOS_URL overrides the address (file:// works, for
# tests); FIC_DASHBOARD_PUBLICADO=1 makes this PC read them too, instead of 2. Power BI.
PUBLISHED_URL = (os.environ.get("FIC_DATOS_URL")
                 or "https://github.com/jsebastianbetancurd-web/fic-dashboard/releases/download/datos")
STATE_FILE = "estado.json"
CHECK_EVERY_S = 3600             # how often the app looks for a newer publication
STALE_DAYS = 3                   # a publication older than this: the daily update is not running
DUCKDB_PUBLISHED = {"memory_limit": "600MB", "threads": 2}      # Streamlit's free hosting: about 690 MB
TABLES = ("dim_fondo", "dim_serie", "fact_diario", "fact_rentabilidad", "fact_volatilidad", "fact_fichas",
          "fact_rentabilidad_bruta", "resumen_bruta")
GROSS_YEARS = (1, 3, 5)          # periods of the gross tables (fic523_metrics.HORIZONS_YEARS)
DEFAULT_CLASS = "Renta Fija"     # asset class preselected
MAX_SERIES = 15                  # lines per chart, so they stay readable
DEFAULT_SELECTED = 8             # participations preselected for the charts (largest AUM)
COMPLETE_DAY_SHARE = 0.9         # a day is complete with >= 90% of the usual rows
ACTIVE_DAYS = 7                  # a participation counts at the cutoff if it reported in these days
MAX_START_GAP_DAYS = 7           # window start value at most this many days before the start
MIN_COVERAGE = 0.9               # share of the year's days needed for vol_1a and caida_1a
MIN_PEERS = 4                    # categories with fewer participations get no quartile

# return horizons: column -> (label, window start; {t} = the participation's date, {c} = cutoff)
HORIZONS = {
    "rent_30d": ("30 días", "{t} - INTERVAL 30 DAY"),
    "rent_180d": ("180 días", "{t} - INTERVAL 180 DAY"),
    "rent_ytd": ("Año corrido", "make_date(year({c}) - 1, 12, 31)"),
    "rent_1a": ("1 año", "{t} - INTERVAL 1 YEAR"),
    "rent_3a": ("3 años", "{t} - INTERVAL 3 YEAR"),
    "rent_5a": ("5 años", "{t} - INTERVAL 5 YEAR"),
}
CONFIRMED = ("tu clasificación", "LVA (oficial)")     # category sources: yours or official
RESEARCHED = ("investigada",)                         # researched fund by fund (factsheet, manager, web)
# any other source is inferred from the name, the SFC subtype or the volatility
SOURCE_LABELS = {"tu clasificación": "propia"}        # how a source is shown (the app is public)
FOCUS_COLOR, PEER_COLOR = "#C8102E", "#B5B5B5"        # one fund against its peers

# =============================================================================
# CODE
# =============================================================================
import io
import json
import re
import shutil
import tempfile
import urllib.request
from datetime import date, datetime, timedelta, timezone

import duckdb
import numpy as np
import pandas as pd
import plotly.express as px
import streamlit as st

st.set_page_config(page_title="FIC Colombia - rentabilidad bruta y neta, riesgo y mercado", layout="wide")

# this PC, with the pipeline's export; anywhere else the published tables are downloaded
LOCAL = (not os.environ.get("FIC_DASHBOARD_PUBLICADO")
         and all((PBI_DIR / f"{t}.parquet").exists() for t in TABLES))
WORK_DIR = Path(tempfile.gettempdir()) / "fic_dashboard"


# ---------- where the tables come from ------------------------------------------------
@st.cache_resource(ttl=CHECK_EVERY_S, show_spinner="Descargando los datos publicados...")
def _published() -> tuple[str, dict]:
    """Folder with the newest publication, and its estado.json. A download that fails or does not
    match the sizes in estado.json is dropped: the newest publication already here is used."""
    WORK_DIR.mkdir(parents=True, exist_ok=True)
    error = None
    try:
        with urllib.request.urlopen(f"{PUBLISHED_URL}/{STATE_FILE}", timeout=60) as r:
            estado = json.loads(r.read().decode("utf-8"))
        folder = WORK_DIR / re.sub(r"\W", "", estado["publicado"])
        if not (folder / STATE_FILE).exists():
            part = WORK_DIR / f"{folder.name}.part"
            shutil.rmtree(part, ignore_errors=True)
            part.mkdir()
            for name, info in estado["archivos"].items():
                with urllib.request.urlopen(f"{PUBLISHED_URL}/{name}", timeout=300) as r, open(part / name, "wb") as f:
                    shutil.copyfileobj(r, f, 1 << 20)
                if (part / name).stat().st_size != info["bytes"]:
                    raise OSError(f"{name} no tiene el tamaño que dice {STATE_FILE}")
            (part / STATE_FILE).write_text(json.dumps(estado), encoding="utf-8")
            part.rename(folder)
    except (OSError, ValueError, KeyError) as e:
        error = e
    ready = sorted(p for p in WORK_DIR.iterdir() if (p / STATE_FILE).exists())
    if not ready:
        raise RuntimeError(f"No se pudieron descargar los datos publicados ({PUBLISHED_URL}): {error}")
    for old in ready[:-2]:                       # the previous one stays for sessions still reading it
        shutil.rmtree(old, ignore_errors=True)
    return str(ready[-1]), json.loads((ready[-1] / STATE_FILE).read_text(encoding="utf-8"))


def _source() -> tuple[str, tuple]:
    """(folder with the tables, signature). The signature changes with every export of the
    pipeline (this PC) or every publication (Streamlit), so every cache reloads."""
    if LOCAL:
        return str(PBI_DIR), tuple((t, (PBI_DIR / f"{t}.parquet").stat().st_mtime) for t in TABLES)
    folder, estado = _published()
    return folder, (estado["publicado"],)


def _signature() -> tuple:
    return _source()[1]


def data_stamp() -> datetime:
    """When the tables were exported (this PC) or published (Streamlit), in UTC."""
    if LOCAL:
        return datetime.fromtimestamp(max(m for _, m in _signature()), timezone.utc)
    return datetime.strptime(_published()[1]["publicado"][:20], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


@st.cache_resource(show_spinner=False, max_entries=2)
def _connect(signature: tuple, folder: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(config={} if LOCAL else {**DUCKDB_PUBLISHED, "temp_directory": str(WORK_DIR / "tmp")})
    for t in TABLES:
        path = (Path(folder) / f"{t}.parquet").as_posix().replace("'", "''")
        con.execute(f"CREATE VIEW {t} AS SELECT * FROM read_parquet('{path}')")
    return con


def q(sql: str, params: list | None = None) -> pd.DataFrame:
    folder, signature = _source()
    return _connect(signature, folder).cursor().execute(sql, params or []).df()


def _in(ids) -> str:
    return ", ".join("'" + str(i).replace("'", "''") + "'" for i in ids)


def _d(day: date) -> str:
    return f"DATE '{day:%Y-%m-%d}'"


def pct(x) -> str:
    return "-" if pd.isna(x) else f"{x:.2%}"


def cop_bn(x) -> str:
    return "-" if pd.isna(x) else f"COP {x / 1e9:,.0f} mil M"


# ---------- data (cached per pipeline export) -------------------------------------
@st.cache_data(show_spinner="Cargando datos...")
def load_dates(signature: tuple) -> tuple[date, date, date]:
    r = q(f"""
        WITH c AS (SELECT fecha_corte, count(*) AS n FROM fact_diario GROUP BY 1)
        SELECT min(fecha_corte) AS d_min, max(fecha_corte) AS d_max,
               max(fecha_corte) FILTER (WHERE n >= {COMPLETE_DAY_SHARE} * (
                   SELECT median(n) FROM c WHERE fecha_corte >= (SELECT max(fecha_corte) FROM c) - 60)) AS d_ok
        FROM c""").iloc[0]
    return tuple(pd.Timestamp(r[k]).date() for k in ("d_min", "d_max", "d_ok"))


@st.cache_data(show_spinner="Calculando métricas a la fecha de corte...")
def universe(signature: tuple, cutoff: date) -> pd.DataFrame:
    """One row per participation reported in the ACTIVE_DAYS before the cutoff, with every metric."""
    c = _d(cutoff)
    cur = f"""cur AS (
        SELECT serie_id, arg_max(fondo_id, fecha_corte) AS fondo_id, max(fecha_corte) AS t,
               arg_max(valor_unidad, fecha_corte) AS vu_t
        FROM fact_rentabilidad
        WHERE fecha_corte BETWEEN {c} - INTERVAL {ACTIVE_DAYS} DAY AND {c} AND valor_unidad IS NOT NULL
        GROUP BY serie_id)"""
    cases = " ".join(f"WHEN '{k}' THEN ({start.format(t='cur.t', c=c)})::DATE" for k, (_, start) in HORIZONS.items())
    keys = ", ".join(f"('{k}')" for k in HORIZONS)
    rets = q(f"""
        WITH {cur},
        tg AS (SELECT cur.serie_id, cur.t, cur.vu_t, h.k, CASE h.k {cases} END AS target
               FROM cur CROSS JOIN (VALUES {keys}) AS h(k)),
        vu AS (SELECT serie_id, fecha_corte, valor_unidad FROM fact_rentabilidad
               WHERE valor_unidad IS NOT NULL AND fecha_corte <= {c}),
        st AS (SELECT tg.*, vu.fecha_corte AS f0, vu.valor_unidad AS vu0
               FROM tg ASOF LEFT JOIN vu ON tg.serie_id = vu.serie_id AND tg.target >= vu.fecha_corte)
        SELECT serie_id, k,
               CASE WHEN target - f0 <= {MAX_START_GAP_DAYS} AND t - f0 >= 7
                    THEN power(vu_t / vu0, 365.0 / (t - f0)) - 1 END AS r
        FROM st""").pivot(index="serie_id", columns="k", values="r")
    risk = q(f"""
        WITH {cur},
        w AS (SELECT r.serie_id, r.fecha_corte, r.valor_unidad, r.rent_diaria, r.es_salto
              FROM cur JOIN fact_rentabilidad r ON r.serie_id = cur.serie_id
               AND r.fecha_corte > cur.t - INTERVAL 1 YEAR AND r.fecha_corte <= cur.t),
        v AS (SELECT serie_id, stddev_samp(rent_diaria) FILTER (WHERE NOT es_salto) * sqrt(365) AS vol,
                     count(rent_diaria) AS n, count(*) FILTER (WHERE es_salto) AS saltos_1a
              FROM w GROUP BY serie_id),
        dd AS (SELECT serie_id, min(valor_unidad / pico - 1) AS caida
               FROM (SELECT serie_id, valor_unidad,
                            max(valor_unidad) OVER (PARTITION BY serie_id ORDER BY fecha_corte
                                                    ROWS UNBOUNDED PRECEDING) AS pico
                     FROM w WHERE valor_unidad IS NOT NULL)
               GROUP BY serie_id),
        fi AS (SELECT serie_id, arg_max(comision_administracion, fecha_corte) / 100 AS comision,
                      max(fecha_corte) AS ficha_fecha
               FROM fact_fichas WHERE serie_id IS NOT NULL AND fecha_corte <= {c}
                 AND comision_administracion IS NOT NULL GROUP BY serie_id)
        SELECT cur.serie_id, cur.fondo_id, cur.t AS fecha_dato,
               CASE WHEN v.n >= {MIN_COVERAGE} * 365 THEN v.vol END AS vol_1a,
               CASE WHEN v.n >= {MIN_COVERAGE} * 365 AND v.saltos_1a = 0 THEN dd.caida END AS caida_1a,
               fv.vol_3a, coalesce(v.saltos_1a, 0) + coalesce(fr.saltos_3a, 0) AS saltos,
               d.valor_fondo AS aum, d.num_inversionistas AS inversionistas,
               fi.comision, fi.ficha_fecha
        FROM cur
        LEFT JOIN v USING (serie_id) LEFT JOIN dd USING (serie_id) LEFT JOIN fi USING (serie_id)
        LEFT JOIN fact_volatilidad fv ON fv.serie_id = cur.serie_id AND fv.fecha_corte = cur.t
        LEFT JOIN fact_rentabilidad fr ON fr.serie_id = cur.serie_id AND fr.fecha_corte = cur.t
        LEFT JOIN fact_diario d ON d.serie_id = cur.serie_id AND d.fecha_corte = cur.t""")
    series = q("SELECT serie_id, serie_label, participacion FROM dim_serie")
    funds = q("""SELECT fondo_id, fondo_label, gestora, clase_activo, categoria_sific, fuente_categoria,
                        tipo_fic, lva, en_fichas, codigos_sfc FROM dim_fondo""")
    u = (risk.merge(series, on="serie_id", how="left").merge(funds, on="fondo_id", how="left")
         .merge(rets, left_on="serie_id", right_index=True, how="left"))
    for col in ("clase_activo", "categoria_sific"):
        u[col] = u[col].fillna("Sin clasificar")
    u["serie_label"] = u["serie_label"].fillna(u["serie_id"])
    u["rent_vol_1a"] = u["rent_1a"] / u["vol_1a"].where(u["vol_1a"] > 0)
    # main class of each fund: the largest AUM at the cutoff
    u["principal"] = u.index.isin(u.assign(a=u["aum"].fillna(0)).groupby("fondo_id")["a"].idxmax())
    return u


@st.cache_data(show_spinner=False)
def fees_until(signature: tuple) -> str:
    """Last month with a factsheet fee linked to an SFC class (YYYY-MM)."""
    return q("""SELECT max(periodo) AS p FROM fact_fichas
                WHERE serie_id IS NOT NULL AND comision_administracion IS NOT NULL""")["p"].iloc[0] or "-"


def peer_ranks(u: pd.DataFrame, cols: dict[str, str]) -> pd.DataFrame:
    """Quartile (Q1 = best 25%) and position within the SIFIC category, for each return column of
    `cols`: column -> suffix of the new columns cuartil_<suffix> and puesto_<suffix>."""
    u = u.copy()
    for col, h in cols.items():
        g = u.groupby("categoria_sific")[col]
        n = g.transform("count")
        pos = g.rank(ascending=False, method="min")
        share = g.rank(ascending=True, pct=True)
        ok = u[col].notna() & (n >= MIN_PEERS)
        u[f"cuartil_{h}"] = np.where(ok, "Q" + np.ceil((1 - share + 1e-9) * 4).clip(1, 4).astype("Int64").astype(str), "")
        u[f"puesto_{h}"] = np.where(ok, pos.astype("Int64").astype(str) + " de " + n.astype(str), "")
    return u


@st.cache_data(show_spinner=False)
def unit_values(signature: tuple, ids: tuple, start: date, end: date) -> pd.DataFrame:
    return q(f"""
        SELECT serie_id, fecha_corte, valor_unidad, rent_diaria, es_salto FROM fact_rentabilidad
        WHERE serie_id IN ({_in(ids)}) AND fecha_corte BETWEEN ? AND ? AND valor_unidad IS NOT NULL
        ORDER BY serie_id, fecha_corte""", [start, end])


@st.cache_data(show_spinner=False)
def rolling_returns(signature: tuple, ids: tuple, start: date, end: date, interval: str) -> pd.DataFrame:
    return q(f"""
        WITH p AS (SELECT serie_id, fecha_corte, valor_unidad FROM fact_rentabilidad
                   WHERE serie_id IN ({_in(ids)}) AND valor_unidad IS NOT NULL AND fecha_corte <= ?),
        x AS (SELECT serie_id, fecha_corte, valor_unidad, (fecha_corte - INTERVAL {interval})::DATE AS target
              FROM p WHERE fecha_corte >= ?)
        SELECT x.serie_id, x.fecha_corte,
               CASE WHEN x.target - p.fecha_corte <= {MAX_START_GAP_DAYS}
                    THEN power(x.valor_unidad / p.valor_unidad, 365.0 / (x.fecha_corte - p.fecha_corte)) - 1 END AS r
        FROM x ASOF LEFT JOIN p ON x.serie_id = p.serie_id AND x.target >= p.fecha_corte
        ORDER BY 1, 2""", [end, start])


@st.cache_data(show_spinner=False)
def rolling_vol(signature: tuple, ids: tuple, start: date, end: date) -> pd.DataFrame:
    return q(f"""
        WITH w AS (
            SELECT serie_id, fecha_corte,
                   stddev_samp(CASE WHEN NOT es_salto THEN rent_diaria END) OVER win * sqrt(365) AS vol,
                   count(rent_diaria) OVER win AS n
            FROM fact_rentabilidad
            WHERE serie_id IN ({_in(ids)}) AND fecha_corte BETWEEN ?::DATE - INTERVAL 1 YEAR AND ?
            WINDOW win AS (PARTITION BY serie_id ORDER BY fecha_corte
                           RANGE BETWEEN INTERVAL 364 DAY PRECEDING AND CURRENT ROW))
        SELECT serie_id, fecha_corte, vol FROM w
        WHERE fecha_corte >= ? AND n >= {MIN_COVERAGE} * 365 ORDER BY 1, 2""", [start, end, start])


@st.cache_data(show_spinner=False)
def aum_history(signature: tuple, fund_ids: tuple, start: date, end: date) -> pd.DataFrame:
    """Month-end AUM per fund (sum of its classes; each class at its last report of the month)."""
    return q(f"""
        WITH m AS (SELECT serie_id, fondo_id, date_trunc('month', fecha_corte) AS mes,
                          arg_max(valor_fondo, fecha_corte) AS aum
                   FROM fact_diario
                   WHERE fondo_id IN ({_in(fund_ids)}) AND fecha_corte BETWEEN ? AND ? AND valor_fondo IS NOT NULL
                   GROUP BY ALL)
        SELECT mes, fondo_id, sum(aum) AS aum FROM m GROUP BY ALL ORDER BY 1""", [start, end])


@st.cache_data(show_spinner=False)
def split_histories(signature: tuple) -> pd.DataFrame:
    """Active funds whose short name is shared with an inactive fund: usually the same fund
    under an older SFC code, so its history (and 3/5-year metrics) starts at the code change."""
    return q("""
        SELECT a.fondo_label, a.gestora, a.codigos_sfc AS codigo_actual, a.primera_fecha AS desde,
               string_agg(o.codigos_sfc || ' (hasta ' || o.ultima_fecha || ')', '; ') AS codigos_anteriores
        FROM dim_fondo a JOIN dim_fondo o
          ON o.nombre_corto = a.nombre_corto AND o.fondo_id <> a.fondo_id AND NOT o.activo_sfc
         AND o.ultima_fecha <= a.primera_fecha + INTERVAL 31 DAY
        WHERE a.activo_sfc GROUP BY ALL ORDER BY a.gestora, a.fondo_label""")


@st.cache_data(show_spinner="Leyendo la rentabilidad bruta...")
def gross_universe(signature: tuple, cutoff: date) -> pd.DataFrame:
    """One row per fund with a gross base 100 reported in the ACTIVE_DAYS before the cutoff: the
    figures of fact_rentabilidad_bruta on that date, the fund's AUM (all its classes) and names."""
    c = _d(cutoff)
    metrics = ", ".join(f"b.rent_bruta_{n}a_ea, b.vol_bruta_{n}a, b.saltos_bruta_{n}a" for n in GROSS_YEARS)
    g = q(f"""
        WITH cur AS (SELECT fondo_id, max(fecha_corte) AS t FROM fact_rentabilidad_bruta
                     WHERE fecha_corte BETWEEN {c} - INTERVAL {ACTIVE_DAYS} DAY AND {c} GROUP BY fondo_id),
             now AS (SELECT b.* FROM cur JOIN fact_rentabilidad_bruta b ON b.fondo_id = cur.fondo_id AND b.fecha_corte = cur.t),
             aum AS (SELECT d.fondo_id, sum(d.valor_fondo) AS aum_fondo
                     FROM fact_diario d JOIN cur ON cur.fondo_id = d.fondo_id AND cur.t = d.fecha_corte GROUP BY 1),
             ini AS (SELECT b.fondo_id, min(b.fecha_corte) AS inicio_base_100
                     FROM fact_rentabilidad_bruta b JOIN now USING (fondo_id, tramo) GROUP BY 1)
        SELECT b.fondo_id, b.fecha_corte AS fecha_dato, b.comision_ea AS comision, {metrics},
               aum.aum_fondo, ini.inicio_base_100, r.participacion_elegida, r.alerta,
               f.fondo_label, f.gestora, f.clase_activo, f.categoria_sific, f.fuente_categoria, f.tipo_fic
        FROM now b LEFT JOIN aum USING (fondo_id) LEFT JOIN ini USING (fondo_id)
        LEFT JOIN resumen_bruta r USING (fondo_id) LEFT JOIN dim_fondo f USING (fondo_id)""")
    for col in ("clase_activo", "categoria_sific"):
        g[col] = g[col].fillna("Sin clasificar")
    g["saltos"] = g[[f"saltos_bruta_{n}a" for n in GROSS_YEARS]].max(axis=1).fillna(0)
    return g


@st.cache_data(show_spinner=False)
def gross_history(signature: tuple, fund_ids: tuple, start: date, end: date) -> pd.DataFrame:
    """Base 100 and 1-year gross volatility of the funds, in the tramo each fund is in at `end`
    (a base 100 that restarted after a long hole is shown from its restart)."""
    return q(f"""
        WITH k AS (SELECT fondo_id, arg_max(tramo, fecha_corte) AS tramo FROM fact_rentabilidad_bruta
                   WHERE fondo_id IN ({_in(fund_ids)}) AND fecha_corte <= ? GROUP BY fondo_id)
        SELECT b.fondo_id, b.fecha_corte, b.base_100, b.vol_bruta_1a
        FROM fact_rentabilidad_bruta b JOIN k USING (fondo_id, tramo)
        WHERE b.fecha_corte BETWEEN ? AND ? ORDER BY 1, 2""", [end, start, end])


def named(df: pd.DataFrame, labels: dict) -> pd.DataFrame:
    return df.assign(participacion=df["serie_id"].map(labels).fillna(df["serie_id"]))


def line(df: pd.DataFrame, y: str, title: str, percent: bool = True):
    fig = px.line(df, x="fecha_corte", y=y, color="participacion",
                  labels={"fecha_corte": "", y: title, "participacion": ""})
    if percent:
        fig.update_yaxes(tickformat=".1%")
    fig.update_layout(legend=dict(orientation="h", y=-0.25), hovermode="x unified", margin=dict(t=30))
    return fig


def to_excel(df: pd.DataFrame, pct_cols: list[str]) -> bytes:
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xw:
        df.to_excel(xw, sheet_name="ranking", index=False)
        ws = xw.sheets["ranking"]
        ws.freeze_panes = "B2"
        for i, col in enumerate(df.columns, start=1):
            letter = ws.cell(row=1, column=i).column_letter
            ws.column_dimensions[letter].width = 45 if i == 1 else 14
            if col in pct_cols:
                for cell in ws[letter][1:]:
                    cell.number_format = "0.00%"
    return buf.getvalue()


def peer_chart(df: pd.DataFrame, y: str, title: str, focus: str, percent: bool = False):
    """Lines of real funds: the chosen one in colour, its peers in grey (their names on hover)."""
    names = [n for n in df["participacion"].unique() if n != focus] + [focus]       # the chosen one on top
    fig = px.line(df, x="fecha_corte", y=y, color="participacion", category_orders={"participacion": names},
                  color_discrete_map={n: FOCUS_COLOR if n == focus else PEER_COLOR for n in names},
                  labels={"fecha_corte": "", y: title, "participacion": ""})
    fig.for_each_trace(lambda t: t.update(line_width=3 if t.name == focus else 1.2))
    if percent:
        fig.update_yaxes(tickformat=".1%")
    fig.update_layout(showlegend=False, hovermode="x unified", margin=dict(t=30))
    return fig


def nota(muestra: str, no_incluye: str) -> None:
    """Under every table and chart: what it shows, and what it leaves out."""
    st.caption(f"**Qué muestra:** {muestra}  \n**Qué no incluye:** {no_incluye}")


def window_start(periodo: str, cutoff: date) -> date:
    years = {"1 año": 1, "3 años": 3, "5 años": 5}.get(periodo)
    return date(cutoff.year - 1, 12, 31) if years is None else date(cutoff.year - years, cutoff.month, min(cutoff.day, 28))


PERIODOS = ["Año corrido", "1 año", "3 años", "5 años"]
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

# ---------- page -------------------------------------------------------------------
try:
    sig = _signature()
except RuntimeError as e:
    st.error(f"{e}\n\nEn el PC del pipeline: python pipeline.py --desde metricas (y --publicar).")
    st.stop()
d_min, d_max, d_ok = load_dates(sig)
fichas_hasta = fees_until(sig)
publicado = data_stamp()

with st.sidebar:
    st.header("Filtros")
    cutoff = st.date_input("Fecha de corte", value=d_ok, min_value=d_min, max_value=d_max,
                           help="Por defecto el último día completo (los últimos días suelen venir parciales).")
    u_all = universe(sig, cutoff)

    clases = sorted(u_all["clase_activo"].unique(), key=lambda c: (c != DEFAULT_CLASS, c == "Sin clasificar", c))
    sel_clases = st.multiselect("Clase de activo", clases, default=[c for c in clases if c == DEFAULT_CLASS] or clases)
    in_class = u_all[u_all["clase_activo"].isin(sel_clases)]
    counts = in_class.drop_duplicates("fondo_id")["categoria_sific"].value_counts()
    sel_cats = st.multiselect("Categoría", sorted(counts.index), default=sorted(counts.index),
                              format_func=lambda c: f"{c} ({counts.get(c, 0)})",
                              help="Categoría SIFIC y número de fondos. Su fuente (propia, oficial de LVA, investigada "
                                   "o inferida) está en cada tabla y en 'Calidad de datos'.")
    gestoras = sorted(in_class["gestora"].dropna().unique())
    sel_gest = st.multiselect("Gestora", gestoras, placeholder="Todas")
    tipos = sorted(in_class["tipo_fic"].dropna().unique())
    sel_tipos = st.multiselect("Tipo de fondo", tipos, placeholder="Todos")
    with st.expander("Opciones", expanded=False):
        modo = st.radio("Participaciones (pestañas netas)", ["Una por fondo (mayor AUM)", "Todas"],
                        help="Una por fondo evita que un fondo con muchas clases pese más en el ranking neto.")
        con_inferidas = st.checkbox("Incluir categorías inferidas", value=True,
                                    help="Inferidas por el nombre, el subtipo SFC o la volatilidad. Las propias, las "
                                         "de LVA y las investigadas fondo por fondo se incluyen siempre.")
        sin_saltos = st.checkbox("Ocultar los que tienen saltos", value=True,
                                 help="Saltos diarios de 20% o más o reinicios de la unidad: en 3 años para las "
                                      "participaciones, en el periodo más largo disponible para la bruta por fondo.")
        aum_min = st.number_input("AUM mínimo (COP mil millones)", min_value=0.0, value=0.0, step=10.0,
                                  help="De la participación en las pestañas netas; del fondo en las de bruta.")

    def sliced(df: pd.DataFrame, aum_col: str) -> pd.DataFrame:
        """The rows of df that pass the filters of the side bar (the same for every tab)."""
        out = df[df["clase_activo"].isin(sel_clases) & df["categoria_sific"].isin(sel_cats)
                 & (df[aum_col].fillna(0) >= aum_min * 1e9)]
        if sel_gest:
            out = out[out["gestora"].isin(sel_gest)]
        if sel_tipos:
            out = out[out["tipo_fic"].isin(sel_tipos)]
        if not con_inferidas:
            out = out[out["fuente_categoria"].isin(CONFIRMED + RESEARCHED)]
        return out

    # net: one row per class
    base = u_all if modo == "Todas" else u_all[u_all["principal"]]
    if sin_saltos:
        base = base[base["saltos"].fillna(0) == 0]
    base = peer_ranks(base, {"rent_1a": "1a", "rent_3a": "3a"})     # against every peer in the category
    v = sliced(base, "aum").sort_values("aum", ascending=False)
    # every fund of the filter, whatever its jumps (all its classes together)
    en_filtro = sliced(u_all.assign(aum_f=u_all.groupby("fondo_id")["aum"].transform("sum"))
                       .drop_duplicates("fondo_id"), "aum_f").sort_values("aum_f", ascending=False)
    # gross: one row per fund
    g_all = gross_universe(sig, cutoff)
    g_cov = sliced(g_all, "aum_fondo")                              # coverage, whatever the jumps
    g_rank = peer_ranks(g_all[g_all["saltos"] == 0] if sin_saltos else g_all,
                        {f"rent_bruta_{n}a_ea": f"bruta_{n}a" for n in GROSS_YEARS})
    g = sliced(g_rank, "aum_fondo").sort_values("aum_fondo", ascending=False)
    labels = dict(zip(u_all["serie_id"], u_all["serie_label"]))
    sel = st.multiselect("Participaciones para los gráficos netos", v["serie_id"].tolist(),
                         default=v["serie_id"].head(DEFAULT_SELECTED).tolist(),
                         format_func=lambda s: labels.get(s, s),
                         help=f"Por defecto las {DEFAULT_SELECTED} de mayor AUM; máximo {MAX_SERIES} en los gráficos.")

st.title("Fondos de inversión colectiva de Colombia")
st.markdown(
    f"**Datos SFC** (Formato 523): hasta {d_max:%Y-%m-%d}, completos hasta {d_ok:%Y-%m-%d} &nbsp;·&nbsp; "
    f"**Comisiones** (fichas técnicas): hasta {fichas_hasta} &nbsp;·&nbsp; "
    f"**{'Exportado' if LOCAL else 'Publicado'}**: {publicado - timedelta(hours=5):%Y-%m-%d %H:%M} (hora de Colombia) "
    f"&nbsp;·&nbsp; **Corte elegido**: {cutoff:%Y-%m-%d}")
st.caption("Rentabilidades efectivas anuales (año de 365 días). **Neta**: después de comisiones, una por participación "
           "(valor de la unidad que reporta la SFC). **Bruta**: antes de la comisión de administración, una sola por "
           "fondo. Cada cifra es de un fondo o de una participación: no hay promedios por categoría. El detalle está "
           "en la pestaña «Qué es y qué no es».")
dias_sin_publicar = (datetime.now(timezone.utc) - publicado).days
if not LOCAL and dias_sin_publicar > STALE_DAYS:
    st.warning(f"La última publicación tiene {dias_sin_publicar} días: la actualización diaria no ha corrido. "
               "Los datos de abajo son los de esa fecha.")
if cutoff > d_ok:
    st.warning("La fecha de corte es posterior al último día completo: muchas participaciones aún no reportan ese día.")
if v.empty:
    st.info("Ningún fondo cumple los filtros a esta fecha de corte.")
    st.stop()

n_filtro = en_filtro["fondo_id"].nunique()
aum_filtro = en_filtro["aum_f"].sum()
cobertura = min(g_cov["aum_fondo"].sum() / aum_filtro, 1.0) if aum_filtro else np.nan
k = st.columns(5)
k[0].metric("Fondos del filtro", f"{n_filtro}")
k[1].metric("Participaciones", f"{len(sliced(u_all, 'aum'))}")
k[2].metric("AUM", cop_bn(aum_filtro))
k[3].metric("Fondos con rentabilidad bruta", f"{len(g_cov)} de {n_filtro}")
k[4].metric("AUM con rentabilidad bruta", pct(cobertura))

(tab_bruta, tab_ranking, tab_fondo, tab_rent, tab_vol, tab_riesgo, tab_aum, tab_calidad, tab_que) = st.tabs(
    ["Bruta por fondo", "Neta por participación", "Un fondo", "Rentabilidad neta", "Volatilidad y caídas (neta)",
     "Riesgo vs retorno (neta)", "AUM y mercado", "Calidad de datos", "Qué es y qué no es"])
plot_ids = tuple(sel[:MAX_SERIES])
sin_bruta_txt = (f"{n_filtro - len(g_cov)} de {n_filtro} fondos del filtro no tienen rentabilidad bruta porque no hay "
                 "una comisión de administración de ficha técnica enlazada (lista en «Calidad de datos»)")
ocultos_bruta = len(g_cov) - len(g)

with tab_bruta:
    st.subheader("Rentabilidad bruta por fondo (antes de la comisión de administración)")
    if g.empty:
        st.info("Ningún fondo del filtro tiene rentabilidad bruta a esta fecha: hace falta su comisión en las fichas "
                "técnicas (ver «Calidad de datos»).")
    else:
        kb = st.columns(4)
        kb[0].metric("Fondos en la tabla", f"{len(g)}")
        kb[1].metric("AUM de esos fondos", cop_bn(g["aum_fondo"].sum()))
        kb[2].metric("Con rentabilidad bruta de 1 año", f"{g['rent_bruta_1a_ea'].notna().sum()}")
        kb[3].metric(f"Con rentabilidad bruta de {GROSS_YEARS[-1]} años",
                     f"{g[f'rent_bruta_{GROSS_YEARS[-1]}a_ea'].notna().sum()}")

        orden = st.radio("Ordenar y comparar por la rentabilidad bruta de",
                         [f"{n} año{'s' if n > 1 else ''}" for n in GROSS_YEARS], horizontal=True, key="orden_bruta")
        n_ord = int(orden.split()[0])
        rcols = [f"rent_bruta_{n}a_ea" for n in GROSS_YEARS]
        vcols = [f"vol_bruta_{n}a" for n in GROSS_YEARS]
        pos_cols = [f"puesto_bruta_{n_ord}a", f"cuartil_bruta_{n_ord}a"]
        gcols = (["fondo_label", "gestora", "categoria_sific", "fuente_categoria", "tipo_fic", "aum_fondo", "comision"]
                 + rcols + pos_cols + vcols + ["inicio_base_100", "saltos", "alerta", "participacion_elegida", "fecha_dato"])
        tg = (g.assign(fuente_categoria=g["fuente_categoria"].replace(SOURCE_LABELS))[gcols]
              .sort_values(f"rent_bruta_{n_ord}a_ea", ascending=False, na_position="last"))
        gp = rcols + vcols + ["comision"]
        shown_g = tg.copy()
        shown_g[gp] = shown_g[gp] * 100
        shown_g["aum_fondo"] = shown_g["aum_fondo"] / 1e9
        st.dataframe(shown_g, hide_index=True, height=480, column_config={
            "fondo_label": st.column_config.TextColumn("Fondo", width="large"),
            "gestora": "Gestora", "categoria_sific": "Categoría", "fuente_categoria": "Fuente categoría", "tipo_fic": "Tipo",
            "aum_fondo": st.column_config.NumberColumn("AUM del fondo (mil M COP)", format="%.0f"),
            "comision": st.column_config.NumberColumn("Comisión usada", format="%.2f %%"),
            **{f"rent_bruta_{n}a_ea": st.column_config.NumberColumn(f"Bruta {n} año{'s' if n > 1 else ''} EA", format="%.2f %%")
               for n in GROSS_YEARS},
            pos_cols[0]: f"Puesto en su categoría ({orden})", pos_cols[1]: f"Cuartil ({orden})",
            **{f"vol_bruta_{n}a": st.column_config.NumberColumn(f"Vol. bruta {n} año{'s' if n > 1 else ''}", format="%.2f %%")
               for n in GROSS_YEARS},
            "inicio_base_100": st.column_config.DateColumn("Base 100 desde"),
            "saltos": st.column_config.NumberColumn("Saltos", format="%d"),
            "alerta": st.column_config.TextColumn("Alerta", width="large"),
            "participacion_elegida": "Participación usada",
            "fecha_dato": st.column_config.DateColumn("Dato SFC"),
        })
        nota("una fila por fondo, al corte: rentabilidad efectiva anual y volatilidad de su base 100 bruta, que se mueve "
             "con la rentabilidad diaria de una participación del fondo más su comisión de administración. Puesto y "
             f"cuartil: frente a todos los fondos de la misma categoría con rentabilidad bruta de {orden} al corte "
             "(Q1 = el mejor 25%), sin importar gestora, tipo o AUM.",
             f"{sin_bruta_txt}"
             + (f"; {ocultos_bruta} fondos ocultos por tener saltos (opción de la barra lateral)" if ocultos_bruta else "")
             + ". Una cifra en blanco: la base 100 actual aún no cubre ese periodo (fondo nuevo, o base reiniciada tras un "
               "hueco de más de 5 días). No hay promedio de la categoría.")
        st.download_button("Descargar Excel", to_excel(tg, gp), file_name=f"bruta_por_fondo_{cutoff:%Y%m%d}.xlsx",
                           mime=XLSX, key="xlsx_bruta")

        nombres = dict(zip(g["fondo_id"], g["fondo_label"]))
        sel_fondos = st.multiselect("Fondos para los gráficos de bruta", g["fondo_id"].tolist(),
                                    default=g["fondo_id"].head(DEFAULT_SELECTED).tolist(),
                                    format_func=lambda f: nombres.get(f, f), key="fondos_bruta",
                                    help=f"Por defecto los {DEFAULT_SELECTED} de mayor AUM; máximo {MAX_SERIES}.")
        if sel_fondos:
            periodo_b = st.radio("Periodo", PERIODOS, index=1, horizontal=True, key="periodo_bruta")
            ini_b = max(window_start(periodo_b, cutoff), d_min)
            gh = gross_history(sig, tuple(sel_fondos[:MAX_SERIES]), ini_b, cutoff)
            if gh.empty:
                st.info("Sin base 100 en el periodo.")
            else:
                gh["participacion"] = gh["fondo_id"].map(nombres).fillna(gh["fondo_id"])
                gh["indice"] = gh["base_100"] / gh.groupby("fondo_id")["base_100"].transform("first") * 100
                st.markdown(f"**Base 100 bruta por fondo** — 100 el {ini_b:%Y-%m-%d}")
                st.plotly_chart(line(gh, "indice", "Base 100 bruta", percent=False), key="chart_base_100")
                tarde = gh.groupby("participacion")["fecha_corte"].min()
                tarde = [f for f, d0 in tarde.items() if d0 > pd.Timestamp(ini_b) + pd.Timedelta(days=MAX_START_GAP_DAYS)]
                nota("cuánto valdrían 100 invertidos al inicio del periodo si no se cobrara la comisión de "
                     "administración, para los fondos elegidos.",
                     "fondos no elegidos y fondos sin rentabilidad bruta."
                     + (f" Empiezan después (fondo nuevo o base 100 reiniciada): {', '.join(tarde)}." if tarde else ""))
                gv = gh.dropna(subset=["vol_bruta_1a"])
                if not gv.empty:
                    st.markdown("**Volatilidad bruta de 1 año por fondo** (anualizada)")
                    st.plotly_chart(line(gv, "vol_bruta_1a", "Volatilidad bruta 1 año"), key="chart_vol_bruta")
                    nota("desviación estándar de los movimientos diarios de la base 100 bruta del último año × √365, "
                         "en cada fecha.", "fechas en las que el fondo aún no tiene un año de base 100.")

with tab_ranking:
    st.subheader("Rentabilidad neta por participación (después de comisiones)")
    order = st.radio("Ordenar por la rentabilidad neta de", [lbl for lbl, _ in HORIZONS.values()], index=3, horizontal=True)
    sort_col = next(k_ for k_, (lbl, _) in HORIZONS.items() if lbl == order)
    rent_cols = list(HORIZONS)
    cols = (["serie_label", "gestora", "categoria_sific", "tipo_fic", "aum", "inversionistas"] + rent_cols
            + ["vol_1a", "vol_3a", "caida_1a", "rent_vol_1a", "cuartil_1a", "puesto_1a", "cuartil_3a",
               "comision", "ficha_fecha", "fuente_categoria", "fecha_dato"])
    t = (v.assign(fuente_categoria=v["fuente_categoria"].replace(SOURCE_LABELS))[cols]
         .sort_values(sort_col, ascending=False, na_position="last"))
    pcols = rent_cols + ["vol_1a", "vol_3a", "caida_1a", "comision"]
    shown = t.copy()
    shown[pcols] = shown[pcols] * 100
    shown["aum"] = shown["aum"] / 1e9
    st.dataframe(shown, hide_index=True, height=560, column_config={
        "serie_label": st.column_config.TextColumn("Participación", width="large"),
        "gestora": "Gestora", "categoria_sific": "Categoría", "tipo_fic": "Tipo",
        "aum": st.column_config.NumberColumn("AUM (mil M COP)", format="%.0f"),
        "inversionistas": st.column_config.NumberColumn("Inversionistas", format="%d"),
        **{c: st.column_config.NumberColumn(f"Neta {HORIZONS[c][0]} EA", format="%.2f %%") for c in rent_cols},
        "vol_1a": st.column_config.NumberColumn("Vol. 1 año", format="%.2f %%"),
        "vol_3a": st.column_config.NumberColumn("Vol. 3 años", format="%.2f %%"),
        "caida_1a": st.column_config.NumberColumn("Máx. caída 1 año", format="%.2f %%"),
        "rent_vol_1a": st.column_config.NumberColumn("Rent/Vol 1 año", format="%.2f"),
        "cuartil_1a": "Cuartil 1 año", "puesto_1a": "Puesto 1 año", "cuartil_3a": "Cuartil 3 años",
        "comision": st.column_config.NumberColumn("Comisión (ficha)", format="%.2f %%"),
        "ficha_fecha": st.column_config.DateColumn("Fecha ficha"),
        "fuente_categoria": "Fuente categoría",
        "fecha_dato": st.column_config.DateColumn("Dato SFC"),
    })
    nota("una fila por participación (por defecto la de mayor AUM de cada fondo), al corte: rentabilidad efectiva anual "
         "neta calculada con el valor de la unidad, volatilidad, máxima caída, y cuartil y puesto frente a todas las "
         "participaciones de la misma categoría (Q1 = el mejor 25%), sin importar gestora, tipo o AUM.",
         f"{len(sliced(u_all if modo == 'Todas' else u_all[u_all['principal']], 'aum')) - len(v)} participaciones ocultas "
         "por tener saltos (opción de la barra lateral). Comisión: la de la última ficha técnica hasta el corte; en "
         "blanco si no hay ficha enlazada. Una rentabilidad en blanco: la participación no tiene historia suficiente.")
    st.download_button("Descargar Excel", to_excel(t, pcols), file_name=f"ranking_fic_{cutoff:%Y%m%d}.xlsx", mime=XLSX)

with tab_fondo:
    nombres_f = dict(zip(en_filtro["fondo_id"], en_filtro["fondo_label"]))
    fid = st.selectbox("Fondo", en_filtro["fondo_id"].tolist(), format_func=lambda f: nombres_f.get(f, f),
                       help="Fondos del filtro de la barra lateral, de mayor a menor AUM.")
    info = en_filtro[en_filtro["fondo_id"] == fid].iloc[0]
    clases_f = u_all[u_all["fondo_id"] == fid].sort_values("aum", ascending=False)
    st.subheader(str(info["fondo_label"]))
    st.markdown(
        f"**Gestora:** {info['gestora']} &nbsp;·&nbsp; **Categoría:** {info['clase_activo']} > {info['categoria_sific']} "
        f"(fuente: {SOURCE_LABELS.get(info['fuente_categoria'], info['fuente_categoria'])}) &nbsp;·&nbsp; "
        f"**Tipo:** {info['tipo_fic']} &nbsp;·&nbsp; **AUM del fondo:** {cop_bn(info['aum_f'])} &nbsp;·&nbsp; "
        f"**Participaciones con datos al corte:** {len(clases_f)}")
    periodo_f = st.radio("Periodo de los gráficos", PERIODOS, index=1, horizontal=True, key="periodo_fondo")
    ini_f = max(window_start(periodo_f, cutoff), d_min)

    st.markdown("#### Rentabilidad bruta (antes de la comisión de administración, una sola para el fondo)")
    fila = g_rank[g_rank["fondo_id"] == fid]
    if fila.empty:
        st.info("Este fondo tiene rentabilidad bruta, pero está oculto por tener saltos (opción de la barra lateral)."
                if fid in set(g_all["fondo_id"]) else
                "Este fondo no tiene rentabilidad bruta: no hay una comisión de administración de ficha técnica "
                "enlazada a alguna de sus participaciones. Abajo están sus cifras netas.")
    else:
        r = fila.iloc[0]
        st.dataframe(pd.DataFrame({
            "Periodo": [f"{n} año{'s' if n > 1 else ''}" for n in GROSS_YEARS],
            "Rentabilidad bruta EA": [r[f"rent_bruta_{n}a_ea"] * 100 for n in GROSS_YEARS],
            "Volatilidad bruta": [r[f"vol_bruta_{n}a"] * 100 for n in GROSS_YEARS],
            "Puesto en su categoría": [r[f"puesto_bruta_{n}a"] for n in GROSS_YEARS],
            "Cuartil": [r[f"cuartil_bruta_{n}a"] for n in GROSS_YEARS],
        }), hide_index=True, column_config={
            "Rentabilidad bruta EA": st.column_config.NumberColumn(format="%.2f %%"),
            "Volatilidad bruta": st.column_config.NumberColumn(format="%.2f %%")})
        st.caption(f"Comisión de administración usada: {pct(r['comision'])}, de «{r['participacion_elegida']}». "
                   f"Base 100 desde {pd.Timestamp(r['inicio_base_100']):%Y-%m-%d}. Dato SFC del "
                   f"{pd.Timestamp(r['fecha_dato']):%Y-%m-%d}."
                   + (f" Alerta: {r['alerta']}." if isinstance(r["alerta"], str) and r["alerta"] else ""))
        pares = g_rank[(g_rank["categoria_sific"] == r["categoria_sific"]) & (g_rank["fondo_id"] != fid)]
        pares = pares.sort_values("aum_fondo", ascending=False)
        ids_b = tuple(pares["fondo_id"].head(MAX_SERIES - 1)) + (fid,)
        nombres_b = dict(zip(g_rank["fondo_id"], g_rank["fondo_label"]))
        gh = gross_history(sig, ids_b, ini_f, cutoff)
        if not gh.empty and fid in set(gh["fondo_id"]):
            gh["participacion"] = gh["fondo_id"].map(nombres_b).fillna(gh["fondo_id"])
            gh["indice"] = gh["base_100"] / gh.groupby("fondo_id")["base_100"].transform("first") * 100
            st.markdown(f"**Base 100 bruta: el fondo frente a los fondos de su categoría** — 100 el {ini_f:%Y-%m-%d}")
            st.plotly_chart(peer_chart(gh, "indice", "Base 100 bruta", nombres_b.get(fid, fid)), key="chart_fondo_bruta")
            nota(f"en rojo, {nombres_b.get(fid, fid)}; en gris, {len(ids_b) - 1} fondos reales de la categoría "
                 f"{r['categoria_sific']} (los de mayor AUM con rentabilidad bruta). Pasa el cursor para ver cada nombre.",
                 f"{max(len(pares) - (MAX_SERIES - 1), 0)} fondos más de la categoría con rentabilidad bruta, y los que "
                 "no la tienen. No hay línea de promedio: cada línea es un fondo.")
        else:
            st.info("El fondo no tiene base 100 bruta en el periodo elegido.")

    st.markdown("#### Rentabilidad neta por participación (después de comisiones)")
    rent_cols_f = list(HORIZONS)
    tf = clases_f[["serie_label", "aum", "inversionistas"] + rent_cols_f + ["vol_1a", "caida_1a", "comision", "ficha_fecha"]].copy()
    pf = rent_cols_f + ["vol_1a", "caida_1a", "comision"]
    tf[pf] = tf[pf] * 100
    tf["aum"] = tf["aum"] / 1e9
    st.dataframe(tf, hide_index=True, column_config={
        "serie_label": st.column_config.TextColumn("Participación", width="large"),
        "aum": st.column_config.NumberColumn("AUM (mil M COP)", format="%.0f"),
        "inversionistas": st.column_config.NumberColumn("Inversionistas", format="%d"),
        **{c: st.column_config.NumberColumn(f"Neta {HORIZONS[c][0]} EA", format="%.2f %%") for c in rent_cols_f},
        "vol_1a": st.column_config.NumberColumn("Vol. 1 año", format="%.2f %%"),
        "caida_1a": st.column_config.NumberColumn("Máx. caída 1 año", format="%.2f %%"),
        "comision": st.column_config.NumberColumn("Comisión (ficha)", format="%.2f %%"),
        "ficha_fecha": st.column_config.DateColumn("Fecha ficha"),
    })
    principal = clases_f.iloc[0]
    pares_n = u_all[u_all["principal"] & (u_all["categoria_sific"] == info["categoria_sific"]) & (u_all["fondo_id"] != fid)]
    pares_n = pares_n.sort_values("aum", ascending=False).head(MAX_SERIES - 1)
    ids_n = tuple(pares_n["serie_id"]) + (principal["serie_id"],)
    nombres_n = {**dict(zip(pares_n["serie_id"], pares_n["fondo_label"])), principal["serie_id"]: str(info["fondo_label"])}
    vu = unit_values(sig, ids_n, ini_f, cutoff)
    if not vu.empty and principal["serie_id"] in set(vu["serie_id"]):
        vu["participacion"] = vu["serie_id"].map(nombres_n)
        vu["indice"] = vu["valor_unidad"] / vu.groupby("serie_id")["valor_unidad"].transform("first") * 100
        st.markdown(f"**Valor de 100 invertidos, neto: el fondo frente a los fondos de su categoría** — 100 el {ini_f:%Y-%m-%d}")
        st.plotly_chart(peer_chart(vu, "indice", "Valor de 100 invertidos (neto)", str(info["fondo_label"])),
                        key="chart_fondo_neta")
        nota(f"en rojo, la participación de mayor AUM del fondo ({principal['serie_label']}); en gris, la de mayor AUM "
             f"de {len(pares_n)} fondos reales de la categoría {info['categoria_sific']} (los más grandes).",
             "las demás participaciones de cada fondo (cada una cobra una comisión distinta) y los demás fondos de la "
             "categoría. No hay línea de promedio.")

with tab_rent:
    st.subheader("Rentabilidad neta de las participaciones elegidas")
    if not plot_ids:
        st.info("Elige participaciones en la barra lateral.")
    else:
        c1, c2 = st.columns(2)
        periodo = c1.radio("Periodo", PERIODOS, index=1, horizontal=True)
        start = window_start(periodo, cutoff)
        vu = unit_values(sig, plot_ids, max(start, d_min), cutoff)
        if vu.empty:
            st.info("Sin valores de la unidad en el periodo.")
        else:
            vu["indice"] = vu["valor_unidad"] / vu.groupby("serie_id")["valor_unidad"].transform("first") * 100
            st.markdown(f"**Valor de 100 invertidos, neto** — 100 el {max(start, d_min):%Y-%m-%d}")
            st.plotly_chart(line(named(vu, labels), "indice", "Valor de 100 invertidos (neto)", percent=False))
            late = vu.groupby("serie_id")["fecha_corte"].min()
            late = [labels.get(s, s) for s, d0 in late.items() if d0 > pd.Timestamp(start) + pd.Timedelta(days=MAX_START_GAP_DAYS)]
            nota("el valor de la unidad de cada participación elegida, llevado a 100 al inicio del periodo (después de "
                 "comisiones).", "participaciones no elegidas en la barra lateral."
                 + (f" Empiezan después (100 en su primer dato): {', '.join(late)}." if late else ""))
        horizonte = c2.radio("Rentabilidad móvil", ["30 días", "180 días", "1 año", "3 años"], index=2, horizontal=True)
        interval = {"30 días": "30 DAY", "180 días": "180 DAY", "1 año": "1 YEAR", "3 años": "3 YEAR"}[horizonte]
        rr = rolling_returns(sig, plot_ids, max(start, d_min), cutoff, interval).dropna()
        if not rr.empty:
            st.markdown(f"**Rentabilidad neta móvil de {horizonte}** (efectiva anual)")
            st.plotly_chart(line(named(rr, labels), "r", f"Rentabilidad neta {horizonte} EA"))
            nota("en cada fecha, la rentabilidad efectiva anual neta de la ventana que termina ese día.",
                 "fechas en las que la participación no tiene un valor de unidad al inicio de la ventana.")

with tab_vol:
    st.subheader("Volatilidad y caídas de las participaciones elegidas (neta)")
    if not plot_ids:
        st.info("Elige participaciones en la barra lateral.")
    else:
        years = st.slider("Años a mostrar", 1, 10, 3, key="vol_years")
        start = max(d_min, date(cutoff.year - years, cutoff.month, min(cutoff.day, 28)))
        rv = rolling_vol(sig, plot_ids, start, cutoff)
        if rv.empty:
            st.info("Sin volatilidad de 1 año (requiere un año de historia).")
        else:
            st.markdown("**Volatilidad neta de 1 año** (anualizada)")
            st.plotly_chart(line(named(rv, labels), "vol", "Volatilidad 1 año"))
            nota("desviación estándar de la rentabilidad diaria neta del último año × √365, en cada fecha.",
                 "los días marcados como salto, y las fechas con menos del 90% de los días del año.")
        vu = unit_values(sig, plot_ids, start, cutoff)
        if not vu.empty:
            vu["caida"] = vu["valor_unidad"] / vu.groupby("serie_id")["valor_unidad"].cummax() - 1
            st.markdown("**Caída desde el máximo** (valor de la unidad, neto)")
            st.plotly_chart(line(named(vu, labels), "caida", "Caída desde el máximo"))
            nota("cuánto está el valor de la unidad por debajo de su máximo previo dentro del periodo mostrado.",
                 "máximos anteriores al periodo mostrado.")

with tab_riesgo:
    st.subheader("Riesgo vs retorno de las participaciones del filtro (neta)")
    hz = st.radio("Horizonte", ["1 año", "3 años"], horizontal=True)
    xcol, ycol = ("vol_1a", "rent_1a") if hz == "1 año" else ("vol_3a", "rent_3a")
    sc = v.dropna(subset=[xcol, ycol]).copy()
    if sc.empty:
        st.info("Sin participaciones con volatilidad y rentabilidad para ese horizonte.")
    else:
        sc["seleccion"] = np.where(sc["serie_id"].isin(plot_ids), "Seleccionada", "Resto del filtro")
        sc["tamano"] = np.sqrt(sc["aum"].fillna(0).clip(lower=0)) + 1
        fig = px.scatter(sc, x=xcol, y=ycol, color="categoria_sific", symbol="seleccion", size="tamano",
                         size_max=28, hover_name="serie_label",
                         hover_data={"gestora": True, "tamano": False, "seleccion": False, "aum": ":,.0f"},
                         labels={xcol: f"Volatilidad neta {hz}", ycol: f"Rentabilidad neta {hz} EA",
                                 "categoria_sific": "Categoría", "seleccion": ""})
        fig.update_xaxes(tickformat=".1%")
        fig.update_yaxes(tickformat=".1%")
        fig.update_layout(legend=dict(orientation="h", y=-0.25), margin=dict(t=30))
        st.plotly_chart(fig)
        nota("cada punto es una participación del filtro: su volatilidad y su rentabilidad neta al corte. Tamaño según "
             "AUM; color según categoría. Arriba a la izquierda: más rentabilidad con menos volatilidad.",
             f"{len(v) - len(sc)} participaciones del filtro sin volatilidad o rentabilidad de {hz}.")

with tab_aum:
    st.subheader("AUM por gestora de los fondos del filtro")
    years = st.slider("Años a mostrar", 1, 10, 5, key="aum_years")
    start = max(d_min, date(cutoff.year - years, cutoff.month, 1))
    funds = v.drop_duplicates("fondo_id")
    hist = aum_history(sig, tuple(funds["fondo_id"]), start, cutoff)
    if hist.empty:
        st.info("Sin AUM en el periodo.")
    else:
        gest = funds.set_index("fondo_id")["gestora"]
        hist["gestora"] = hist["fondo_id"].map(gest).fillna("Sin gestora")
        top = hist[hist["mes"] == hist["mes"].max()].groupby("gestora")["aum"].sum().nlargest(10).index
        hist["gestora"] = hist["gestora"].where(hist["gestora"].isin(top), "Otras")
        by_g = hist.groupby(["mes", "gestora"], as_index=False)["aum"].sum()
        by_g["aum_bn"] = by_g["aum"] / 1e9
        fig = px.area(by_g, x="mes", y="aum_bn", color="gestora",
                      labels={"mes": "", "aum_bn": "AUM (mil M COP)", "gestora": ""})
        fig.update_layout(legend=dict(orientation="h", y=-0.25), hovermode="x unified", margin=dict(t=30))
        st.plotly_chart(fig)
        nota("AUM a fin de mes de los fondos del filtro que tienen datos al corte, sumado por gestora (las 10 mayores; "
             "el resto en 'Otras').",
             "fondos que ya no reportan al corte, aunque tuvieran AUM antes; la historia anterior de un fondo que cambió "
             "de código SFC y aún no está unido (ver «Calidad de datos»).")
    all_cls = u_all[u_all["fondo_id"].isin(funds["fondo_id"])]
    tbl = all_cls.groupby("gestora").agg(aum=("aum", "sum"), fondos=("fondo_id", "nunique"),
                                         inversionistas=("inversionistas", "sum")).sort_values("aum", ascending=False)
    tbl["participacion_mercado"] = tbl["aum"] / tbl["aum"].sum() * 100
    if not hist.empty:
        ago = pd.Timestamp(cutoff) - pd.DateOffset(years=1)
        month_ago = hist[hist["mes"] <= ago]["mes"].max()
        if pd.notna(month_ago):
            prev = hist.assign(g=hist["fondo_id"].map(gest))[hist["mes"] == month_ago].groupby("g")["aum"].sum()
            tbl["var_12m"] = (tbl["aum"] / prev.reindex(tbl.index) - 1) * 100
    tbl["aum"] = tbl["aum"] / 1e9
    st.dataframe(tbl.reset_index(), hide_index=True, column_config={
        "gestora": "Gestora", "aum": st.column_config.NumberColumn("AUM (mil M COP)", format="%.0f"),
        "fondos": "Fondos", "inversionistas": st.column_config.NumberColumn("Inversionistas", format="%d"),
        "participacion_mercado": st.column_config.NumberColumn("Participación", format="%.1f %%"),
        "var_12m": st.column_config.NumberColumn("Variación AUM 12 meses", format="%.1f %%"),
    })
    nota("por gestora, al corte: AUM, fondos e inversionistas de todas las participaciones de los fondos del filtro, y "
         "su parte del total del filtro.",
         "flujos netos: la variación de AUM mezcla rentabilidad con entradas y salidas de dinero. Un inversionista en "
         "dos participaciones cuenta dos veces.")

with tab_calidad:
    st.subheader("Calidad de datos de los fondos del filtro")
    funds = v.drop_duplicates("fondo_id")
    fuente = funds["fuente_categoria"]
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Categoría propia o de LVA", f"{fuente.isin(CONFIRMED).sum()} de {len(funds)}")
    c2.metric("Categoría investigada", f"{fuente.isin(RESEARCHED).sum()} de {len(funds)}")
    c3.metric("Con ficha técnica", f"{funds['en_fichas'].fillna(False).sum()} de {len(funds)}")
    c4.metric("Con rentabilidad bruta", f"{funds['fondo_id'].isin(g_all['fondo_id']).sum()} de {len(funds)}")
    st.caption(f"Fondos del filtro con al menos una participación sin saltos. Sin rentabilidad neta de 3 años: "
               f"{v['rent_3a'].isna().sum()} de {len(v)} participaciones. Rentabilidad bruta: necesita la comisión de "
               "administración del fondo en las fichas técnicas.")
    st.markdown("#### Categorías inferidas")
    inf = funds[~fuente.isin(CONFIRMED + RESEARCHED)][
        ["fondo_label", "gestora", "clase_activo", "categoria_sific", "fuente_categoria", "vol_1a"]]
    if inf.empty:
        st.write("Ninguna: todas las categorías del filtro son propias, de LVA o investigadas fondo por fondo.")
    else:
        st.dataframe(inf.assign(vol_1a=inf["vol_1a"] * 100), hide_index=True, column_config={
            "fondo_label": "Fondo", "gestora": "Gestora", "clase_activo": "Clase de activo",
            "categoria_sific": "Categoría", "fuente_categoria": "Fuente categoría",
            "vol_1a": st.column_config.NumberColumn("Vol. 1 año", format="%.2f %%")})
        if LOCAL:
            st.caption("Para fijar una categoría: llena Macrocategoria y Categoria del fondo en relaciona_nombres "
                       "(relacionamiento_nombres_v2.xlsx) y corre de nuevo el pipeline. revision_relaciones.xlsx trae "
                       "la lista completa; la evidencia de las investigadas está en 3. Relaciones/categorias_evidencia.xlsx.")
    sin_bruta = funds[~funds["fondo_id"].isin(g_all["fondo_id"])][["fondo_label", "gestora", "categoria_sific", "en_fichas"]]
    if not sin_bruta.empty:
        st.markdown("#### Fondos sin rentabilidad bruta")
        st.dataframe(sin_bruta, hide_index=True, column_config={"fondo_label": "Fondo", "gestora": "Gestora",
                                                                "categoria_sific": "Categoría", "en_fichas": "Tiene ficha"})
        st.caption("No hay una comisión de administración enlazada a alguna de sus participaciones: el fondo no tiene "
                   "ficha técnica en la base, o su ficha no se pudo enlazar con una participación de la SFC."
                   + (" Si tiene ficha, revisa su fila en 3. Relaciones/llaves_fichas.xlsx y revision_relaciones.xlsx."
                      if LOCAL else ""))
    st.markdown("#### Historias partidas por cambio de código SFC")
    sp = split_histories(sig)
    sp = sp[sp["fondo_label"].isin(funds["fondo_label"])]
    if sp.empty:
        st.write("Ninguna en el filtro.")
    else:
        st.dataframe(sp, hide_index=True)
        st.caption("Fondos activos cuyo nombre corto coincide con un fondo inactivo que dejó de reportar cuando "
                   "empezó el actual: casi siempre el mismo fondo con un código nuevo (p. ej. una fusión de "
                   "fiduciarias). Sus cifras de 1, 3 y 5 años quedan en blanco hasta que la historia se une."
                   + (" Para unirla: dales la misma Llave en relaciona_nombres; revision_relaciones.xlsx dice cuáles "
                      "están probados por la continuidad del valor de unidad." if LOCAL else ""))
    st.markdown("#### Participaciones sin datos recientes")
    stale = v[pd.to_datetime(v["fecha_dato"]) < pd.Timestamp(cutoff) - pd.Timedelta(days=2)]
    if stale.empty:
        st.write("Todas reportaron en los dos días previos al corte.")
    else:
        st.dataframe(stale[["serie_label", "gestora", "fecha_dato"]], hide_index=True, column_config={
            "serie_label": "Participación", "gestora": "Gestora", "fecha_dato": st.column_config.DateColumn("Último dato SFC")})

with tab_que:
    fondos_corte = u_all["fondo_id"].nunique()
    aum_corte = u_all["aum"].sum()
    st.subheader("Qué es y qué no es este tablero")
    a, b = st.columns(2)
    a.markdown(f"""
#### Qué es

**Las fuentes**
- **SFC, Formato 523**: valor de la unidad, valor del fondo e inversionistas, cada día, por participación.
  Desde {d_min:%Y-%m-%d} hasta {d_max:%Y-%m-%d}. El último día **completo** es {d_ok:%Y-%m-%d}: los fondos reportan
  con rezago, y por eso la fecha de corte por defecto es ese día.
- **Fichas técnicas mensuales** de las gestoras: de ellas se toma solo la comisión de administración.
  Hay comisiones hasta {fichas_hasta}.
- **Categorías**: clase de activo > categoría SIFIC. Cada fondo dice de dónde sale la suya: propia, LVA (oficial),
  investigada fondo por fondo, o inferida.

**Los cálculos**
- **Rentabilidad neta**, una por participación: `(unidad final / unidad inicial) ^ (365 / días) − 1`,
  con el valor de la unidad de la SFC. Ya descuenta comisiones y gastos.
- **Rentabilidad bruta**, una sola por fondo: se toma una participación del fondo (la que tiene comisión en más
  fechas), a su rentabilidad diaria se le devuelve la comisión de administración de su ficha de ese mes (o del mes
  más cercano), y con eso se arma una **base 100**. La base reinicia en 100 después de un hueco de más de 5 días
  sin datos. La rentabilidad efectiva anual y la volatilidad se calculan sobre esa base 100.
- **Volatilidad**: desviación estándar de los movimientos diarios × √365.
- **Puesto y cuartil**: entre los fondos (bruta) o las participaciones (neta) de la misma categoría, a la fecha de
  corte. Q1 es el mejor 25%.

**La actualización**
- Los datos de la SFC se actualizan cada día. Las fichas técnicas, cuando hay nuevas disponibles.
- Arriba de la página siempre están las fechas de lo que estás viendo.

**La cobertura al {cutoff:%Y-%m-%d}**
- {fondos_corte} fondos con datos de la SFC, AUM de {cop_bn(aum_corte)}.
- {len(g_all)} de ellos tienen rentabilidad bruta: {pct(min(g_all['aum_fondo'].sum() / aum_corte, 1.0) if aum_corte else np.nan)} del AUM.
""")
    b.markdown(f"""
#### Qué no es

- **No son cifras oficiales** de la SFC ni de las gestoras. Son cálculos propios sobre datos públicos, y pueden
  diferir de lo que publica cada ficha técnica.
- **No es una recomendación de inversión.** Rentabilidades pasadas no aseguran rentabilidades futuras.
- **No hay promedios ni índices por categoría.** Cada cifra es de un fondo o de una participación. La categoría
  solo sirve para filtrar y para comparar fondos reales entre sí.
- **La rentabilidad bruta no existe para todos los fondos.** Solo para los que tienen una comisión de
  administración en una ficha técnica enlazada a la SFC. Casi ningún fondo de capital privado la tiene.
- **La bruta solo devuelve la comisión de administración.** No devuelve otros gastos del fondo ni comisiones de
  éxito, entrada o salida.
- **La comisión puede ser más vieja que el valor de la unidad.** Las fichas son mensuales: después de
  {fichas_hasta}, la bruta usa la comisión de la última ficha disponible de cada fondo.
- **Los saltos no se corrigen.** Un movimiento diario de 20% o más, o un reinicio de la unidad a 10.000, se muestra
  como lo reportó el fondo. Por defecto, los fondos y participaciones con saltos se ocultan (opción de la barra
  lateral); la columna «Alerta» dice qué tiene cada fondo.
- **Los últimos días están incompletos.** Después de {d_ok:%Y-%m-%d} faltan los fondos que reportan tarde.
- **La variación de AUM no son flujos.** Mezcla rentabilidad con entradas y salidas de dinero.
- **Un fondo que cambió de código SFC** tiene su historia unida solo cuando la continuidad está probada. Si no,
  sus cifras de 1, 3 y 5 años quedan en blanco hasta completar historia con el código nuevo.
""")
