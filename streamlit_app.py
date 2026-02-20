import streamlit as st

st.set_page_config(
    page_title="ChatMMA Analyst Tracker",
    page_icon="assets/favicon.ico" if False else None,  # swap in an icon later if desired
    layout="wide",
)

pages = [
    st.Page("pages/1_url_ingestion.py",  title="URL Ingestion",  icon=None),
    st.Page("pages/2_qc_editor.py",      title="QC / Editor",    icon=None),
    st.Page("pages/3_results_entry.py",  title="Results Entry",  icon=None),
    st.Page("pages/4_analytics.py",      title="Analytics",      icon=None),
]

pg = st.navigation(pages)
pg.run()
