# Model store

The single, controlled location where claude-local stores and resolves local MLX models —
never a scattered cache elsewhere. claude-local discovers the models present here at runtime
(no config list). Model weights are git-ignored (large binaries, never committed); only this
file is tracked, to keep the directory in the repo. Downloads are explicit and user-initiated —
claude-local never pulls a model on its own.
