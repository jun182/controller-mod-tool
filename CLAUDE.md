# controller-mod-tool: release repo (read before editing)

This repo is the **public release copy** of the controller mod tool. Most files here are **generated** from the private
research repo (local folder `~/Desktop/_claude-projects/g8-paddles`) by its `tools/export_release.py`, so edits made here to
`g8tui.py`, `g8ota.py`, `g8fw.py`, `model_mfi.py`, `mfifw.py` or `mods/*` are overwritten at the next export.

- **Code or mod-file changes:** make them in the research repo, run its export script into this folder, then run
  `python3 g8tui.py --selftest` here and commit.
- **Edited directly here:** only `README.md`, `LICENSE`, `.gitignore` and this file.
- **Never commit** firmware images, vendor download URLs or `known_controllers.json`. This repo is public.
- Anything pushed here is public: show the user the exact change and get an OK before pushing.
