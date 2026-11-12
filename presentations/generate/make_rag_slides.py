#!/usr/bin/env python3
"""RAG explained — beginner slide deck (.pptx)."""
from pptx import Presentation
from pptx.util import Inches, Pt, Emu
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN, MSO_ANCHOR
from pptx.enum.shapes import MSO_SHAPE
from pathlib import Path

INK = RGBColor(0x1F, 0x29, 0x37)
SUB = RGBColor(0x5B, 0x65, 0x72)
ACCENT = RGBColor(0x1F, 0x4E, 0x79)
ORANGE = RGBColor(0xB3, 0x61, 0x0E)
ORANGE_BG = RGBColor(0xFB, 0xE4, 0xCF)
PURPLE = RGBColor(0x5A, 0x3D, 0x99)
PURPLE_BG = RGBColor(0xE8, 0xDF, 0xF5)
GREEN = RGBColor(0x2C, 0x7A, 0x4A)
GREEN_BG = RGBColor(0xDC, 0xF2, 0xE3)
GRAY_BG = RGBColor(0xEE, 0xF1, 0xF6)
YELLOW_BG = RGBColor(0xFF, 0xF6, 0xD6)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

prs = Presentation()
prs.slide_width = Inches(13.333)
prs.slide_height = Inches(7.5)
BLANK = prs.slide_layouts[6]
SW, SH = prs.slide_width, prs.slide_height


def slide():
    return prs.slides.add_slide(BLANK)


def textbox(s, l, t, w, h):
    tb = s.shapes.add_textbox(l, t, w, h)
    tb.text_frame.word_wrap = True
    return tb


def para(tf, text, size=18, bold=False, color=INK, align=PP_ALIGN.LEFT,
         space_after=6, bullet=None, first=False):
    p = tf.paragraphs[0] if first else tf.add_paragraph()
    p.alignment = align
    p.space_after = Pt(space_after)
    if bullet:
        text = bullet + " " + text
    r = p.add_run(); r.text = text
    f = r.font; f.size = Pt(size); f.bold = bold; f.color.rgb = color
    f.name = "Calibri"
    return p


def header(s, title, subtitle=None, color=ACCENT):
    tb = textbox(s, Inches(0.5), Inches(0.28), SW - Inches(1), Inches(0.9))
    para(tb.text_frame, title, size=30, bold=True, color=color, first=True)
    if subtitle:
        para(tb.text_frame, subtitle, size=15, color=SUB)
    bar = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(0.5), Inches(1.18), Inches(1.4), Pt(4))
    bar.fill.solid(); bar.fill.fore_color.rgb = color; bar.line.fill.background()


def card(s, l, t, w, h, title, body, bg, tcol, tsize=17, bsize=13.5):
    sh = s.shapes.add_shape(MSO_SHAPE.ROUNDED_RECTANGLE, l, t, w, h)
    sh.adjustments[0] = 0.08
    sh.fill.solid(); sh.fill.fore_color.rgb = bg; sh.line.fill.background()
    tf = sh.text_frame; tf.word_wrap = True
    tf.margin_left = Inches(0.14); tf.margin_right = Inches(0.14)
    tf.margin_top = Inches(0.1); tf.margin_bottom = Inches(0.08)
    tf.vertical_anchor = MSO_ANCHOR.TOP
    para(tf, title, size=tsize, bold=True, color=tcol, first=True)
    for line in body:
        para(tf, line, size=bsize, color=INK, space_after=3)


def arrow_right(s, l, t, w=Inches(0.42), h=Inches(0.34)):
    sh = s.shapes.add_shape(MSO_SHAPE.RIGHT_ARROW, l, t, w, h)
    sh.fill.solid(); sh.fill.fore_color.rgb = RGBColor(0x8A, 0x94, 0xA6)
    sh.line.fill.background()


# ── Slide 1: title ───────────────────────────────────────────────────
s = slide()
bg = s.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SW, SH)
bg.fill.solid(); bg.fill.fore_color.rgb = ACCENT; bg.line.fill.background()
tb = textbox(s, Inches(1), Inches(2.3), SW - Inches(2), Inches(3))
para(tb.text_frame, "RAG, Explained Simply", size=54, bold=True, color=WHITE, first=True)
para(tb.text_frame, "How retrieval-augmented generation finds the right evidence", size=22, color=RGBColor(0xC9, 0xD6, 0xE8))
para(tb.text_frame, "— and why chunks, embeddings and rerankers each have exactly one job", size=22, color=RGBColor(0xC9, 0xD6, 0xE8))

# ── Slide 2: the pipeline image ──────────────────────────────────────
s = slide()
header(s, "The whole picture in one slide", "Two phases — build the index once, then use it for every question")
from pathlib import Path
HERE = Path(__file__).resolve().parent.parent
img = str(HERE / "RAG_pipeline.png")
from PIL import Image  # noqa
iw, ih = Image.open(img).size
disp_w = SW - Inches(1.6)
disp_h = Emu(int(disp_w * ih / iw))
s.shapes.add_picture(img, int((SW - disp_w) / 2), Inches(1.5), width=disp_w, height=disp_h)

# ── Slide 3: the book analogy ────────────────────────────────────────
s = slide()
header(s, "The core idea: searching a book for one fact", "Every RAG concept maps to a person working with pieces of a book")
card(s, Inches(0.5), Inches(1.55), Inches(5.9), Inches(4.7), "📖  The setup",
     ["A 200-page manual holds the answer — but it's too big to hand over whole.",
      "",
      "So we CUT the book into pieces first.",
      "That cutting is called chunking.",
      "",
      "Then two people work with those pieces…"],
     GRAY_BG, INK, tsize=19, bsize=16)
card(s, Inches(6.7), Inches(1.55), Inches(6.1), Inches(1.55), "Person 1 — the Indexer  (embedding model)",
     ["Reads each piece once, writes a summary card.",
      "Question arrives → matches it to all cards → ~50 candidates."],
     ORANGE_BG, ORANGE, tsize=15.5, bsize=13)
card(s, Inches(6.7), Inches(3.28), Inches(6.1), Inches(1.7), "Person 2 — the Editor  (the reranker)",
     ["Reads the question + EACH candidate pair carefully —",
      "picks the few that TRULY answer → best ~8 on the desk.",
      "Can also say \"none of these are good enough.\""],
     PURPLE_BG, PURPLE, tsize=15.5, bsize=13)
card(s, Inches(6.7), Inches(5.16), Inches(6.1), Inches(1.55), "Person 3 — the Answerer  (the LLM)",
     ["Reads only those final ~8 pieces,",
      "writes the grounded answer + citations — or refuses."],
     GREEN_BG, GREEN, tsize=15.5, bsize=13)

# ── Slide 4: three models, three jobs ────────────────────────────────
s = slide()
header(s, "Three models, three jobs", "Each stage optimizes a different thing — that's why they exist separately")
card(s, Inches(0.5), Inches(1.6), Inches(4.0), Inches(4.9), "EMBEDDING MODEL",
     ["Job: recall — make sure the answer is SOMEWHERE in the candidate set.",
      "",
      "Used twice:",
      "• once for every chunk (index time)",
      "• once for the question (query time)",
      "",
      "Fast: ~5ms per piece."],
     ORANGE_BG, ORANGE, tsize=16, bsize=14)
card(s, Inches(4.66), Inches(1.6), Inches(4.0), Inches(4.9), "RERANKER",
     ["Job: precision — order the candidates honestly; pick the true top few.",
      "",
      "Sees question + candidate TOGETHER → fine-grained relevance.",
      "",
      "Slow per pair → only ever sees ~50 candidates, never the whole index."],
     PURPLE_BG, PURPLE, tsize=16, bsize=14)
card(s, Inches(8.82), Inches(1.6), Inches(4.0), Inches(4.9), "LLM",
     ["Job: generation — read the top chunks and write the grounded answer.",
      "",
      "Most expensive model → it sees the LEAST text.",
      "",
      "The whole pipeline exists to protect its desk."],
     GREEN_BG, GREEN, tsize=16, bsize=14)

# ── Slide 5: why chunk at all ────────────────────────────────────────
s = slide()
header(s, "Why cut the book into pieces?", "Three independent reasons all point the same direction")
card(s, Inches(0.5), Inches(1.6), Inches(4.0), Inches(4.7), "1 · The indexer can't read big pieces",
     ["The embedding model reads ~500 tokens max.",
      "Longer piece → it silently stops reading.",
      "The tail is invisible to search. Forever.",
      "No error. No warning."],
     ORANGE_BG, ORANGE, tsize=15.5, bsize=14)
card(s, Inches(4.66), Inches(1.6), Inches(4.0), Inches(4.7), "2 · The answerer's desk is small",
     ["The LLM sees a limited amount at once —",
      "instructions + history + question + chunks + its answer.",
      "8 paragraphs fit. 8 chapters don't."],
     GREEN_BG, GREEN, tsize=15.5, bsize=14)
card(s, Inches(8.82), Inches(1.6), Inches(4.0), Inches(4.7), "3 · Big pieces make fuzzy summaries",
     ["A 3000-word piece covers many topics →",
      "its summary is a blurry average of all of them.",
      "Small piece = one idea = sharp match."],
     GRAY_BG, INK, tsize=15.5, bsize=14)

# ── Slide 6: the sizing rule ─────────────────────────────────────────
s = slide()
header(s, "So how big should a chunk be?", "The model sets the ceiling — the content picks the size under it")
# too small / sweet spot / too big
card(s, Inches(0.5), Inches(1.7), Inches(3.9), Inches(3.9), "TOO SMALL  (~50 tokens)",
     ["Fragments: \"…you must then reset it within 24\"",
      "",
      "Reset WHAT? No context.",
      "Answers get split across pieces — neither half matches."],
     RGBColor(0xFD, 0xE8, 0xE8), RGBColor(0x9C, 0x00, 0x06), tsize=15, bsize=13.5)
card(s, Inches(4.7), Inches(1.55), Inches(3.9), Inches(4.2), "SWEET SPOT  (~300 tokens)",
     ["One complete idea:",
      "\"To reset a password: Settings → Security → Reset. Link expires in 24h.\"",
      "",
      "Sharp summary → strong match → complete, usable piece.",
      "Plus ~15% overlap so boundary answers survive."],
     GREEN_BG, GREEN, tsize=15.5, bsize=13.5)
card(s, Inches(8.9), Inches(1.7), Inches(3.9), Inches(3.9), "TOO BIG  (>500 tokens)",
     ["Above ~500: embedder literally can't read the tail.",
      "",
      "Legal but big: several topics smeared into one fuzzy vector —",
      "outranked by sharper pieces."],
     ORANGE_BG, ORANGE, tsize=15, bsize=13.5)
tb = textbox(s, Inches(0.5), Inches(6.2), SW - Inches(1), Inches(0.8))
para(tb.text_frame, "Rule of thumb: smallest piece that still contains one complete thought.", size=17, bold=True, color=ACCENT, align=PP_ALIGN.CENTER, first=True)

# ── Slide 7: the desk math ───────────────────────────────────────────
s = slide()
header(s, "Correlation #2 — the answerer's desk (the math)", "Chunk size and 'how many chunks' are the same dial")
card(s, Inches(0.5), Inches(1.55), Inches(6.1), Inches(4.6), "What the LLM sees — one flat budget",
     ["[ instructions + persona ]        ~1,000 tokens",
      "[ conversation history   ]        varies",
      "[ the question           ]        ~50",
      "[ ███ EVIDENCE (chunks) ███ ]  ← what remains (~6,000)",
      "[ its answer             ]        ~500 reserved",
      "",
      "chunks fed × chunk size  ≤  evidence budget"],
     GRAY_BG, INK, tsize=16, bsize=14)
card(s, Inches(6.9), Inches(1.55), Inches(5.9), Inches(2.1), "Fits ✅",
     ["8 chunks × 300 tokens = 2,400",
      "comfortable — room to spare"],
     GREEN_BG, GREEN, tsize=16, bsize=15)
card(s, Inches(6.9), Inches(4.0), Inches(5.9), Inches(2.1), "Doesn't fit ❌",
     ["8 chunks × 3,000 tokens = 24,000",
      "→ chunks get silently dropped (maybe the right one!)"],
     RGBColor(0xFD, 0xE8, 0xE8), RGBColor(0x9C, 0x00, 0x06), tsize=16, bsize=15)

# ── Slide 8: why several small > one big ─────────────────────────────
s = slide()
header(s, "Why several small pieces beat one big piece", "Fitting isn't the same as being used well")
card(s, Inches(0.5), Inches(1.6), Inches(4.0), Inches(4.7), "1 · Evidence mixing",
     ["Real answers need pieces from DIFFERENT places:",
      "policy + exception note + config reference.",
      "Small chunks = 8 best slivers from anywhere.",
      "Giant chunks = 2 chapters, 90% irrelevant."],
     GRAY_BG, INK, tsize=15.5, bsize=13.5)
card(s, Inches(4.66), Inches(1.6), Inches(4.0), Inches(4.7), "2 · \"Lost in the middle\"",
     ["LLMs read the start and end of long inputs carefully —",
      "and get fuzzy about what's buried in between.",
      "A fact inside a 3,000-token chunk is HARDER to use",
      "than the same sentence in a 300-token chunk."],
     PURPLE_BG, PURPLE, tsize=15.5, bsize=13.5)
card(s, Inches(8.82), Inches(1.6), Inches(4.0), Inches(4.7), "3 · Cost",
     ["Every stuffed token is paid for, every query.",
      "8 × 300   = 2.4k tokens",
      "8 × 3,000 = 24k tokens  ← 10× the cost,",
      "for WORSE answers. The expensive option loses twice."],
     ORANGE_BG, ORANGE, tsize=15.5, bsize=13.5)

# ── Slide 9: the three dials ─────────────────────────────────────────
s = slide()
header(s, "The pipeline in three numbers", "Set at different times, sized against each other")
y = Inches(2.1); bh = Inches(1.6); bw = Inches(3.55)
card(s, Inches(0.6), y, bw, bh, "~50 candidates",
     ["RETRIEVE — recall.", "vector + keyword search,", "don't miss anything."], GRAY_BG, INK, tsize=17, bsize=14)
card(s, Inches(4.85), y, bw, bh, "top ~8 chunks",
     ["RERANK — precision.", "keep only the honest best."], PURPLE_BG, PURPLE, tsize=17, bsize=14)
card(s, Inches(9.1), y, bw, bh, "× ~300 tokens each",
     ["CHUNK — the unit.", "small enough to fit,", "big enough to mean something."], ORANGE_BG, ORANGE, tsize=17, bsize=14)
arrow_right(s, Inches(4.28), Inches(2.75))
arrow_right(s, Inches(8.55), Inches(2.75))
tb = textbox(s, Inches(0.5), Inches(4.3), SW - Inches(1), Inches(1.6))
para(tb.text_frame, "8 × 300 ≈ 2,400 tokens of evidence — fits the desk perfectly.",
     size=20, bold=True, color=ACCENT, align=PP_ALIGN.CENTER, first=True)
para(tb.text_frame, "The 50 protects recall. The 8 protects precision. The 300 makes both fit.",
     size=15, color=SUB, align=PP_ALIGN.CENTER)

# ── Slide 10: what breaks without each ───────────────────────────────
s = slide()
header(s, "What happens without each piece?", "Same system, parts removed")
card(s, Inches(0.5), Inches(1.6), Inches(4.0), Inches(4.9), "No embedding model",
     ["Only keyword search left.",
      "",
      "\"reset a password\" can't find",
      "\"credential recovery\" — zero shared words.",
      "",
      "→ synonym/paraphrase recall collapses."],
     ORANGE_BG, ORANGE, tsize=15.5, bsize=14)
card(s, Inches(4.66), Inches(1.6), Inches(4.0), Inches(4.9), "No reranker",
     ["Raw top-50 goes straight to the desk.",
      "",
      "Desk fills with near-misses →",
      "LLM answers from the wrong piece,",
      "or gives up when the right one was buried."],
     PURPLE_BG, PURPLE, tsize=15.5, bsize=14)
card(s, Inches(8.82), Inches(1.6), Inches(4.0), Inches(4.9), "No RAG at all",
     ["LLM answers from memory alone:",
      "",
      "• hallucination-prone",
      "• stale (frozen in time)",
      "• no citations",
      "",
      "RAG IS these stages — remove them and there's nothing."],
     GREEN_BG, GREEN, tsize=15.5, bsize=14)

# ── Slide 11: takeaways ──────────────────────────────────────────────
s = slide()
header(s, "Take it home in five lines")
tb = textbox(s, Inches(0.9), Inches(1.8), SW - Inches(1.8), Inches(4.8))
lines = [
    ("1", "RAG = build an index once → for each question, find the right pieces → let the LLM answer from them.", 20),
    ("2", "The embedding model is the indexer's memory — it turns every chunk into a findable summary, and the question into a comparable one.", 20),
    ("3", "The reranker is the editor — 50 candidates in, honest top-8 out, with the power to say \"nothing here is good enough.\"", 20),
    ("4", "Chunks ~300 tokens because: embedder's reading limit above, complete thought below, LLM desk-space sideways.", 20),
    ("5", "Each stage is cheap for its job — that's the architecture: spend compute only where it's scarce.", 20),
]
for n, t, sz in lines:
    para(tb.text_frame, f"{n}.  {t}", size=sz, color=INK, space_after=16, first=(n == "1"))
tb2 = textbox(s, Inches(0.9), Inches(6.35), SW - Inches(1.8), Inches(0.7))
para(tb2.text_frame, "Retriever decides what the system can possibly know — the reranker decides how honest it is.",
     size=16, color=ACCENT, align=PP_ALIGN.CENTER, first=True)
tb2.text_frame.paragraphs[0].runs[0].font.italic = True

out = str(HERE / "RAG_Explained_Slides.pptx")
prs.save(out)
print("wrote", out)
