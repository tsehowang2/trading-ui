from __future__ import annotations

import argparse
import contextlib
import csv
import math
import os
import random
import statistics as stats
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

import numpy as np
import pandas as pd
import time

import main as strat


# ── progress helpers ──────────────────────────────────────────────────────────

def _fmt_elapsed(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def _pbar(done: int, total: int, width: int = 20) -> str:
    filled = int(width * done / total) if total else 0
    return "[" + "#" * filled + "-" * (width - filled) + "]"


def _running_stats(rows: list) -> str:
    """One-liner median ret + sharpe from accumulated TrialResult rows."""
    if not rows:
        return ""
    rets    = [r.avg_ret_traded  for r in rows]
    sharpes = [r.avg_sharpe_traded for r in rows]
    beat    = [r.beat_dca_rate_traded for r in rows]
    med_r   = float(np.median(rets))
    med_s   = float(np.median(sharpes))
    med_b   = float(np.median(beat))
    return (f"med_ret={med_r:+.1%}  med_sharpe={med_s:.2f}  "
            f"beat_dca={med_b:.0%}")


@dataclass
class TrialResult:
    trial: int
    start: str
    end: str
    n_requested: int
    n_attempted: int
    n_traded: int
    coverage: float
    avg_ret_traded: float
    med_ret_traded: float
    avg_sharpe_traded: float
    med_mdd_traded: float
    avg_tpm_traded: float
    pos_rate_traded: float
    beat_dca_rate_traded: float
    avg_ret_all: float


def _safe_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return default


def _quantiles(values: list[float], probs=(0.1, 0.5, 0.9)) -> dict[str, float]:
    if not values:
        return {f"q{int(p*100)}": 0.0 for p in probs}
    arr = np.array(values, dtype=float)
    return {f"q{int(p*100)}": float(np.quantile(arr, p)) for p in probs}


@contextlib.contextmanager
def patched_globals(**overrides):
    originals = {}
    touched = []
    for key, val in overrides.items():
        if hasattr(strat, key):
            originals[key] = getattr(strat, key)
            setattr(strat, key, val)
            touched.append(key)
    try:
        yield
    finally:
        for key in touched:
            setattr(strat, key, originals[key])


def get_universe(custom: list[str] | None = None) -> list[str]:
    if custom:
        vals = [x.strip().upper() for x in custom if x.strip()]
    elif hasattr(strat, "SP500_POOL") and strat.SP500_POOL:
        vals = [str(x).upper() for x in strat.SP500_POOL]
    elif hasattr(strat, "DEFAULT_TICKERS") and strat.DEFAULT_TICKERS:
        vals = [str(x).upper() for x in strat.DEFAULT_TICKERS]
    else:
        vals = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "AMD", "MU", "LLY"]
    return sorted(set(vals))


def pick_window(rng: random.Random, from_year: int, to_year: int, window_years: int) -> tuple[str, str]:
    if window_years < 1:
        raise ValueError("window_years must be >= 1")
    max_start = to_year - window_years + 1
    if max_start < from_year:
        raise ValueError("window_years is too large for the chosen year range")
    sy = rng.randint(from_year, max_start)
    ey = sy + window_years - 1
    start = f"{sy}-01-01"
    if ey >= datetime.now().year:
        end = datetime.now().strftime("%Y-%m-%d")
    else:
        end = f"{ey}-12-31"
    return start, end


def run_one_trial(
    trial_num: int,
    tickers: list[str],
    start: str,
    end: str,
    verbose: bool = False,
    bad_tickers: set[str] | None = None,
    reserve: list[str] | None = None,
) -> TrialResult | None:
    if bad_tickers is None:
        bad_tickers = set()
    if reserve is None:
        reserve = []
    reserve = list(reserve)  # local copy so we can pop
    attempted = 0
    traded = 0
    rets_traded: list[float] = []
    sharpes_traded: list[float] = []
    mdds_traded: list[float] = []
    tpms_traded: list[float] = []
    beat_dca_flags: list[int] = []
    all_rets: list[float] = []

    dca_ret = None
    if hasattr(strat, "_bmark_dca_voo"):
        try:
            dca_eq = strat._bmark_dca_voo(start, end)
            if dca_eq is not None and len(dca_eq):
                dca_ret = float(dca_eq.iloc[-1] / dca_eq.iloc[0] - 1)
        except Exception:
            dca_ret = None

    for sym in tickers:
        attempted += 1
        try:
            r = strat.run_backtest(sym, start, end, verbose=False)
        except Exception:
            continue
        if not r:
            # No data — blacklist and try a substitute from reserve
            bad_tickers.add(sym)
            if reserve:
                sub = reserve.pop(0)
                try:
                    r = strat.run_backtest(sub, start, end, verbose=False)
                except Exception:
                    r = {}
                if not r:
                    bad_tickers.add(sub)
                    continue
            else:
                continue
        ret = _safe_float(r.get("total_ret"), 0.0)
        all_rets.append(ret)
        if _safe_float(r.get("n_trades"), 0.0) > 0:
            traded += 1
            rets_traded.append(ret)
            sharpes_traded.append(_safe_float(r.get("sharpe"), 0.0))
            mdds_traded.append(_safe_float(r.get("mdd"), 0.0))
            tpms_traded.append(_safe_float(r.get("tpm"), 0.0))
            if dca_ret is not None:
                beat_dca_flags.append(1 if ret > dca_ret else 0)
        elif dca_ret is not None:
            beat_dca_flags.append(0)

    if attempted == 0:
        return None

    cov = traded / attempted if attempted else 0.0
    avg_ret_traded = float(np.mean(rets_traded)) if rets_traded else 0.0
    med_ret_traded = float(np.median(rets_traded)) if rets_traded else 0.0
    avg_sharpe_traded = float(np.mean(sharpes_traded)) if sharpes_traded else 0.0
    med_mdd_traded = float(np.median(mdds_traded)) if mdds_traded else 0.0
    avg_tpm_traded = float(np.mean(tpms_traded)) if tpms_traded else 0.0
    pos_rate_traded = float(np.mean([x > 0 for x in rets_traded])) if rets_traded else 0.0
    beat_dca_rate_traded = float(np.mean(beat_dca_flags)) if beat_dca_flags else 0.0
    avg_ret_all = float(np.mean(all_rets)) if all_rets else 0.0

    if verbose:
        print(
            f" Trial {trial_num:>3}: {start} -> {end} | "
            f"traded={traded}/{attempted} ({cov:.0%}) | "
            f"avg_ret_traded={avg_ret_traded:+.1%} | "
            f"avg_sharpe={avg_sharpe_traded:.2f} | "
            f"beat_dca={beat_dca_rate_traded:.0%}"
        )

    return TrialResult(
        trial=trial_num,
        start=start,
        end=end,
        n_requested=len(tickers),
        n_attempted=attempted,
        n_traded=traded,
        coverage=cov,
        avg_ret_traded=avg_ret_traded,
        med_ret_traded=med_ret_traded,
        avg_sharpe_traded=avg_sharpe_traded,
        med_mdd_traded=med_mdd_traded,
        avg_tpm_traded=avg_tpm_traded,
        pos_rate_traded=pos_rate_traded,
        beat_dca_rate_traded=beat_dca_rate_traded,
        avg_ret_all=avg_ret_all,
    )


def summarise_trials(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    summary = {
        "n_trials": len(df),
        "mean_coverage": df["coverage"].mean(),
        "median_coverage": df["coverage"].median(),
        "mean_avg_ret_traded": df["avg_ret_traded"].mean(),
        "median_avg_ret_traded": df["avg_ret_traded"].median(),
        "mean_avg_ret_all": df["avg_ret_all"].mean(),
        "median_avg_ret_all": df["avg_ret_all"].median(),
        "mean_avg_sharpe_traded": df["avg_sharpe_traded"].mean(),
        "median_avg_sharpe_traded": df["avg_sharpe_traded"].median(),
        "mean_med_mdd_traded": df["med_mdd_traded"].mean(),
        "median_med_mdd_traded": df["med_mdd_traded"].median(),
        "mean_pos_rate_traded": df["pos_rate_traded"].mean(),
        "median_pos_rate_traded": df["pos_rate_traded"].median(),
        "mean_beat_dca_rate_traded": df["beat_dca_rate_traded"].mean(),
        "median_beat_dca_rate_traded": df["beat_dca_rate_traded"].median(),
        "pct_trials_pos_avg_ret_traded": float(np.mean(df["avg_ret_traded"] > 0)),
        "pct_trials_sharpe_gt_0_5": float(np.mean(df["avg_sharpe_traded"] > 0.5)),
        "pct_trials_beat_dca_majority": float(np.mean(df["beat_dca_rate_traded"] > 0.5)),
    }
    summary.update(_quantiles(df["avg_ret_traded"].tolist()))
    summary.update({f"sharpe_{k}": v for k, v in _quantiles(df["avg_sharpe_traded"].tolist()).items()})
    summary.update({f"mdd_{k}": v for k, v in _quantiles(df["med_mdd_traded"].tolist()).items()})
    return pd.DataFrame([summary])


def run_random_robustness(
    universe: list[str],
    n_trials: int,
    n_stocks: int,
    from_year: int,
    to_year: int,
    window_years: int,
    seed: int,
    overrides: dict[str, float | int] | None = None,
    verbose_every: int = 10,
    label: str = "robustness",
) -> pd.DataFrame:
    rng = random.Random(seed)
    rows: list[TrialResult] = []
    n_stocks = min(max(1, n_stocks), len(universe))
    overrides = overrides or {}
    bad_tickers: set[str] = set()   # grows across trials; delisted tickers skipped

    label_str = f"[{label}]" if label else ""
    print(f"\n{label_str} Running {n_trials} trials  "
          f"({n_stocks} stocks/{window_years}yr window  "
          f"{from_year}-{to_year}  seed={seed})")
    if overrides:
        print(f"  Overrides: {overrides}")
    print(f"  {'─'*70}")

    t_start = time.monotonic()

    with patched_globals(**overrides):
        for trial in range(1, n_trials + 1):
            t_trial = time.monotonic()
            clean_pool = [t for t in universe if t not in bad_tickers]
            if len(clean_pool) < n_stocks:
                clean_pool = universe   # fallback: don't filter if pool too small
            sample = rng.sample(clean_pool, min(n_stocks, len(clean_pool)))
            reserve = [t for t in clean_pool if t not in sample]
            rng.shuffle(reserve)        # random order for substitution
            start, end = pick_window(rng, from_year, to_year, window_years)
            row = run_one_trial(
                trial_num=trial,
                tickers=sample,
                start=start,
                end=end,
                verbose=False,           # suppress per-ticker noise; use progress bar
                bad_tickers=bad_tickers,
                reserve=reserve,
            )
            if row is not None:
                rows.append(row)

            # Progress line every verbose_every trials and on first/last
            show = (trial == 1 or trial % max(1, verbose_every) == 0
                    or trial == n_trials)
            if show:
                elapsed   = time.monotonic() - t_start
                per_trial = elapsed / trial
                eta       = per_trial * (n_trials - trial)
                bar       = _pbar(trial, n_trials)
                stats_str = _running_stats(rows)
                pct       = trial / n_trials * 100
                print(f"  {bar} {trial:>4}/{n_trials} ({pct:>5.1f}%)  "
                      f"elapsed={_fmt_elapsed(elapsed)}  eta={_fmt_elapsed(eta)}  "
                      f"{stats_str}")

    elapsed_total = time.monotonic() - t_start
    print(f"  {'─'*70}")
    print(f"  Done in {_fmt_elapsed(elapsed_total)}  "
          f"({elapsed_total/n_trials:.1f}s/trial avg)")
    if bad_tickers:
        print(f"  Blacklisted tickers (no data): {', '.join(sorted(bad_tickers))}")

    return pd.DataFrame([r.__dict__ for r in rows])


def run_sensitivity(
    universe: list[str],
    n_trials: int,
    n_stocks: int,
    from_year: int,
    to_year: int,
    window_years: int,
    seed: int,
) -> pd.DataFrame:
    grid = []
    candidates = {
        "STOP_ATR_MULT": [2.5, 3.0, 3.5],
        "RS_GATE_THRESHOLD": [-0.10, -0.05, 0.00],
        "MIN_TRAIL_STOP": [0.12, 0.15, 0.18],
    }

    total_runs = sum(len(v) for v in candidates.values()
                     if hasattr(strat, next(iter(candidates))))
    total_runs = sum(len(v) for k, v in candidates.items() if hasattr(strat, k))
    run_num = 0
    t_sens_start = time.monotonic()

    print(f"\n{'='*78}")
    print(f"SENSITIVITY ANALYSIS  ({total_runs} parameter sweeps  ×  {n_trials} trials each)")
    print(f"{'='*78}")

    for param, values in candidates.items():
        if not hasattr(strat, param):
            continue
        base = getattr(strat, param)
        for val in values:
            run_num += 1
            print(f"\n  [{run_num}/{total_runs}] {param} = {val}  (base={base})")
            df = run_random_robustness(
                universe=universe,
                n_trials=n_trials,
                n_stocks=n_stocks,
                from_year=from_year,
                to_year=to_year,
                window_years=window_years,
                seed=seed + int(abs(hash((param, val))) % 10000),
                overrides={param: val},
                verbose_every=max(1, n_trials // 4),
                label=f"{param}={val}",
            )
            if df.empty:
                continue
            result = {
                "param": param,
                "base": base,
                "test_value": val,
                "n_trials": len(df),
                "median_avg_ret_traded": float(df["avg_ret_traded"].median()),
                "mean_avg_ret_traded": float(df["avg_ret_traded"].mean()),
                "median_avg_sharpe_traded": float(df["avg_sharpe_traded"].median()),
                "median_med_mdd_traded": float(df["med_mdd_traded"].median()),
                "pct_trials_pos_avg_ret": float(np.mean(df["avg_ret_traded"] > 0)),
                "pct_trials_sharpe_gt_0_5": float(np.mean(df["avg_sharpe_traded"] > 0.5)),
                "pct_trials_beat_dca_majority": float(np.mean(df["beat_dca_rate_traded"] > 0.5)),
            }
            grid.append(result)
            print(f"     → med_ret={result['median_avg_ret_traded']:+.1%}  "
                  f"med_sharpe={result['median_avg_sharpe_traded']:.2f}  "
                  f"beat_dca={result['pct_trials_beat_dca_majority']:.0%}")

    elapsed_sens = time.monotonic() - t_sens_start
    print(f"\nSensitivity done in {_fmt_elapsed(elapsed_sens)}")
    return pd.DataFrame(grid)


def print_report(summary_df: pd.DataFrame, sens_df: pd.DataFrame) -> None:
    if summary_df.empty:
        print("No robustness results produced.")
        return
    s = summary_df.iloc[0]
    print("\n" + "=" * 78)
    print("ROBUSTNESS SUMMARY")
    print("=" * 78)
    print(f"Trials:                     {int(s['n_trials'])}")
    print(f"Coverage (median):         {s['median_coverage']:.0%}")
    print(f"Avg ret traded (median):   {s['median_avg_ret_traded']:+.1%}")
    print(f"Avg ret traded (10/90):    {s['q10']:+.1%} / {s['q90']:+.1%}")
    print(f"Avg ret all (median):      {s['median_avg_ret_all']:+.1%}")
    print(f"Avg Sharpe (median):       {s['median_avg_sharpe_traded']:.2f}")
    print(f"Avg Sharpe (10/90):        {s['sharpe_q10']:.2f} / {s['sharpe_q90']:.2f}")
    print(f"Median MDD across trials:  {s['median_med_mdd_traded']:+.1%}")
    print(f"Positive-ret trials:       {s['pct_trials_pos_avg_ret_traded']:.0%}")
    print(f"Sharpe > 0.5 trials:       {s['pct_trials_sharpe_gt_0_5']:.0%}")
    print(f"Beat DCA majority trials:  {s['pct_trials_beat_dca_majority']:.0%}")
    print("-" * 78)

    pass_flags = {
        "median_avg_ret_traded > 0": s["median_avg_ret_traded"] > 0,
        "median_avg_sharpe_traded > 0.5": s["median_avg_sharpe_traded"] > 0.5,
        "pct_trials_pos_avg_ret_traded >= 60%": s["pct_trials_pos_avg_ret_traded"] >= 0.60,
        "pct_trials_beat_dca_majority >= 55%": s["pct_trials_beat_dca_majority"] >= 0.55,
    }
    for label, ok in pass_flags.items():
        print(f"{'PASS' if ok else 'FAIL'}  {label}")

    if not sens_df.empty:
        print("\nTop sensitivity rows (sorted by median Sharpe then median return):")
        top = sens_df.sort_values(
            ["median_avg_sharpe_traded", "median_avg_ret_traded"],
            ascending=[False, False],
        ).head(12)
        with pd.option_context("display.max_rows", 20, "display.width", 160):
            print(top.to_string(index=False))


def load_universe_from_file(path: str) -> list[str]:
    tickers: list[str] = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for row in csv.reader(f):
            if not row:
                continue
            val = row[0].strip().upper()
            if val and not val.startswith("#"):
                tickers.append(val)
    return sorted(set(tickers))


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Robustness test harness for the strategy defined in main.py",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument("--from-year", type=int, default=2020)
    ap.add_argument("--to-year", type=int, default=datetime.now().year)
    ap.add_argument("--window-years", type=int, default=1, help="Contiguous years per sampled test window")
    ap.add_argument("--trials", type=int, default=200)
    ap.add_argument("--stocks", type=int, default=20, help="Tickers sampled per trial")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--universe-file", type=str, default=None, help="CSV/TXT file, first column = ticker")
    ap.add_argument("--tickers", nargs="*", default=None, help="Optional explicit tickers")
    ap.add_argument("--sensitivity-trials", type=int, default=60)
    ap.add_argument("--skip-sensitivity", action="store_true")
    ap.add_argument("--outdir", type=str, default="results/robustness")
    args = ap.parse_args()

    if args.universe_file:
        universe = load_universe_from_file(args.universe_file)
    else:
        universe = get_universe(args.tickers)

    if not universe:
        raise SystemExit("Universe is empty.")

    os.makedirs(args.outdir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    print("\n" + "=" * 78)
    print("ROBUSTNESS TEST HARNESS")
    print("=" * 78)
    print(f"  Universe size : {len(universe)} tickers")
    print(f"  Sample        : {', '.join(universe[:12])}{' ...' if len(universe) > 12 else ''}")
    print(f"  Main trials   : {args.trials}  ({args.stocks} stocks each, "
          f"{args.window_years}yr window, {args.from_year}-{args.to_year})")
    print(f"  Sensitivity   : {'skipped' if args.skip_sensitivity else f'{args.sensitivity_trials} trials per sweep'}")
    print(f"  Output dir    : {os.path.abspath(args.outdir)}")
    print(f"  Seed          : {args.seed}")
    print("=" * 78)

    trials_df = run_random_robustness(
        universe=universe,
        n_trials=args.trials,
        n_stocks=args.stocks,
        from_year=args.from_year,
        to_year=args.to_year,
        window_years=args.window_years,
        seed=args.seed,
        overrides={},
        verbose_every=max(1, args.trials // 10),
        label="main",
    )
    summary_df = summarise_trials(trials_df)

    if args.skip_sensitivity:
        sens_df = pd.DataFrame()
    else:
        sens_df = run_sensitivity(
            universe=universe,
            n_trials=args.sensitivity_trials,
            n_stocks=args.stocks,
            from_year=args.from_year,
            to_year=args.to_year,
            window_years=args.window_years,
            seed=args.seed + 1000,
        )

    trials_path = os.path.join(args.outdir, f"trial_results_{stamp}.csv")
    summary_path = os.path.join(args.outdir, f"summary_{stamp}.csv")
    sens_path = os.path.join(args.outdir, f"sensitivity_{stamp}.csv")

    trials_df.to_csv(trials_path, index=False)
    summary_df.to_csv(summary_path, index=False)
    if not sens_df.empty:
        sens_df.to_csv(sens_path, index=False)

    print_report(summary_df, sens_df)
    print("\nSaved:")
    print(" ", trials_path)
    print(" ", summary_path)
    if not sens_df.empty:
        print(" ", sens_path)


if __name__ == "__main__":
    main()