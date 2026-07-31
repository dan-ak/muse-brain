"""Generate the summary and overview plot for a published session.

    python analysis/summarize.py data/latest

Writes SUMMARY.md and overview.png into the session directory. The plot is what
someone sees first on GitHub, so it has to answer "is this good data?" without
anyone running anything.
"""

from __future__ import annotations

import sys
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
from scipy import signal as sig  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from analysis.session import EEG_CHANNELS, FS, Session  # noqa: E402

BANDS = {"delta": (1, 4), "theta": (4, 8), "alpha": (8, 13),
         "beta": (13, 30), "gamma": (30, 45)}


def overview_plot(session: Session, out: Path) -> None:
    clean = session.clean_eeg()
    raw = session.eeg()
    t = clean["t"].to_numpy()

    fig, axes = plt.subplots(3, 1, figsize=(11, 9), height_ratios=[2, 1.4, 1.2])
    fig.patch.set_facecolor("#0d1117")
    for ax in axes:
        ax.set_facecolor("#0d1117")
        ax.tick_params(colors="#8b949e")
        for spine in ax.spines.values():
            spine.set_color("#30363d")
        ax.yaxis.label.set_color("#c9d1d9")
        ax.xaxis.label.set_color("#c9d1d9")
        ax.title.set_color("#c9d1d9")

    # 1. Filtered traces, offset so all four are legible at once.
    ax = axes[0]
    colors = ["#58a6ff", "#3fb950", "#d29922", "#f85149"]
    spacing = 120
    for i, c in enumerate(EEG_CHANNELS):
        ax.plot(t, clean[c].to_numpy() + i * spacing, lw=0.35,
                color=colors[i], label=c)
    ax.set_yticks([i * spacing for i in range(4)])
    ax.set_yticklabels(EEG_CHANNELS)
    ax.set_xlabel("seconds")
    ax.set_title("EEG, mains-notched and band-passed 1-45 Hz", loc="left")
    ax.set_xlim(t[0], t[-1])

    # Shade cued trials if the session has them.
    for trial in session.trials():
        ax.axvspan(trial.start, trial.end,
                   color="#3fb950" if trial.task == "focus" else "#58a6ff",
                   alpha=0.10)

    # 2. Spectrum per channel, raw, so mains contamination is visible.
    ax = axes[1]
    for i, c in enumerate(EEG_CHANNELS):
        f, p = sig.welch(sig.detrend(raw[c].to_numpy()), FS, nperseg=1024)
        ax.semilogy(f, p, lw=0.9, color=colors[i], label=c)
    ax.axvline(50, color="#f85149", ls=":", lw=1)
    ax.axvline(60, color="#f85149", ls=":", lw=1)
    ax.text(51, ax.get_ylim()[1] * 0.3, "mains", color="#f85149", fontsize=8)
    ax.set_xlim(0, 70)
    ax.set_xlabel("Hz")
    ax.set_ylabel("power")
    ax.set_title("Raw spectrum — peaks at 50/60 Hz mean a poorly seated electrode",
                 loc="left")
    ax.legend(facecolor="#161b22", edgecolor="#30363d", labelcolor="#c9d1d9",
              fontsize=8, ncol=4)

    # 3. The live focus score.
    ax = axes[2]
    tel = session.telemetry()
    ax.plot(tel["t"], tel["focus_score"], lw=0.7, color="#bc8cff")
    ax.set_xlim(t[0], t[-1])
    ax.set_xlabel("seconds")
    ax.set_ylabel("focus score")
    ax.set_title("Live focus score, log10(beta) - log10(theta)", loc="left")

    fig.tight_layout()
    fig.savefig(out, dpi=110, facecolor=fig.get_facecolor())
    plt.close(fig)


def band_table(session: Session) -> str:
    clean = session.clean_eeg()
    lines = ["| channel | mains ratio | contact | " +
             " | ".join(BANDS) + " |",
             "|---|---|---|" + "---|" * len(BANDS)]
    for c in EEG_CHANNELS:
        x = clean[c].to_numpy()
        f, p = sig.welch(x, FS, nperseg=1024)
        total = p[(f >= 1) & (f <= 45)].sum()
        cells = []
        for lo, hi in BANDS.values():
            cells.append(f"{100 * p[(f >= lo) & (f < hi)].sum() / total:.0f}%")
        ratio = session.mains_ratio(c)
        verdict = "clean" if ratio < 100 else "**hum**"
        lines.append(f"| {c} | {ratio:,.0f}x | {verdict} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


def main(directory: str) -> None:
    session = Session(directory)
    out_dir = Path(directory)
    overview_plot(session, out_dir / "overview.png")

    trials = session.trials()
    gaps = session.gaps()
    rows = len(session.eeg())
    expected = int(FS * session.duration)

    parts = [
        f"# Session: {session.label}",
        "",
        f"- **Duration** {session.duration:.0f} s",
        f"- **EEG** {rows:,} samples ({100 * rows / max(1, expected):.1f}% of {FS} Hz)",
        f"- **Recording errors** {session.meta.get('errors', 0)}",
        f"- **Dropouts reported** {len(gaps)}",
        f"- **Cued trials** {len(trials)}" + (" (uncued session)" if not trials else ""),
        "",
        "![overview](overview.png)",
        "",
        "## Signal quality",
        "",
        "Mains ratio is power at 50/60 Hz over the broadband floor. Above ~100x the",
        "electrode is picking up more mains hum than brain signal — a contact problem,",
        "fixed by dampening the pad and clearing hair, not by tightening the band.",
        "Band percentages are after notching, so they describe the brain signal.",
        "",
        band_table(session),
        "",
    ]

    if trials:
        parts += ["## Conditions", "",
                  "| condition | trials | total seconds |", "|---|---|---|"]
        seen: dict[tuple[str, str], list[float]] = {}
        for tr in trials:
            seen.setdefault((tr.eyes, tr.task), []).append(tr.duration)
        for (eyes, task), durs in sorted(seen.items()):
            parts.append(f"| eyes {eyes}, {task} | {len(durs)} | {sum(durs):.0f} |")
        parts.append("")

    # Relative to the repo root, since this is read by people who cloned it.
    try:
        shown = out_dir.resolve().relative_to(Path(__file__).resolve().parent.parent)
    except ValueError:
        shown = out_dir

    parts += [
        "## Loading it",
        "",
        "```python",
        "from analysis.session import Session",
        f"s = Session({str(shown)!r})",
        "eeg = s.clean_eeg()        # mains removed, 1-45 Hz",
        "for trial in s.trials():   # condition-labelled slices",
        "    print(trial.eyes, trial.task, trial.eeg().shape)",
        "```",
        "",
    ]

    (out_dir / "SUMMARY.md").write_text("\n".join(parts))
    print(f"wrote {out_dir/'SUMMARY.md'} and {out_dir/'overview.png'}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "data/latest")
