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


def get_or_create_event(name: str) -> str:
    """Return event_id for an existing event (case-insensitive) or create a new one."""
    db = get_supabase()
    resp = db.table("events").select("event_id").ilike("name", name).limit(1).execute()
    if resp.data:
        return resp.data[0]["event_id"]
    resp = db.table("events").insert({"name": name}).execute()
    return resp.data[0]["event_id"]


def get_or_create_fight(event_id: str, fighter_a: str, fighter_b: str) -> str:
    """Return fight_id for an existing fight (either order) or create a new one."""
    db = get_supabase()
    for fa, fb in [(fighter_a, fighter_b), (fighter_b, fighter_a)]:
        resp = (
            db.table("fights")
            .select("fight_id")
            .eq("event_id", event_id)
            .eq("fighter_a", fa)
            .eq("fighter_b", fb)
            .execute()
        )
        if resp.data:
            return resp.data[0]["fight_id"]
    resp = db.table("fights").insert(
        {"event_id": event_id, "fighter_a": fighter_a, "fighter_b": fighter_b}
    ).execute()
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
