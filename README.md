# chatmmatracker
I want to build a new multi-page Streamlit app for tracking MMA analyst predictions and fight results. Here is the full scope. Please build it phase by phase, confirming with me before moving to the next phase.

PROJECT NAME: ChatMMA Analyst Tracker
TECH STACK:
* Streamlit (multi-page app)
* Supabase (PostgreSQL database, free tier)
* Anthropic API using claude-haiku-4-5-20251001 for AI extraction only
* trafilatura for web scraping
* All credentials via st.secrets only — never hardcoded. Before writing any code, create a .gitignore that includes .env, .streamlit/secrets.toml, *.pem, and pycache/

DATABASE SCHEMA — create all of these tables in Supabase:


events: event_id (uuid pk), name, date, location, promotion, created_at

fights: fight_id (uuid pk), event_id (fk), fighter_a, fighter_b, weight_class, bout_order (int), title_fight (bool), status (text: scheduled/completed/cancelled)

fighter_aliases: alias_id (uuid pk), canonical_name, alias

analyst_picks: pick_id (uuid pk), fight_id (fk), analyst_name, source_url, picked_fighter, method_prediction, confidence_tag, reasoning_notes, created_at

pick_tags: tag_id (uuid pk), pick_id (fk), tag

results: result_id (uuid pk), fight_id (fk), winner, method (KO/TKO/Submission/Decision/NC/DQ), round (int), time (text), referee, judge1_name, judge1_score, judge2_name, judge2_score, judge3_name, judge3_score

APP PAGES — build these in order:
Page 1: URL Ingestion
* Text input field for an article URL
* Scrape the article text using trafilatura
* If trafilatura returns nothing (site blocks scraping), show a fallback text area where I can paste the article text manually
* Send the text to Claude Haiku API with the extraction prompt defined below
* Display the returned picks in an editable table/form before saving
* Fighter name matching: after extraction, look up each extracted name against the fighter_aliases table (fuzzy match using rapidfuzz library). If a name doesn't match with confidence above 85%, flag it in yellow and ask me to either confirm the match, map it to a canonical name (which saves a new alias), or enter it as a new fighter
* Save confirmed picks to analyst_picks and pick_tags tables
Page 2: QC / Editor
* Browse picks by event or by analyst
* Edit any field inline
* Mark a fight as cancelled (sets status = 'cancelled', does not delete the row)
* Delete individual picks if needed (with confirmation dialog)
Page 3: Results Entry
* Select event from dropdown, fights for that event populate
* For each fight: winner, method, round, time, referee
* If method = Decision, show scorecard fields: three rows of judge name + score (e.g. 29-28, 30-27)
* Batch save whole card at once
Page 4: Analytics
* Analyst leaderboard: record (W-L), pick accuracy percentage
* Filter by: analyst name, weight class, title fights only, date range
* Method prediction accuracy: what % of the time did they correctly predict KO vs Decision vs Sub
* Scorecard view: for decision fights, show all judge scores
* Only show analytics for fights that have results entered (status = completed)

EXTRACTION PROMPT — use this exact prompt when calling Claude Haiku:


You are a data extraction assistant for MMA fight predictions. You will be given the text of a sports article containing analyst fight picks.

Your job is to extract all fight predictions and return them as structured JSON only. No explanation, no markdown, no preamble — raw JSON only.

Rules:
1. Detect whether this is a single-analyst article or a multi-analyst "staff picks" article.
2. If multi-analyst, group each pick under the correct analyst name.
3. For each fight, extract both fighters' names exactly as written, then extract who the analyst picked to win.
4. If an analyst uses a nickname (e.g. "Stylebender", "Gamebred", "The Nigerian Nightmare"), preserve it in a "nickname_used" field — do not try to resolve it yourself.
5. If a fighter name has an alternate transliteration or spelling uncertainty, note it in an "alt_spelling_note" field.
6. If a prediction includes a method (KO, submission, decision), capture it in "method_prediction".
7. If the analyst gives reasoning or key factors, summarize it briefly in "reasoning_notes" (max 30 words).
8. If you cannot confidently determine who an analyst picked for a fight, set "picked_fighter" to null and "flag_for_review" to true.
9. Never invent or assume a pick. When in doubt, flag it.

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
          "method_prediction": "string or null",
          "confidence_tag": "lean / confident / lock",
          "reasoning_notes": "string or null",
          "flag_for_review": false
        }
      ]
    }
  ]
}

SECURITY REQUIREMENTS — non-negotiable:
* All API keys and database credentials must be loaded exclusively via st.secrets
* Create a secrets.toml.example file (with placeholder values, not real keys) so I know the required format
* Add .streamlit/secrets.toml to .gitignore before writing any other files
* Never put any credential in any Python file

START WITH PHASE 1: Set up the project structure, .gitignore, secrets.toml.example, Supabase schema (give me the SQL to run in Supabase's SQL editor), and the app skeleton with page routing. Confirm with me before building Page 1.
