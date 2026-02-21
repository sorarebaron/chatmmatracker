import streamlit as st

st.set_page_config(
    page_title="ChatMMAPicks",
    page_icon=None,
    layout="wide",
)

pages = [
    st.Page("pages/1_url_ingestion.py",  title="URL Ingestion",  icon=None),
    st.Page("pages/2_qc_editor.py",      title="QC / Editor",    icon=None),
    st.Page("pages/3_results_entry.py",  title="Results Entry",  icon=None),
    st.Page("pages/4_analytics.py",      title="Analytics",      icon=None),
    st.Page("pages/5_export.py",         title="Export",         icon=None),
    st.Page("pages/6_chat.py",           title="Chat",           icon=None),
]

pg = st.navigation(pages)
pg.run()
