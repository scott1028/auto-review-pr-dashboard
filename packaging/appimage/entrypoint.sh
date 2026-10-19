# -I: ignore PYTHONPATH, user site-packages and the cwd, so the bundled
# auto_review_pr_dashboard always loads.
# Inside the AppImage sys.executable is the AppImage itself, so the daemon spawn
# (`sys.executable -m <module>`) lands here; pass it to Python, not the CLI.
if [ "$1" = "-m" ]; then
    {{ python-executable }} -I "$@"
else
    {{ python-executable }} -I -m auto_review_pr_dashboard "$@"
fi
