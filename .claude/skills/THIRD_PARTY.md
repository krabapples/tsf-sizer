# Third-party skills

The skills in this folder, except any added later by the team, come from
https://github.com/nextlevelbuilder/ui-ux-pro-max-skill (MIT license, see
LICENSE-ui-ux-pro-max), version 2.13.0, commit 09170eec67eefd46a7ae85de61b40c194020f997.

| Skill | Use in this project |
|---|---|
| ui-ux-pro-max | Main UI/UX guidance: styles, palettes, font pairings, UX rules, stack guidance (React, Tailwind, shadcn/ui) |
| ui-styling | shadcn/ui + Tailwind component and theming guidance |
| design-system | Design tokens (primitive → semantic → component) |
| brand, design, banner-design, slides | Branding, logos, banners, HTML slides (not needed for the web UI) |

Security notes (reviewed at install): the ui-ux-pro-max search scripts are
local-only. `design/scripts/logo|icon` call external image APIs (Gemini,
Atlas Cloud, MuAPI) only when their API keys are set; never set them for
customer or internal data. `ui-styling/scripts/shadcn_add.py` runs
`npx shadcn` locally.

To update: re-clone the repo and copy `.claude/skills/` over these folders.

Local change: in `ui-ux-pro-max/SKILL.md`, `${CLAUDE_PLUGIN_ROOT}` became
`${CLAUDE_PLUGIN_ROOT:-.}` so the script paths also work when the skill is
installed at project level (run from the repo root) instead of as a plugin.
Reapply after updating.
