"""
Product matcher: TF-IDF pre-filter + rapidfuzz confirmation + size/quantity gates.

Pipeline (CPU-only, no PyTorch):
  1. TF-IDF over the whole candidate set (character + word n-grams), so common
     Greek grocery words (Γάλα, Κουτί, Συσκευασία) are down-weighted and rare,
     distinctive words (brands, product names) dominate similarity.
  2. Sparse cosine similarity finds CANDIDATE pairs fast (vectorized over the
     whole catalog at once) — this is the cheap pre-filter.
  3. rapidfuzz token_sort_ratio CONFIRMS each surviving candidate precisely.
  4. Hard GATES veto: different size/dimension, or conflicting quantity tokens
     (promo pack 5+1 vs 4+2, No7 vs No1, different piece counts) -> never match.

Public interface kept stable so storage's scan/ingest code needs no changes:
  * model_available()        -> True if scikit-learn is importable
  * find_candidate_pairs(rows, threshold) -> [(id_a, id_b, score), ...]
  * hybrid_score(...)        -> single-pair score (rapidfuzz + gates), used at
                                ingest and as the confirm step
  * enrich_size(name)        -> backfill grams/kg/L/ml/pieces
Thresholds are env-tunable.
"""

import os
import logging

import dedup

log = logging.getLogger("matcher")

AUTO_BLOCK = float(os.environ.get("MATCH_AUTO_BLOCK", "0.92"))
# Batch-scan "queue for review" floor. MUST equal the prune floor
# (dedup.FUZZY_SUGGEST), else scan re-adds exactly what prune drops and the
# queue churns forever (scan add >= SUGGEST, prune keep >= FUZZY_SUGGEST).
SUGGEST = float(os.environ.get("MATCH_SUGGEST", str(dedup.FUZZY_SUGGEST)))
# TF-IDF cosine above this makes a pair a CANDIDATE to be confirmed by rapidfuzz.
# Deliberately lower than SUGGEST: the pre-filter should be generous (high
# recall); rapidfuzz + gates then enforce precision.
PREFILTER = float(os.environ.get("MATCH_PREFILTER", "0.45"))


def _sklearn():
    """Import scikit-learn lazily; return (TfidfVectorizer, cosine) or (None,None)."""
    try:
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity
        return TfidfVectorizer, cosine_similarity
    except Exception as e:
        log.warning("scikit-learn unavailable (%s) — batch scan falls back to "
                    "blocked rapidfuzz pairing.", e)
        return None, None


def model_available():
    TfidfVectorizer, _ = _sklearn()
    return TfidfVectorizer is not None


def _rf_ratio(a, b):
    """rapidfuzz token_sort_ratio in [0,1], difflib fallback."""
    try:
        from rapidfuzz import fuzz
        return fuzz.token_sort_ratio(a, b) / 100.0
    except Exception:
        import difflib
        sa, sb = " ".join(sorted(a.split())), " ".join(sorted(b.split()))
        return difflib.SequenceMatcher(None, sa, sb).ratio()


def _gated(name_a, brand_a, name_b, brand_b):
    """Apply the hard gates. Return True if the pair is ALLOWED (no conflict)."""
    sa, sb = dedup.extract_size(name_a), dedup.extract_size(name_b)
    if sa and sb and sa != sb:
        return False
    if dedup.signatures_conflict(name_a, name_b):
        return False
    return True


def hybrid_score(name_a, brand_a, name_b, brand_b, emb_a=None, emb_b=None):
    """
    Single-pair similarity in [0,1]. Delegates to dedup.fuzzy_score so that the
    SCAN (which calls this) and the PRUNE (which calls dedup.fuzzy_score) use the
    EXACT SAME scoring — otherwise scan adds candidates that prune immediately
    drops, churning the queue forever. dedup.fuzzy_score already applies the
    size/quantity hard gates and the variant-word/percentage/shade penalties.
    (emb_* ignored; kept for call-site compatibility.)
    """
    return dedup.fuzzy_score(name_a, brand_a, name_b, brand_b)


def find_candidate_pairs(rows, threshold=None):
    """
    Given rows [{'id','name','brand'}, ...], return [(id_a, id_b, score), ...]
    for likely-duplicate pairs. TF-IDF pre-filters across the WHOLE set at once,
    then rapidfuzz + gates confirm. Falls back to first-token blocking + pairwise
    rapidfuzz if scikit-learn isn't available.

    `threshold` is the final confirmed-score cutoff (defaults to SUGGEST).
    """
    threshold = SUGGEST if threshold is None else threshold
    if len(rows) < 2:
        return []

    essences = [dedup.norm_name_nobrand(r["name"], r["brand"]) for r in rows]
    TfidfVectorizer, cosine_similarity = _sklearn()

    pairs = []
    if TfidfVectorizer is not None:
        import numpy as np
        # char n-grams catch spelling/transliteration; word n-grams catch
        # token identity. Combined analyzer = char_wb is robust for Greek.
        vec = TfidfVectorizer(analyzer="char_wb", ngram_range=(2, 4),
                              min_df=1, lowercase=True)
        try:
            tfidf = vec.fit_transform(essences)
        except ValueError:
            return []                      # empty vocabulary (all blank)
        # Candidate pre-filter via sparse cosine. Compute row-blocks to bound
        # memory on large catalogs rather than a full NxN dense matrix.
        n = tfidf.shape[0]
        BLOCK = 512
        for start in range(0, n, BLOCK):
            end = min(start + BLOCK, n)
            sims = cosine_similarity(tfidf[start:end], tfidf)   # (block x n)
            for li in range(end - start):
                gi = start + li
                row = sims[li]
                # only upper triangle (j > gi) to avoid dup/self pairs
                for gj in np.where(row >= PREFILTER)[0]:
                    if gj <= gi:
                        continue
                    pairs.append((gi, gj))
    else:
        # Fallback: block by two longest tokens, pair within blocks.
        block = {}
        for idx, r in enumerate(rows):
            toks = sorted(set(dedup.norm_name(r["name"]).split()),
                          key=len, reverse=True)
            for key in toks[:2]:
                if len(key) >= 3:
                    block.setdefault(key, []).append(idx)
        seen = set()
        for grp in block.values():
            for i in range(len(grp)):
                for j in range(i + 1, len(grp)):
                    p = (min(grp[i], grp[j]), max(grp[i], grp[j]))
                    if p not in seen:
                        seen.add(p)
                        pairs.append(p)

    # Confirm each candidate pair with rapidfuzz + gates.
    out = []
    seen = set()
    for gi, gj in pairs:
        a, b = rows[gi], rows[gj]
        key = (a["id"], b["id"])
        if key in seen:
            continue
        seen.add(key)
        score = hybrid_score(a["name"], a["brand"], b["name"], b["brand"])
        if threshold <= score < 1.0 or score >= 1.0:
            out.append((a["id"], b["id"], round(score, 3)))
    return out


def enrich_size(name):
    """Return {'size_value','size_unit','size_dim'} to backfill, or {}."""
    sz = dedup.extract_size(name)
    if not sz:
        return {}
    dim, base_amount = sz
    unit = {"vol": "ml", "mass": "g", "pc": "pc"}.get(dim)
    return {"size_value": base_amount, "size_unit": unit, "size_dim": dim}


# Back-compat: embeddings removed.
def embed(texts):
    return None
