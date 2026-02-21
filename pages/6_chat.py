"""
ChatMMAPicks – Chat page.

Natural-language Q&A over analyst predictions stored in Supabase.
Supports fight-specific questions, consensus picks, inside-distance picks,
underdog analysis, and general MMA queries.
"""

import streamlit as st

# ── API key check ────────────────────────────────────────────────────────────

def _get_api_key() -> str | None:
    # Support both nested [anthropic] section and flat ANTHROPIC_API_KEY,
    # matching the same pattern used by pages/1_url_ingestion.py.
    try:
        if "anthropic" in st.secrets:
            return st.secrets["anthropic"]["api_key"]
        if "ANTHROPIC_API_KEY" in st.secrets:
            return st.secrets["ANTHROPIC_API_KEY"]
    except Exception:
        pass
    return None


api_key = _get_api_key()

if not api_key:
    st.title("Chat")
    st.error(
        "Anthropic API key not found in Streamlit secrets. "
        "Add one of these to your app's **Settings → Secrets** on Streamlit Cloud:\n\n"
        "```toml\n"
        "# Option A – nested section (matches the rest of this app)\n"
        "[anthropic]\n"
        'api_key = "sk-ant-..."\n\n'
        "# Option B – flat key\n"
        'ANTHROPIC_API_KEY = "sk-ant-..."\n'
        "```"
    )
    st.stop()

# ── lazy-import (keeps error surfaced above) ─────────────────────────────────

from utils.chat import ChatMMABot  # noqa: E402  (imported after key check)


@st.cache_resource
def _init_bot(key: str) -> ChatMMABot:
    return ChatMMABot(api_key=key)


bot = _init_bot(api_key)

# ── session state ─────────────────────────────────────────────────────────────

if "chat_messages" not in st.session_state:
    st.session_state.chat_messages = []

if "chat_total_cost" not in st.session_state:
    st.session_state.chat_total_cost = 0.0

if "chat_query_count" not in st.session_state:
    st.session_state.chat_query_count = 0

# ── layout ────────────────────────────────────────────────────────────────────

st.title("ChatMMAPicks")
st.caption("Ask anything about analyst predictions in your database.")

# Sidebar
with st.sidebar:
    st.subheader("Session Stats")
    st.metric("Queries", st.session_state.chat_query_count)
    st.metric("Total Cost", f"${st.session_state.chat_total_cost:.4f}")
    if st.session_state.chat_query_count > 0:
        avg = st.session_state.chat_total_cost / st.session_state.chat_query_count
        st.metric("Avg Cost / Query", f"${avg:.5f}")

    st.divider()
    st.markdown(
        """
**What you can ask:**
- *Who will win Jones vs Aspinall?*
- *What are the consensus picks for UFC 309?*
- *Which fighters are likely to finish inside the distance at UFC Vegas 100?*
- *Best underdog picks for UFC 310?*
"""
    )
    st.divider()

    if st.button("Clear chat history"):
        st.session_state.chat_messages = []
        st.session_state.chat_total_cost = 0.0
        st.session_state.chat_query_count = 0
        st.rerun()

# ── message history ───────────────────────────────────────────────────────────

for msg in st.session_state.chat_messages:
    with st.chat_message(msg["role"]):
        st.markdown(msg["content"])
        if msg["role"] == "assistant" and msg.get("cost"):
            cost = msg["cost"]
            st.caption(
                f"Cost: ${cost['cost_usd']:.5f} "
                f"({cost['total_tokens']:,} tokens)"
            )

# ── input ─────────────────────────────────────────────────────────────────────

if prompt := st.chat_input("Ask about any fight or event…"):
    # Show user message
    st.session_state.chat_messages.append({"role": "user", "content": prompt})
    with st.chat_message("user"):
        st.markdown(prompt)

    # Get answer
    with st.chat_message("assistant"):
        with st.spinner("Analyzing predictions…"):
            try:
                result = bot.answer_question(prompt)
                answer = result["answer"]
                cost = result.get("metadata", {}).get("cost_estimate")

                st.markdown(answer)
                if cost:
                    st.caption(
                        f"Cost: ${cost['cost_usd']:.5f} "
                        f"({cost['total_tokens']:,} tokens)"
                    )
                    st.session_state.chat_total_cost += cost["cost_usd"]
                st.session_state.chat_query_count += 1

            except Exception as exc:
                answer = f"Error: {exc}"
                cost = None
                st.error(answer)

    st.session_state.chat_messages.append(
        {"role": "assistant", "content": answer, "cost": cost}
    )
