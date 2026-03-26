# Handoff Notes — Ticket Takeaway

## For the Next Agent

### Directory Rename Pending
The directory is currently `~/projects/software-dashboard` but the project has been rebranded to **Ticket Takeaway**. The user will rename the directory to `ticket-takeaway` between sessions. All internal docs already use the new name.

### What's Ready
- All docs rebranded to "Ticket Takeaway"
- `.gitignore` configured (excludes `.claude/`, generated HTML, feature working files)
- No identifying paths or project-specific names in tracked files
- README has ASCII art header
- All source files in `src/` (generate.py, skills)

### Next Steps
1. User renames directory: `mv ~/projects/software-dashboard ~/projects/ticket-takeaway`
2. Update registry path: `~/.claude/ticket-takeaway/registry.json` — change path from software-dashboard to ticket-takeaway
3. `git init` in the renamed directory
4. Initial commit with all files
5. Create GitHub repo and push

### Key Files
- `src/generate.py` — the generator script (also deployed at `~/.claude/ticket-takeaway/generate.py`)
- `src/skills/ticket-takeaway/SKILL.md` — the /dashboard skill (deployed at `~/.claude/skills/ticket-takeaway/SKILL.md`)
- `src/skills/review/SKILL.md` — the /review skill (deployed at `~/.claude/skills/review/SKILL.md`)
- `docs/LIFECYCLE.md` — authoritative lifecycle spec
- `docs/REVIEW_PROCESS.md` — review process spec

### Deployment
Source files in `src/` are the canonical copies. They get deployed to `~/.claude/` for Claude Code to use. See `INSTALL.md` for the deployment map.
