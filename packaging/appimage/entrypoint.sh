# -I: ignore PYTHONPATH, user site-packages and the cwd, so the bundled
# auto_review_pr_dashboard always loads.
{{ python-executable }} -I -m auto_review_pr_dashboard "$@"
