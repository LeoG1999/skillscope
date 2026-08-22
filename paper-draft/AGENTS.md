# Project working agreements

## CHI 2027 LaTeX format

- The current working layout intentionally uses the two-column `\documentclass[sigconf]{acmart}` format for easier reading.
- Before the initial CHI 2027 review submission, switch back to the official single-column anonymous format: `\documentclass[manuscript,review,anonymous]{acmart}`.
- The true publication-ready version also uses `\documentclass[sigconf]{acmart}`, but its author, rights, DOI, ISBN, and conference metadata must come from the authors and ACM rather than being invented.

## Build and Git automation

- Do not manually compile the LaTeX project or rebuild `main.pdf`; compilation is handled automatically.
- Do not manually commit, push, pull, stage, or otherwise operate Git; synchronization and Git operations are handled automatically.
- Make the requested source-file edits only and let the automated workflow regenerate outputs and synchronize changes.
