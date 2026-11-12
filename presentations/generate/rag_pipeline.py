#!/usr/bin/env python3
"""RAG pipeline — presentation-quality diagram."""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.lines import Line2D

FIG_W, FIG_H = 16, 9
fig = plt.figure(figsize=(FIG_W, FIG_H), dpi=150)
ax = fig.add_axes([0, 0, 1, 1]); ax.set_xlim(0, 16); ax.set_ylim(0, 9); ax.axis("off")

C = dict(
    gray="#EEF1F6", gray_e="#C3CAD6",
    orange="#FBE4CF", orange_e="#E8974A", orange_t="#B3610E",
    purple="#E8DFF5", purple_e="#8B6FC7", purple_t="#5A3D99",
    green="#DCF2E3", green_e="#4CAA6E", green_t="#2C7A4A",
    store="#FFF6D6", store_e="#D9B84A",
    lane1="#F7F9FC", lane2="#FDF9F4",
    ink="#1F2937", sub="#5B6572",
)

def box(x, y, w, h, face, edge, num, title, sub, tcol=C["ink"], subc=C["sub"], lw=2.0, numcol=None):
    ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.12,rounding_size=0.18",
                              fc=face, ec=edge, lw=lw, zorder=3))
    if num is not None:
        ax.add_patch(plt.Circle((x + 0.42, y + h - 0.42), 0.26, fc=numcol or edge, ec="none", zorder=4))
        ax.text(x + 0.42, y + h - 0.42, str(num), color="white", fontsize=12.5,
                fontweight="bold", ha="center", va="center", zorder=5)
        tx = x + 0.85
    else:
        tx = x + w / 2
    ax.text(tx if num else x + w / 2, y + h - (0.42 if num else 0.55) + (0.02 if num else 0),
            title, color=tcol, fontsize=14.5, fontweight="bold",
            ha="left" if num else "center", va="center", zorder=5)
    if sub:
        ax.text(x + w / 2, y + (0.42 if sub else h / 2), sub, color=subc, fontsize=10.5,
                ha="center", va="center", zorder=5)

def arrow(x1, y1, x2, y2, color="#8A94A6", style="-|>", lw=2.4, dashed=False, rad=0.0):
    ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2),
                 connectionstyle=f"arc3,rad={rad}",
                 arrowstyle=style, mutation_scale=22,
                 lw=lw, color=color, zorder=2,
                 linestyle="--" if dashed else "-"))

# ── Title ─────────────────────────────────────────────────────────────
ax.text(8, 8.62, "RAG — Retrieval-Augmented Generation",
        fontsize=26, fontweight="bold", ha="center", color=C["ink"])
ax.text(8, 8.22, "find the right evidence → put it in the prompt → generate a grounded answer",
        fontsize=13, ha="center", color=C["sub"])

# ── Lane 1: indexing ─────────────────────────────────────────────────
ax.add_patch(FancyBboxPatch((0.45, 5.35), 15.1, 2.5, boxstyle="round,pad=0.05,rounding_size=0.15",
                            fc=C["lane1"], ec="#E2E7F0", lw=1.2, zorder=1))
ax.text(1.0, 7.62, "PHASE 1 — INDEXING   (runs once, offline)", fontsize=12.5,
        fontweight="bold", color="#4A5A77")

y1, h1, w1, gap = 5.85, 1.35, 2.15, 0.62
x = 0.9
steps1 = [
    (1, "Ingest", "pull from sources\nConfluence · files · SharePoint", C["gray"], C["gray_e"]),
    (2, "Extract", "strip markup → plain text", C["gray"], C["gray_e"]),
    (3, "Chunk", "split into ~300-token\nsearchable units", C["gray"], C["gray_e"]),
    (4, "EMBED", "chunk text → dense vector\n(one vector per chunk)", C["orange"], C["orange_e"]),
]
centers1 = []
for i, (n, t, s, f, e) in enumerate(steps1):
    bx = x + i * (w1 + gap)
    box(bx, y1, w1, h1, f, e, n, t, s,
        tcol=C["orange_t"] if f == C["orange"] else C["ink"])
    centers1.append(bx + w1)
    if i:
        arrow(bx - gap + 0.06, y1 + h1 / 2, bx - 0.06, y1 + h1 / 2)

# store box — under EMBED, feeding both lanes
sx = x + 3 * (w1 + gap) + w1 + 0.9
box(sx, 5.85, 3.3, 1.35, C["store"], C["store_e"], None,
    "The Index", "vector store (embeddings)\n+ BM25 / FTS index (same chunks)", lw=2.4)
arrow(centers1[-1] + 0.06, y1 + h1 / 2, sx - 0.06, y1 + h1 / 2, lw=2.6)

# ── Lane 2: answering ────────────────────────────────────────────────
ax.add_patch(FancyBboxPatch((0.45, 1.15), 15.1, 2.9, boxstyle="round,pad=0.05,rounding_size=0.15",
                            fc=C["lane2"], ec="#F0E2D2", lw=1.2, zorder=1))
ax.text(1.0, 3.8, "PHASE 2 — ANSWERING   (runs for every query)", fontsize=12.5,
        fontweight="bold", color="#8A5A24")

y2, h2, w2, gap2 = 1.55, 1.5, 2.0, 0.42
steps2 = [
    (5, "User asks", '"how do I reset\na password?"', C["gray"], C["gray_e"], C["ink"]),
    (6, "EMBED", "question → vector\n(same model)", C["orange"], C["orange_e"], C["orange_t"]),
    (7, "Retrieve", "vector + BM25 legs\nfused → top ~50", C["gray"], C["gray_e"], C["ink"]),
    (8, "RERANK", "score each candidate\n→ top ~8  · abstain?", C["purple"], C["purple_e"], C["purple_t"]),
    (9, "Augment", "chunks injected\ninto the prompt", C["gray"], C["gray_e"], C["ink"]),
    (10, "Generate", "LLM answers + cites\n(or refuses)", C["green"], C["green_e"], C["green_t"]),
]
x2 = 0.9
for i, (n, t, s, f, e, tc) in enumerate(steps2):
    bx = x2 + i * (w2 + gap2)
    box(bx, y2, w2, h2, f, e, n, t, s, tcol=tc)
    if i:
        arrow(bx - gap2 + 0.06, y2 + h2 / 2, bx - 0.06, y2 + h2 / 2)

# store → retrieve (dashed, down)
arrow(sx + 1.65, 5.83, x2 + 2 * (w2 + gap2) + w2 / 2, y2 + h2 + 0.28,
      color="#B8963A", dashed=True, lw=2.2, rad=0.0)
ax.text(sx + 1.3, 5.55, "lookup", fontsize=10, color="#B8963A", ha="left", style="italic")

# same-model connector between the two EMBED boxes
ex1 = x + 3 * (w1 + gap) + w1 / 2
ex2 = x2 + w2 + gap2 + w2 / 2
ax.plot([ex2, ex2], [y2 + h2, 4.6], color=C["orange_e"], lw=1.8, ls=":", zorder=2)
ax.plot([ex2, ex1 + 0.0], [4.6, 4.6], color=C["orange_e"], lw=1.8, ls=":", zorder=2)
ax.plot([ex1], [4.6], marker="v", color=C["orange_e"], markersize=10, zorder=2)
ax.plot([ex1, ex1], [4.6, y1 + h1], color=C["orange_e"], lw=1.8, ls=":", zorder=2)
ax.plot([ex1], [y1 + h1], marker="^", color=C["orange_e"], markersize=10, zorder=2)
ax.text((ex1 + ex2) / 2 + 0.15, 4.72, "SAME embedding model — used twice:\nall chunks offline · one query online",
        fontsize=10.5, color=C["orange_t"], ha="center", style="italic")

# ── Legend ────────────────────────────────────────────────────────────
leg = [
    Line2D([0], [0], marker="s", color="none", mfc=C["orange"], mec=C["orange_e"], ms=15, label="embedding model — appears twice (index + query)"),
    Line2D([0], [0], marker="s", color="none", mfc=C["purple"], mec=C["purple_e"], ms=15, label="reranker — precision stage"),
    Line2D([0], [0], marker="s", color="none", mfc=C["green"], mec=C["green_e"], ms=15, label="LLM — the 'G' in RAG"),
    Line2D([0], [0], marker="s", color="none", mfc=C["gray"], mec=C["gray_e"], ms=15, label="plain code — no model"),
    Line2D([0], [0], marker="s", color="none", mfc=C["store"], mec=C["store_e"], ms=15, label="the index — what both phases share"),
]
ax.legend(handles=leg, loc="lower center", ncol=5, frameon=False,
          fontsize=10.5, bbox_to_anchor=(0.5, -0.015), handlelength=1.4, columnspacing=1.6)

from pathlib import Path
fig.savefig(str(Path(__file__).resolve().parent.parent / "RAG_pipeline.png"),
            facecolor="white", bbox_inches="tight", pad_inches=0.25)
print("wrote", Path(__file__).resolve().parent.parent / "RAG_pipeline.png")
