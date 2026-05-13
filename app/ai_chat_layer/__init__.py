"""AI Chat Layer — Gemini-powered "Ask Your Data" assistant.

Runs as a sibling FastAPI process to the regular chat service (chat_main.py)
on port 8518. Reuses the existing chat persistence, S3, Redis, and auth
plumbing — the AI Assistant is just another DM from a synthetic bot user.
"""
