import json

import anthropic
import streamlit as st
import trafilatura
from rapidfuzz import fuzz, process

from utils.db import (
    get_fighter_aliases,
    get_or_create_event,
    get_or_create_fight,
    save_alias,
    save_analyst_pick,
    save_pick_tags,
)

# ── Constants ────────────────────────────────────────────────────────────────

EXTRACTION_PROMPT = """You are a data extraction assistant for MMA fight predictions. You will be given the text of a sports article containing analyst fight picks.

Your job is to extract all fight predictions and return them as structured JSON only. No explanation, no markdown, no preamble — raw JSON only.

Rules:
1. Detect whether this is a single-analyst article or a multi-analyst "staff picks" article.
2. If multi-analyst, group each pick under the correct analyst name.
3. Extract ALL fight predictions regardless of format. This includes:
   - Fights with full written breakdowns or analysis
   - Fights listed as quick picks, bullet points, or simple name-only lists (e.g. "Prelims: Fighter A, Fighter B")
   - Fights in tables, sidebars, or summary sections at the top or bottom of the article
   Do not skip any fight just because it lacks prose analysis.
4. For each fight, extract both fighters' names exactly as written, then extract who the analyst picked to win.
5. If an analyst uses a nickname (e.g. "Stylebender", "Gamebred", "The Nigerian Nightmare"), preserve it in a "nickname_used" field — do not try to resolve it yourself.
6. If a fighter name has an alternate transliteration or spelling uncertainty, note it in an "alt_spelling_note" field.
7. If a prediction includes a winning method, capture it in "method_prediction" using EXACTLY one of these values (or null if none stated):
   - "KO/TKO"  — for knockout, TKO, stoppage, strikes
   - "Submission"  — for any submission finish
   - "Decision"  — for any decision (unanimous, split, majority)
   - "NC"  — no contest
   - "DQ"  — disqualification
8. If the analyst gives reasoning or key factors, summarize it briefly in "reasoning_notes" (max 30 words).
9. If you cannot confidently determine who an analyst picked for a fight, set "picked_fighter" to null and "flag_for_review" to true.
10. Never invent or assume a pick. When in doubt, flag it.

Return this JSON structure:
{
  "article_type": "single" or "staff",
  "analysts": [
    {
      "analyst_name": "string",
      "picks": [
        {
          "fighter_a": "string",
          "fighter_b": "string",
          "picked_fighter": "string or null",
          "nickname_used": "string or null",
          "alt_spelling_note": "string or null",
          "method_prediction": "KO/TKO" or "Submission" or "Decision" or "NC" or "DQ" or null,
          "confidence_tag": "lean / confident / lock",
          "reasoning_notes": "string or null",
          "flag_for_review": false
        }
      ]
    }
  ]
}"""

CONFIDENCE_OPTIONS = ["lean", "confident", "lock"]
METHOD_OPTIONS = ["", "KO/TKO", "Submission", "Decision", "NC", "DQ"]
FUZZY_THRESHOLD = 85

# Normalize free-text method strings Claude might return to the canonical values above
_METHOD_NORMALIZER = {
    "ko": "KO/TKO",
    "tko": "KO/TKO",
    "ko/tko": "KO/TKO",
    "knockout": "KO/TKO",
    "stoppage": "KO/TKO",
    "strikes": "KO/TKO",
    "submission": "Submission",
    "sub": "Submission",
    "rear naked choke": "Submission",
    "guillotine": "Submission",
    "triangle": "Submission",
    "armbar": "Submission",
    "decision": "Decision",
    "unanimous decision": "Decision",
    "split decision": "Decision",
    "majority decision": "Decision",
    "ud": "Decision",
    "sd": "Decision",
    "md": "Decision",
    "points": "Decision",
    "nc": "NC",
    "no contest": "NC",
    "dq": "DQ",
    "disqualification": "DQ",
}


def normalize_method(raw: str | None) -> str:
    """Map any Claude-returned method string to an exact METHOD_OPTIONS value, or ''."""
    if not raw:
        return ""
    key = raw.strip().lower()
    if raw in METHOD_OPTIONS:
        return raw
    return _METHOD_NORMALIZER.get(key, "")


# ── Helpers ──────────────────────────────────────────────────────────────────

def scrape_url(url: str) -> str | None:
    downloaded = trafilatura.fetch_url(url)
    if downloaded:
        return trafilatura.extract(downloaded)
    return None


def call_claude(article_text: str) -> dict:
    # Support both nested [anthropic] section and flat ANTHROPIC_API_KEY
    if "anthropic" in st.secrets:
        api_key = st.secrets["anthropic"]["api_key"]
    elif "ANTHROPIC_API_KEY" in st.secrets:
        api_key = st.secrets["ANTHROPIC_API_KEY"]
    else:
        available = list(st.secrets.keys())
        raise KeyError(
            f"Anthropic API key not found. Available secret keys: {available}. "
            "Add ANTHROPIC_API_KEY = \"sk-ant-...\" to your Streamlit secrets."
        )
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        messages=[{"role": "user", "content": EXTRACTION_PROMPT + "\n\n" + article_text}],
    )
    raw = message.content[0].text.strip()
    # Strip markdown code fences if the model wraps output anyway
    if raw.startswith("```"):
        parts = raw.split("```")
        raw = parts[1]
        if raw.startswith("json"):
            raw = raw[4:]
    return json.loads(raw.strip())


def fuzzy_match(name: str, aliases: list[dict]) -> tuple[str | None, int]:
    """Return (best_canonical_name, score 0-100) against the aliases table."""
    if not aliases:
        return None, 0
    alias_to_canonical = {a["alias"]: a["canonical_name"] for a in aliases}
    for a in aliases:
        alias_to_canonical[a["canonical_name"]] = a["canonical_name"]
    result = process.extractOne(name, list(alias_to_canonical.keys()), scorer=fuzz.WRatio)
    if result:
        return alias_to_canonical[result[0]], int(result[1])
    return None, 0


def reset_session():
    for k in list(st.session_state.keys()):
        if k.startswith("ing_"):
            del st.session_state[k]


# ── Page header ──────────────────────────────────────────────────────────────

st.title("URL Ingestion")
st.caption("Paste an article URL to extract analyst picks via AI.")

if "ing_stage" not in st.session_state:
    st.session_state.ing_stage = "input"

if st.session_state.ing_stage != "input":
    if st.button("↩ Start over", type="secondary"):
        reset_session()
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# STAGE: input — URL entry
# ══════════════════════════════════════════════════════════════════════════════
if st.session_state.ing_stage == "input":
    url = st.text_input("Article URL", placeholder="https://...")
    if st.button("Scrape", type="primary", disabled=not url):
        with st.spinner("Scraping article…"):
            text = scrape_url(url)
        st.session_state.ing_url = url
        if text:
            st.session_state.ing_article_text = text
            st.session_state.ing_stage = "text_ready"
        else:
            st.session_state.ing_article_text = ""
            st.session_state.ing_stage = "paste_fallback"
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# STAGE: paste_fallback — scraping blocked, let user paste
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.ing_stage == "paste_fallback":
    st.warning(
        f"Could not scrape **{st.session_state.ing_url}** — the site may block bots. "
        "Paste the article text below instead."
    )
    pasted = st.text_area("Article text", height=300, placeholder="Paste article text here…")
    if st.button("Use this text →", type="primary", disabled=not pasted):
        st.session_state.ing_article_text = pasted
        st.session_state.ing_stage = "text_ready"
        st.rerun()

# ══════════════════════════════════════════════════════════════════════════════
# STAGE: text_ready — preview scraped text and trigger extraction
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.ing_stage == "text_ready":
    char_count = len(st.session_state.ing_article_text)
    st.success(f"Article ready — {char_count:,} characters")
    with st.expander("Preview article text"):
        preview = st.session_state.ing_article_text[:3000]
        if char_count > 3000:
            preview += "\n\n[… truncated for preview …]"
        st.text(preview)

    if st.button("Extract picks with AI ✨", type="primary"):
        with st.spinner("Calling Claude Haiku — this takes a few seconds…"):
            try:
                extracted = call_claude(st.session_state.ing_article_text)
                st.session_state.ing_extracted = extracted
                st.session_state.ing_stage = "review_picks"
                st.rerun()
            except Exception as e:
                st.error(f"Extraction failed: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# STAGE: review_picks — edit picks, resolve names, save
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.ing_stage == "review_picks":
    extracted = st.session_state.ing_extracted
    aliases = get_fighter_aliases()

    analysts = extracted.get("analysts", [])
    total_picks = sum(len(a.get("picks", [])) for a in analysts)

    st.subheader("Review Extracted Picks")
    st.caption(
        f"Article type: **{extracted.get('article_type', '?')}** · "
        f"**{total_picks}** pick(s) across **{len(analysts)}** analyst(s)"
    )

    event_name = st.text_input(
        "Event name *",
        placeholder="e.g. UFC 315",
        help="Required. A new event row is created automatically if this name doesn't exist yet.",
    )

    st.divider()

    # Collect edits in a list we'll walk when saving
    analysts_data = []

    for ai, analyst in enumerate(analysts):
        st.markdown(f"### Analyst: {analyst.get('analyst_name', '')}")
        analyst_name_edit = st.text_input(
            "Analyst name",
            value=analyst.get("analyst_name", ""),
            key=f"analyst_{ai}",
        )

        picks_data = []
        for pi, pick in enumerate(analyst.get("picks", [])):
            with st.container(border=True):
                if pick.get("flag_for_review"):
                    st.error("🚩 AI flagged this pick — it could not determine the winner confidently.")

                if pick.get("nickname_used"):
                    st.info(f"Nickname detected: **{pick['nickname_used']}**")
                if pick.get("alt_spelling_note"):
                    st.info(f"Spelling note: {pick['alt_spelling_note']}")

                c1, c2 = st.columns(2)
                with c1:
                    fa = st.text_input(
                        "Fighter A", value=pick.get("fighter_a", ""), key=f"fa_{ai}_{pi}"
                    )
                with c2:
                    fb = st.text_input(
                        "Fighter B", value=pick.get("fighter_b", ""), key=f"fb_{ai}_{pi}"
                    )

                picked = st.text_input(
                    "Picked to win",
                    value=pick.get("picked_fighter") or "",
                    key=f"picked_{ai}_{pi}",
                )

                c3, c4 = st.columns(2)
                with c3:
                    raw_method = normalize_method(pick.get("method_prediction"))
                    method_idx = METHOD_OPTIONS.index(raw_method) if raw_method in METHOD_OPTIONS else 0
                    method = st.selectbox(
                        "Method prediction",
                        METHOD_OPTIONS,
                        index=method_idx,
                        key=f"method_{ai}_{pi}",
                    )
                with c4:
                    raw_conf = pick.get("confidence_tag") or "lean"
                    conf_idx = (
                        CONFIDENCE_OPTIONS.index(raw_conf)
                        if raw_conf in CONFIDENCE_OPTIONS
                        else 0
                    )
                    confidence = st.selectbox(
                        "Confidence",
                        CONFIDENCE_OPTIONS,
                        index=conf_idx,
                        key=f"conf_{ai}_{pi}",
                    )

                reasoning = st.text_area(
                    "Reasoning notes",
                    value=pick.get("reasoning_notes") or "",
                    height=80,
                    key=f"reasoning_{ai}_{pi}",
                )

                tags_raw = st.text_input(
                    "Tags (comma-separated)",
                    value="",
                    placeholder="e.g. grappling-edge, title-fight",
                    key=f"tags_{ai}_{pi}",
                )

                # ── Fighter name resolution ───────────────────────────────
                name_overrides: dict[str, str] = {}

                if aliases:
                    names_to_check = {n for n in [fa, fb, picked] if n}
                    for name in sorted(names_to_check):
                        canonical, score = fuzzy_match(name, aliases)
                        if score < FUZZY_THRESHOLD:
                            with st.expander(
                                f"⚠️ Name not confidently matched: **{name}**"
                                + (f" (closest: '{canonical}', {score}%)" if canonical else ""),
                                expanded=True,
                            ):
                                opts = ["Use as-is (treat as new fighter)"]
                                if canonical:
                                    opts.append(f'Map to "{canonical}" ({score}%)')
                                opts.append("Enter canonical name manually")

                                choice = st.radio(
                                    "What should we do with this name?",
                                    opts,
                                    key=f"res_{ai}_{pi}_{name}",
                                    horizontal=True,
                                )

                                if canonical and choice == f'Map to "{canonical}" ({score}%)':
                                    name_overrides[name] = canonical
                                elif choice == "Enter canonical name manually":
                                    manual = st.text_input(
                                        "Canonical name",
                                        value=name,
                                        key=f"man_{ai}_{pi}_{name}",
                                    )
                                    name_overrides[name] = manual
                                # else: use as-is, no entry in overrides

                picks_data.append(
                    {
                        "fighter_a": fa,
                        "fighter_b": fb,
                        "picked_fighter": picked,
                        "method": method,
                        "confidence": confidence,
                        "reasoning": reasoning,
                        "tags": [t.strip() for t in tags_raw.split(",") if t.strip()],
                        "name_overrides": name_overrides,
                    }
                )

        analysts_data.append({"analyst_name": analyst_name_edit, "picks": picks_data})

    st.divider()

    save_disabled = not event_name.strip()
    if save_disabled:
        st.caption("Enter an event name above to enable saving.")

    if st.button("💾 Save all picks", type="primary", disabled=save_disabled):
        saved_count = 0
        try:
            event_id = get_or_create_event(event_name.strip())

            for analyst in analysts_data:
                for pick in analyst["picks"]:
                    overrides = pick["name_overrides"]

                    fa = overrides.get(pick["fighter_a"], pick["fighter_a"])
                    fb = overrides.get(pick["fighter_b"], pick["fighter_b"])
                    picked = overrides.get(pick["picked_fighter"], pick["picked_fighter"])

                    # Persist any new aliases the user chose to map
                    for orig, canon in overrides.items():
                        if orig != canon:
                            save_alias(canon, orig)

                    fight_id = get_or_create_fight(event_id, fa, fb)

                    pick_row = {
                        "fight_id": fight_id,
                        "analyst_name": analyst["analyst_name"],
                        "source_url": st.session_state.get("ing_url", ""),
                        "picked_fighter": picked or None,
                        "method_prediction": pick["method"] or None,
                        "confidence_tag": pick["confidence"],
                        "reasoning_notes": pick["reasoning"] or None,
                    }
                    pick_id = save_analyst_pick(pick_row)
                    save_pick_tags(pick_id, pick["tags"])
                    saved_count += 1

            st.session_state.ing_saved_count = saved_count
            st.session_state.ing_saved_event = event_name.strip()
            st.session_state.ing_stage = "done"
            st.rerun()

        except Exception as e:
            st.error(f"Save failed: {e}")

# ══════════════════════════════════════════════════════════════════════════════
# STAGE: done
# ══════════════════════════════════════════════════════════════════════════════
elif st.session_state.ing_stage == "done":
    st.success(
        f"✅ Saved **{st.session_state.get('ing_saved_count', 0)}** pick(s) "
        f"for **{st.session_state.get('ing_saved_event', '')}**."
    )
    if st.button("Ingest another article", type="primary"):
        reset_session()
        st.rerun()
