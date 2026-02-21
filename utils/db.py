import streamlit as st
from supabase import create_client, Client


@st.cache_resource
def get_supabase() -> Client:
    """Return a cached Supabase client using service_role credentials from st.secrets."""
    url = st.secrets["supabase"]["url"]
    key = st.secrets["supabase"]["service_role_key"]
    return create_client(url, key)


@st.cache_data(ttl=300)
def get_fighter_aliases() -> list[dict]:
    """Fetch all fighter aliases from the database (cached 5 min)."""
    db = get_supabase()
    resp = db.table("fighter_aliases").select("alias_id, canonical_name, alias").execute()
    return resp.data or []


def save_alias(canonical_name: str, alias: str) -> None:
    """Upsert a fighter alias and bust the cache."""
    db = get_supabase()
    db.table("fighter_aliases").upsert(
        {"canonical_name": canonical_name, "alias": alias},
        on_conflict="alias",
    ).execute()
    get_fighter_aliases.clear()


def get_or_create_event(
    name: str,
    date: str | None = None,
    location: str | None = None,
) -> str:
    """Return event_id for an existing event (case-insensitive) or create a new one.
    If the event already exists, fills in date/location if they were previously blank.
    """
    db = get_supabase()
    resp = (
        db.table("events")
        .select("event_id, date, location")
        .ilike("name", name)
        .limit(1)
        .execute()
    )
    if resp.data:
        event_id = resp.data[0]["event_id"]
        updates: dict = {}
        if date and not resp.data[0].get("date"):
            updates["date"] = date
        if location and not resp.data[0].get("location"):
            updates["location"] = location
        if updates:
            db.table("events").update(updates).eq("event_id", event_id).execute()
        return event_id

    insert_data: dict = {"name": name}
    if date:
        insert_data["date"] = date
    if location:
        insert_data["location"] = location
    resp = db.table("events").insert(insert_data).execute()
    return resp.data[0]["event_id"]


def get_or_create_fight(
    event_id: str,
    fighter_a: str,
    fighter_b: str,
    weight_class: str | None = None,
) -> str:
    """Return fight_id for an existing fight (either order) or create a new one.
    If the fight already exists, fills in weight_class if it was previously blank.
    """
    db = get_supabase()
    for fa, fb in [(fighter_a, fighter_b), (fighter_b, fighter_a)]:
        resp = (
            db.table("fights")
            .select("fight_id, weight_class")
            .eq("event_id", event_id)
            .eq("fighter_a", fa)
            .eq("fighter_b", fb)
            .execute()
        )
        if resp.data:
            fight_id = resp.data[0]["fight_id"]
            if weight_class and not resp.data[0].get("weight_class"):
                db.table("fights").update({"weight_class": weight_class}).eq("fight_id", fight_id).execute()
            return fight_id

    insert_data: dict = {"event_id": event_id, "fighter_a": fighter_a, "fighter_b": fighter_b}
    if weight_class:
        insert_data["weight_class"] = weight_class
    resp = db.table("fights").insert(insert_data).execute()
    return resp.data[0]["fight_id"]


def save_analyst_pick(pick_data: dict) -> str:
    """Insert a row into analyst_picks and return the new pick_id."""
    db = get_supabase()
    resp = db.table("analyst_picks").insert(pick_data).execute()
    return resp.data[0]["pick_id"]


def save_pick_tags(pick_id: str, tags: list[str]) -> None:
    """Insert tags for a pick (skips empty list)."""
    if not tags:
        return
    db = get_supabase()
    rows = [{"pick_id": pick_id, "tag": t.strip()} for t in tags if t.strip()]
    if rows:
        db.table("pick_tags").insert(rows).execute()


def get_events() -> list[dict]:
    """Return all events ordered by date descending."""
    db = get_supabase()
    resp = (
        db.table("events")
        .select("event_id, name, date, location")
        .order("date", desc=True)
        .execute()
    )
    return resp.data or []


def get_picks_for_event(event_id: str) -> list[dict]:
    """Return a flat list of all picks for an event, joined with fight and event data."""
    db = get_supabase()

    # Get all fights for this event
    fights_resp = (
        db.table("fights")
        .select("fight_id, fighter_a, fighter_b, weight_class, bout_order")
        .eq("event_id", event_id)
        .execute()
    )
    fights = {f["fight_id"]: f for f in (fights_resp.data or [])}

    if not fights:
        return []

    # Get event info
    event_resp = (
        db.table("events")
        .select("name, date, location")
        .eq("event_id", event_id)
        .limit(1)
        .execute()
    )
    event = event_resp.data[0] if event_resp.data else {}

    # Get all picks for those fights
    fight_ids = list(fights.keys())
    picks_resp = (
        db.table("analyst_picks")
        .select("pick_id, fight_id, analyst_name, platform, source_url, picked_fighter, method_prediction, confidence_tag, reasoning_notes, created_at")
        .in_("fight_id", fight_ids)
        .execute()
    )
    picks = picks_resp.data or []

    # Get tags for all picks
    pick_ids = [p["pick_id"] for p in picks]
    tags_by_pick: dict[str, list[str]] = {}
    if pick_ids:
        tags_resp = (
            db.table("pick_tags")
            .select("pick_id, tag")
            .in_("pick_id", pick_ids)
            .execute()
        )
        for row in (tags_resp.data or []):
            tags_by_pick.setdefault(row["pick_id"], []).append(row["tag"])

    # Assemble flat rows
    rows = []
    for pick in picks:
        fight = fights.get(pick["fight_id"], {})
        tags = tags_by_pick.get(pick["pick_id"], [])
        context_parts = [pick.get("reasoning_notes") or ""]
        if tags:
            context_parts.append(", ".join(tags))
        context = " | ".join(p for p in context_parts if p)

        rows.append({
            "date": event.get("date") or "",
            "analyst": pick.get("analyst_name") or "",
            "platform": pick.get("platform") or pick.get("analyst_name") or "",
            "event": event.get("name") or "",
            "location": event.get("location") or "",
            "fight": f"{fight.get('fighter_a', '')} vs {fight.get('fighter_b', '')}",
            "weight_class": fight.get("weight_class") or "",
            "pick": pick.get("picked_fighter") or "",
            "context": context,
            # Extra columns available in the DB but not in the original CSV
            "method": pick.get("method_prediction") or "",
            "confidence": pick.get("confidence_tag") or "",
        })

    # Sort by fight bout_order if available, then analyst name
    rows.sort(key=lambda r: (
        fights.get(next((p["fight_id"] for p in picks if p["analyst_name"] == r["analyst"]), ""), {}).get("bout_order") or 999,
        r["analyst"],
    ))

    return rows
