"""
ChatMMAPicks – chat backend.

Adapts the original ChatMMA query-optimizer / prompt-generator / chatbot
to work with the Supabase data model used by ChatMMAPicks Tracker.

Schema mapping (ChatMMA original → ChatMMAPicks Supabase):
  predictions.pick ('fighter_a'/'fighter_b') → analyst_picks.picked_fighter (actual name)
  predictions.context_tags (JSON array)      → pick_tags table (separate rows)
  predictions.notes                          → analyst_picks.reasoning_notes
  predictions.confidence (high/med/low)      → analyst_picks.confidence_tag (lock/confident/lean)
  predictions.method                         → analyst_picks.method_prediction
  analysts.accuracy_rate                     → not yet tracked (defaults to 0)
"""

import re
from collections import Counter

import streamlit as st
from anthropic import Anthropic
from rapidfuzz import fuzz, process

from utils.db import get_supabase


# ---------------------------------------------------------------------------
# QueryOptimizer
# ---------------------------------------------------------------------------

class QueryOptimizer:
    """Queries Supabase to build context dicts that feed PromptGenerator."""

    # ── internal helpers ────────────────────────────────────────────────────

    def _get_event(self, event_name: str) -> dict | None:
        """Case-insensitive event lookup. Returns event row or None."""
        db = get_supabase()
        resp = (
            db.table("events")
            .select("event_id, name, date, location")
            .ilike("name", f"%{event_name}%")
            .order("date", desc=True)
            .limit(1)
            .execute()
        )
        return resp.data[0] if resp.data else None

    def _get_fights_for_event(self, event_id: str) -> list[dict]:
        db = get_supabase()
        resp = (
            db.table("fights")
            .select("fight_id, fighter_a, fighter_b, weight_class, bout_order, status")
            .eq("event_id", event_id)
            .execute()
        )
        return resp.data or []

    def _get_picks_for_fight(self, fight_id: str) -> list[dict]:
        """Return analyst_picks rows for a fight, with tags pre-attached."""
        db = get_supabase()
        picks_resp = (
            db.table("analyst_picks")
            .select(
                "pick_id, analyst_name, platform, picked_fighter, "
                "method_prediction, confidence_tag, reasoning_notes"
            )
            .eq("fight_id", fight_id)
            .execute()
        )
        picks = picks_resp.data or []
        if not picks:
            return []

        # Attach tags
        pick_ids = [p["pick_id"] for p in picks]
        tags_resp = (
            db.table("pick_tags")
            .select("pick_id, tag")
            .in_("pick_id", pick_ids)
            .execute()
        )
        tags_by_pick: dict[str, list[str]] = {}
        for row in tags_resp.data or []:
            tags_by_pick.setdefault(row["pick_id"], []).append(row["tag"])

        for p in picks:
            p["tags"] = tags_by_pick.get(p["pick_id"], [])

        return picks

    def _classify_picks(
        self, picks: list[dict], fighter_a: str, fighter_b: str
    ) -> tuple[list[dict], list[dict]]:
        """
        Split picks into those for fighter_a vs fighter_b.
        Uses rapidfuzz to handle minor name variations.
        """
        picks_a, picks_b = [], []
        for p in picks:
            picked = p.get("picked_fighter") or ""
            score_a = fuzz.token_set_ratio(picked.lower(), fighter_a.lower())
            score_b = fuzz.token_set_ratio(picked.lower(), fighter_b.lower())
            if score_a >= score_b and score_a >= 60:
                picks_a.append(p)
            elif score_b > score_a and score_b >= 60:
                picks_b.append(p)
        return picks_a, picks_b

    def _build_fighter_context(self, picks: list[dict]) -> dict:
        all_tags: list[str] = []
        for p in picks:
            all_tags.extend(p.get("tags", []))

        tag_counts = Counter(all_tags)
        methods = Counter(
            p["method_prediction"] for p in picks if p.get("method_prediction")
        )
        rationales = [
            p["reasoning_notes"]
            for p in picks
            if p.get("reasoning_notes")
        ][:3]

        return {
            "top_tags": [
                {"tag": tag, "count": cnt}
                for tag, cnt in tag_counts.most_common(5)
            ],
            "methods": dict(methods),
            "example_rationales": rationales,
        }

    # ── public API ───────────────────────────────────────────────────────────

    def get_fight_by_fighters(
        self, fighter_a_hint: str, fighter_b_hint: str, event_name: str | None = None
    ) -> dict | None:
        """
        Find a fight by two partial fighter names (fuzzy).
        Returns a dict with fight_id, fighter_a, fighter_b, event, date.
        """
        db = get_supabase()

        # Build base query – try both orderings with ILIKE
        def _try_order(fa_hint, fb_hint):
            return (
                db.table("fights")
                .select(
                    "fight_id, fighter_a, fighter_b, "
                    "events(event_id, name, date, location)"
                )
                .ilike("fighter_a", f"%{fa_hint}%")
                .ilike("fighter_b", f"%{fb_hint}%")
                .order("events(date)", desc=True)
                .limit(5)
                .execute()
            )

        for fa, fb in [
            (fighter_a_hint, fighter_b_hint),
            (fighter_b_hint, fighter_a_hint),
        ]:
            try:
                resp = _try_order(fa, fb)
                rows = resp.data or []
            except Exception:
                rows = []

            if rows:
                # If event_name specified, prefer that event
                if event_name:
                    for row in rows:
                        ev = row.get("events") or {}
                        if event_name.lower() in (ev.get("name") or "").lower():
                            return self._format_fight_row(row)
                return self._format_fight_row(rows[0])

        return None

    def _format_fight_row(self, row: dict) -> dict:
        ev = row.get("events") or {}
        return {
            "fight_id": row["fight_id"],
            "fighter_a": row["fighter_a"],
            "fighter_b": row["fighter_b"],
            "event": ev.get("name", "Unknown Event"),
            "date": ev.get("date"),
            "results_entered": False,   # placeholder until Results page is live
        }

    def aggregate_fight_context(self, fight_id: str, fight_meta: dict | None = None) -> dict | None:
        """
        Aggregate all picks for a fight into optimized context for Claude.
        fight_meta (optional): pre-fetched dict with fighter_a, fighter_b, event, date.
        """
        if fight_meta is None:
            db = get_supabase()
            resp = (
                db.table("fights")
                .select("fight_id, fighter_a, fighter_b, events(name, date)")
                .eq("fight_id", fight_id)
                .limit(1)
                .execute()
            )
            if not resp.data:
                return None
            row = resp.data[0]
            ev = row.get("events") or {}
            fight_meta = {
                "fight_id": fight_id,
                "fighter_a": row["fighter_a"],
                "fighter_b": row["fighter_b"],
                "event": ev.get("name", "Unknown Event"),
                "date": ev.get("date"),
                "results_entered": False,
            }

        picks = self._get_picks_for_fight(fight_id)
        if not picks:
            return None

        fa = fight_meta["fighter_a"]
        fb = fight_meta["fighter_b"]
        picks_a, picks_b = self._classify_picks(picks, fa, fb)

        return {
            "fight": {
                "fighter_a": fa,
                "fighter_b": fb,
                "event": fight_meta["event"],
                "results_entered": fight_meta.get("results_entered", False),
            },
            "summary": {
                "total_predictions": len(picks),
                "picks_for_a": len(picks_a),
                "picks_for_b": len(picks_b),
            },
            "fighter_a_context": self._build_fighter_context(picks_a),
            "fighter_b_context": self._build_fighter_context(picks_b),
            "analyst_info": {
                "fighter_a_high_accuracy_count": len(picks_a),
                "fighter_b_high_accuracy_count": len(picks_b),
                "reveal_names": True,   # always reveal in our own tracker
                "top_analysts_a": list({p["analyst_name"] for p in picks_a})[:5],
                "top_analysts_b": list({p["analyst_name"] for p in picks_b})[:5],
            },
        }

    def get_event_consensus_picks(self, event_name: str) -> dict | None:
        event = self._get_event(event_name)
        if not event:
            return None

        fights = self._get_fights_for_event(event["event_id"])
        consensus_picks = []

        for fight in fights:
            picks = self._get_picks_for_fight(fight["fight_id"])
            if not picks:
                continue

            picks_a, picks_b = self._classify_picks(
                picks, fight["fighter_a"], fight["fighter_b"]
            )
            total = len(picks_a) + len(picks_b)
            if total == 0:
                continue

            consensus_count = max(len(picks_a), len(picks_b))
            consensus_fighter = (
                fight["fighter_a"] if len(picks_a) >= len(picks_b) else fight["fighter_b"]
            )
            opposing_count = min(len(picks_a), len(picks_b))

            consensus_picks.append({
                "fight": f"{fight['fighter_a']} vs {fight['fighter_b']}",
                "fighter_a": fight["fighter_a"],
                "fighter_b": fight["fighter_b"],
                "consensus_fighter": consensus_fighter,
                "consensus_count": consensus_count,
                "opposing_count": opposing_count,
                "total_predictions": total,
                "consensus_percentage": (consensus_count / total) * 100,
                "high_accuracy_count": consensus_count,  # simplified until accuracy is tracked
            })

        consensus_picks.sort(key=lambda x: x["consensus_percentage"], reverse=True)

        return {
            "event": event["name"],
            "results_entered": False,
            "consensus_picks": consensus_picks,
        }

    def get_inside_distance_picks(self, event_name: str) -> dict | None:
        event = self._get_event(event_name)
        if not event:
            return None

        fights = self._get_fights_for_event(event["event_id"])
        inside_distance_fights = []

        finish_methods = {"KO/TKO", "Submission", "KO", "TKO", "Sub"}

        for fight in fights:
            picks = self._get_picks_for_fight(fight["fight_id"])
            finish_picks = [
                p for p in picks
                if p.get("method_prediction") in finish_methods
            ]
            if len(finish_picks) < 3:
                continue

            picks_a, picks_b = self._classify_picks(
                finish_picks, fight["fighter_a"], fight["fighter_b"]
            )

            fa_count = len(picks_a)
            fb_count = len(picks_b)
            if fa_count == 0 and fb_count == 0:
                continue

            favored = fight["fighter_a"] if fa_count >= fb_count else fight["fighter_b"]
            finish_count = max(fa_count, fb_count)
            method_picks = picks_a if favored == fight["fighter_a"] else picks_b
            methods = [
                {"method": p["method_prediction"]}
                for p in method_picks
                if p.get("method_prediction")
            ]

            inside_distance_fights.append({
                "fight": f"{fight['fighter_a']} vs {fight['fighter_b']}",
                "fighter_a": fight["fighter_a"],
                "fighter_b": fight["fighter_b"],
                "favored_fighter": favored,
                "finish_prediction_count": finish_count,
                "methods": methods,
                "total_finish_predictions": len(finish_picks),
            })

        inside_distance_fights.sort(
            key=lambda x: x["finish_prediction_count"], reverse=True
        )

        return {
            "event": event["name"],
            "inside_distance_picks": inside_distance_fights,
        }

    def get_event_underdogs(self, event_name: str) -> dict | None:
        event = self._get_event(event_name)
        if not event:
            return None

        fights = self._get_fights_for_event(event["event_id"])
        underdog_picks = []

        for fight in fights:
            picks = self._get_picks_for_fight(fight["fight_id"])
            picks_a, picks_b = self._classify_picks(
                picks, fight["fighter_a"], fight["fighter_b"]
            )
            total = len(picks_a) + len(picks_b)
            if total < 5:
                continue

            underdog_is_a = len(picks_a) < len(picks_b)
            underdog_fighter = fight["fighter_a"] if underdog_is_a else fight["fighter_b"]
            underdog_picks_list = picks_a if underdog_is_a else picks_b
            favorite_picks_list = picks_b if underdog_is_a else picks_a
            underdog_count = len(underdog_picks_list)
            favorite_count = len(favorite_picks_list)

            if underdog_count < 2 or underdog_count >= total / 2:
                continue

            all_tags: list[str] = []
            for p in underdog_picks_list:
                all_tags.extend(p.get("tags", []))
            top_tags = [
                {"tag": t, "count": c}
                for t, c in Counter(all_tags).most_common(3)
            ]

            underdog_picks.append({
                "fight": f"{fight['fighter_a']} vs {fight['fighter_b']}",
                "fighter_a": fight["fighter_a"],
                "fighter_b": fight["fighter_b"],
                "underdog": underdog_fighter,
                "underdog_count": underdog_count,
                "favorite_count": favorite_count,
                "total_predictions": total,
                "underdog_percentage": (underdog_count / total) * 100,
                "high_accuracy_analysts": [
                    {"name": p["analyst_name"], "accuracy": 0, "reasoning": p.get("reasoning_notes")}
                    for p in underdog_picks_list
                ],
                "value_score": underdog_count / total,
                "top_tags": top_tags,
            })

        underdog_picks.sort(key=lambda x: x["value_score"], reverse=True)

        return {
            "event": event["name"],
            "results_entered": False,
            "underdog_picks": underdog_picks,
        }


# ---------------------------------------------------------------------------
# PromptGenerator  (ported directly from ChatMMA)
# ---------------------------------------------------------------------------

class PromptGenerator:
    """Builds lean, focused prompts for each query type."""

    @staticmethod
    def build_fight_analysis_prompt(context: dict, user_question: str) -> str:
        fight = context["fight"]
        summary = context["summary"]
        a_ctx = context["fighter_a_context"]
        b_ctx = context["fighter_b_context"]
        analyst_info = context.get("analyst_info", {})

        prompt = f"""You are ChatMMAPicks, an AI that synthesizes MMA analyst predictions.

USER QUESTION: {user_question}

FIGHT CONTEXT:
Event: {fight['event']}
Fight: {fight['fighter_a']} vs {fight['fighter_b']}

PREDICTION SUMMARY:
- Total analysts: {summary['total_predictions']}
- Picking {fight['fighter_a']}: {summary['picks_for_a']} analysts
- Picking {fight['fighter_b']}: {summary['picks_for_b']} analysts
"""

        for fighter, ctx in [(fight['fighter_a'], a_ctx), (fight['fighter_b'], b_ctx)]:
            if ctx['top_tags']:
                prompt += f"\nKEY FACTORS FOR {fighter.upper()}:\n"
                for t in ctx['top_tags'][:5]:
                    prompt += f"- {t['tag'].replace('_', ' ')}: mentioned by {t['count']} analysts\n"
            if ctx['methods']:
                methods_str = ", ".join(
                    f"{m} ({c})" for m, c in ctx['methods'].items()
                )
                prompt += f"Expected methods: {methods_str}\n"
            if ctx['example_rationales']:
                prompt += f"\nExample analyst reasoning for {fighter}:\n"
                for i, note in enumerate(ctx['example_rationales'][:2], 1):
                    prompt += f"{i}. {note[:200]}...\n"

        if analyst_info.get("reveal_names"):
            prompt += f"\nTOP ANALYSTS:\n"
            prompt += f"For {fight['fighter_a']}: {', '.join(analyst_info.get('top_analysts_a', [])[:3])}\n"
            prompt += f"For {fight['fighter_b']}: {', '.join(analyst_info.get('top_analysts_b', [])[:3])}\n"
        else:
            prompt += (
                f"\n- {analyst_info.get('fighter_a_high_accuracy_count', 0)} analysts "
                f"picked {fight['fighter_a']}\n"
                f"- {analyst_info.get('fighter_b_high_accuracy_count', 0)} analysts "
                f"picked {fight['fighter_b']}\n"
            )

        prompt += """
INSTRUCTIONS:
1. Answer the user's question based on the consensus and reasoning above
2. Focus on WHY analysts favor each fighter, not just the numbers
3. Mention specific context tags and analyst reasoning
4. If asked about methods, reference the expected finish types
5. Keep response conversational and insightful (2-4 paragraphs)

RESPONSE:
"""
        return prompt

    @staticmethod
    def build_inside_distance_prompt(context: dict, user_question: str) -> str:
        prompt = f"""You are ChatMMAPicks, an AI that synthesizes MMA analyst predictions.

USER QUESTION: {user_question}

EVENT: {context['event']}

FIGHTERS MOST LIKELY TO WIN INSIDE THE DISTANCE (KO/TKO/SUB):
"""
        if not context['inside_distance_picks']:
            prompt += "\nNo fighters have significant finish predictions for this event.\n"
        else:
            for idx, pick in enumerate(context['inside_distance_picks'][:10], 1):
                method_counts: dict = {}
                for m in pick['methods']:
                    method_counts[m['method']] = method_counts.get(m['method'], 0) + 1
                prompt += (
                    f"\n{idx}. {pick['favored_fighter']} ({pick['fight']})\n"
                    f"   - {pick['finish_prediction_count']} analysts predict finish\n"
                    f"   - Methods: {', '.join(f'{m} ({c})' for m, c in method_counts.items())}\n"
                )

        prompt += """
INSTRUCTIONS:
1. Answer the user's question about which fighters are most likely to win inside the distance
2. Focus on the fighters with the most finish predictions
3. Mention the expected methods (KO, TKO, SUB)
4. Keep response conversational and actionable (2-3 paragraphs)

RESPONSE:
"""
        return prompt

    @staticmethod
    def build_consensus_picks_prompt(context: dict, user_question: str) -> str:
        prompt = f"""You are ChatMMAPicks, an AI that synthesizes MMA analyst predictions.

USER QUESTION: {user_question}

EVENT: {context['event']}

CONSENSUS PICKS (sorted by strength):
"""
        for idx, pick in enumerate(context['consensus_picks'], 1):
            other = pick['fighter_a'] if pick['consensus_fighter'] == pick['fighter_b'] else pick['fighter_b']
            prompt += (
                f"\n{idx}. {pick['consensus_fighter']} over {other}\n"
                f"   - Consensus: {pick['consensus_count']}-{pick['opposing_count']} "
                f"({pick['consensus_percentage']:.0f}%)\n"
            )

        prompt += """
INSTRUCTIONS:
1. Answer the user's question about consensus picks
2. Focus on the strongest consensus picks (highest percentages)
3. Highlight interesting patterns or contrarian fights
4. Keep response conversational and actionable (2-3 paragraphs)

RESPONSE:
"""
        return prompt

    @staticmethod
    def build_underdogs_prompt(context: dict, user_question: str) -> str:
        prompt = f"""You are ChatMMAPicks, an AI that synthesizes MMA analyst predictions.

USER QUESTION: {user_question}

EVENT: {context['event']}

BEST UNDERDOG PICKS (sorted by value):
"""
        if not context['underdog_picks']:
            prompt += "\nNo clear underdog opportunities identified for this event.\n"
        else:
            for idx, pick in enumerate(context['underdog_picks'][:8], 1):
                prompt += (
                    f"\n{idx}. {pick['underdog']} ({pick['fight']})\n"
                    f"   - Underdog pick: {pick['underdog_count']}-{pick['favorite_count']} "
                    f"({pick['underdog_percentage']:.0f}%)\n"
                )
                if pick['top_tags']:
                    tags_str = ', '.join(t['tag'].replace('_', ' ') for t in pick['top_tags'])
                    prompt += f"   - Key factors: {tags_str}\n"
                if pick['high_accuracy_analysts']:
                    names = [a['name'] for a in pick['high_accuracy_analysts'][:3]]
                    prompt += f"   - Backed by: {', '.join(names)}\n"

        prompt += """
INSTRUCTIONS:
1. Answer the user's question about underdog picks
2. Explain why these underdogs have potential despite being less popular picks
3. Keep response conversational and actionable (2-3 paragraphs)

RESPONSE:
"""
        return prompt

    @staticmethod
    def build_general_prompt(user_question: str) -> str:
        return f"""You are ChatMMAPicks, an AI assistant for MMA predictions.

The user asked: {user_question}

This appears to be a general question. Respond helpfully and direct them to ask about
specific fights or events if appropriate. You can answer questions about:
- Specific fights ("who will win Jones vs Miocic?")
- Consensus picks ("what are the top picks for UFC 309?")
- Finish predictions ("who is likely to win inside the distance?")
- Underdogs ("best underdog picks for UFC Vegas 100?")

RESPONSE:
"""


# ---------------------------------------------------------------------------
# ChatMMABot
# ---------------------------------------------------------------------------

class ChatMMABot:
    """Main chatbot: detects query type, fetches context, calls Claude."""

    def __init__(self, api_key: str):
        self.client = Anthropic(api_key=api_key)
        self.model = "claude-sonnet-4-6"
        self.optimizer = QueryOptimizer()
        self.generator = PromptGenerator()

    # ── query-type detection ────────────────────────────────────────────────

    def detect_query_type(self, question: str) -> tuple[str, dict]:
        q = question.lower()

        # Inside the distance
        if any(
            kw in q
            for kw in [
                "inside the distance", "inside distance", "finish",
                "knockout", " ko ", "submission", "most likely to finish",
                "not go the distance",
            ]
        ):
            return ("inside_distance", {"event_name": self._extract_event_name(q)})

        # Consensus
        if any(
            kw in q
            for kw in [
                "consensus", "top picks", "favorites", "who should win",
                "most likely to win", "best bets", "safest picks", "locks",
            ]
        ):
            return ("consensus_picks", {"event_name": self._extract_event_name(q)})

        # Underdogs
        if any(
            kw in q
            for kw in [
                "underdog", "upset", "dark horse", "value pick", "sleeper",
                "best underdog", "undervalued", "contrarian",
            ]
        ):
            return ("underdogs", {"event_name": self._extract_event_name(q)})

        # Fight-specific  ("X vs Y")
        for sep in [" vs ", " vs. ", " versus ", " v ", " against "]:
            if sep in q:
                parts = q.split(sep)
                if len(parts) >= 2:
                    left_words = parts[0].strip().split()
                    right_words = parts[1].strip().split()
                    fa_words = left_words[-2:] if len(left_words) >= 2 else left_words[-1:]
                    fb_words = right_words[:2] if len(right_words) >= 2 else right_words[:1]
                    fa = re.sub(r"[^\w\s']", "", " ".join(fa_words)).strip().title()
                    fb = re.sub(r"[^\w\s']", "", " ".join(fb_words)).strip().title()
                    return ("fight_specific", {"fighter_a": fa, "fighter_b": fb, "event_name": None})

        return ("general", {})

    def _extract_event_name(self, q: str) -> str | None:
        m = re.search(r"ufc\s+(\d+|vegas\s+\d+|fight\s+night\s+\d+)", q)
        if m:
            return f"UFC {m.group(1).title()}"
        # Fall back to the most recent event in the DB
        db = get_supabase()
        resp = (
            db.table("events")
            .select("name")
            .order("date", desc=True)
            .limit(1)
            .execute()
        )
        if resp.data:
            return resp.data[0]["name"]
        return None

    # ── query handlers ───────────────────────────────────────────────────────

    def answer_question(self, user_question: str) -> dict:
        query_type, details = self.detect_query_type(user_question)

        handlers = {
            "fight_specific":  self._handle_fight_specific,
            "inside_distance": self._handle_inside_distance,
            "consensus_picks": self._handle_consensus_picks,
            "underdogs":       self._handle_underdogs,
            "general":         self._handle_general,
        }
        return handlers[query_type](user_question, details)

    def _call_claude(self, prompt: str, max_tokens: int = 800) -> tuple[str, dict]:
        response = self.client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        cost = self._estimate_cost(response.usage)
        return response.content[0].text, cost

    def _handle_fight_specific(self, question: str, details: dict) -> dict:
        fa, fb = details["fighter_a"], details["fighter_b"]
        fight = self.optimizer.get_fight_by_fighters(fa, fb, details.get("event_name"))

        if not fight:
            return {
                "answer": (
                    f"I couldn't find a fight between {fa} and {fb}. "
                    "Please check the fighter names or try specifying the event."
                ),
                "metadata": {"query_type": "fight_not_found"},
            }

        context = self.optimizer.aggregate_fight_context(fight["fight_id"], fight)
        if not context or context["summary"]["total_predictions"] == 0:
            return {
                "answer": (
                    f"Found the fight, but there are no analyst predictions yet for "
                    f"{fight['fighter_a']} vs {fight['fighter_b']}. "
                    "Try ingesting some articles from the URL Ingestion page."
                ),
                "metadata": {"query_type": "no_predictions"},
            }

        prompt = self.generator.build_fight_analysis_prompt(context, question)
        answer, cost = self._call_claude(prompt, max_tokens=800)
        return {
            "answer": answer,
            "metadata": {
                "query_type": "fight_analysis",
                "fight": fight,
                "cost_estimate": cost,
            },
        }

    def _handle_inside_distance(self, question: str, details: dict) -> dict:
        event_name = details.get("event_name")
        if not event_name:
            return {
                "answer": "Please specify an event (e.g., 'UFC 309') to get inside-distance predictions.",
                "metadata": {"query_type": "missing_event"},
            }
        context = self.optimizer.get_inside_distance_picks(event_name)
        if not context:
            return {
                "answer": f"I don't have predictions for '{event_name}' yet.",
                "metadata": {"query_type": "event_not_found"},
            }
        prompt = self.generator.build_inside_distance_prompt(context, question)
        answer, cost = self._call_claude(prompt, max_tokens=800)
        return {
            "answer": answer,
            "metadata": {"query_type": "inside_distance", "cost_estimate": cost},
        }

    def _handle_consensus_picks(self, question: str, details: dict) -> dict:
        event_name = details.get("event_name")
        if not event_name:
            return {
                "answer": "Please specify an event (e.g., 'UFC 309') to get consensus picks.",
                "metadata": {"query_type": "missing_event"},
            }
        context = self.optimizer.get_event_consensus_picks(event_name)
        if not context:
            return {
                "answer": f"I don't have predictions for '{event_name}' yet.",
                "metadata": {"query_type": "event_not_found"},
            }
        if not context["consensus_picks"]:
            return {
                "answer": f"Found '{event_name}', but not enough predictions to determine consensus yet.",
                "metadata": {"query_type": "no_consensus"},
            }
        prompt = self.generator.build_consensus_picks_prompt(context, question)
        answer, cost = self._call_claude(prompt, max_tokens=1000)
        return {
            "answer": answer,
            "metadata": {"query_type": "consensus_picks", "cost_estimate": cost},
        }

    def _handle_underdogs(self, question: str, details: dict) -> dict:
        event_name = details.get("event_name")
        if not event_name:
            return {
                "answer": "Please specify an event (e.g., 'UFC 309') to get underdog picks.",
                "metadata": {"query_type": "missing_event"},
            }
        context = self.optimizer.get_event_underdogs(event_name)
        if not context:
            return {
                "answer": f"I don't have predictions for '{event_name}' yet.",
                "metadata": {"query_type": "event_not_found"},
            }
        if not context["underdog_picks"]:
            return {
                "answer": f"Found '{event_name}', but no clear underdogs — consensus is strong across all fights.",
                "metadata": {"query_type": "no_underdogs"},
            }
        prompt = self.generator.build_underdogs_prompt(context, question)
        answer, cost = self._call_claude(prompt, max_tokens=1000)
        return {
            "answer": answer,
            "metadata": {"query_type": "underdogs", "cost_estimate": cost},
        }

    def _handle_general(self, question: str, details: dict) -> dict:
        prompt = self.generator.build_general_prompt(question)
        answer, cost = self._call_claude(prompt, max_tokens=400)
        return {
            "answer": answer,
            "metadata": {"query_type": "general", "cost_estimate": cost},
        }

    # ── cost estimation ─────────────────────────────────────────────────────

    @staticmethod
    def _estimate_cost(usage) -> dict:
        input_cost = (usage.input_tokens / 1_000_000) * 3.0
        output_cost = (usage.output_tokens / 1_000_000) * 15.0
        return {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.input_tokens + usage.output_tokens,
            "cost_usd": round(input_cost + output_cost, 5),
        }
