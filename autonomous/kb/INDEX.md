# Autonomous Claude — Knowledge Base

This directory holds the *append-only* memory that Claude reads
at the start of every cycle. Files in here are never deleted.

- `MEMORY.md`  — durable facts and decisions
- `facts.jsonl` — structured per-fact log
- `cycle-notes/` — short notes per cycle (auto-created)
