# RAG explained — presentation assets

Beginner-friendly explainer material for the RAG pipeline.

| File | What it is |
|---|---|
| `RAG_pipeline.png` | The two-phase pipeline diagram — indexing (ingest → extract → chunk → embed → index) and answering (question → embed → retrieve → rerank → augment → generate). The embedding model drawn twice, reranker and LLM color-coded. Slides-ready 16:9. |
| `RAG_Explained_Slides.pptx` | 11-slide editable deck: pipeline picture → book analogy (Indexer / Editor / Answerer) → three models three jobs → why chunk → the ~300-token sizing rule → the context-budget math → small-beats-big → the three dials → what breaks without each → takeaways. |

## Regenerating

```bash
pip install matplotlib python-pptx Pillow
python presentations/generate/rag_pipeline.py       # the PNG
python presentations/generate/make_rag_slides.py    # the deck (embeds the PNG)
```

Outputs write into this `presentations/` directory.
